"""Mức dùng/ngày phải là tốc độ KHI NHÀ MÁY CHẠY, không bị pha loãng bởi ngày nghỉ.

Bug thật, người dùng phát hiện 28/09/2026: bấm "Áp dụng số đo thực tế" trên trang
Kế hoạch ra 3,44 m³/ngày, trong khi báo cáo của chính cổng nguồn cho sáu ngày liền
tụt đều 6,42…6,87, trung bình 6,65. Đo mốc 07:00 mỗi ngày trong tháng:

    28/08 → 14/09   18 ngày đứng yên ~53 m³, lưu lượng khí 0      ~0,03 m³/ngày
    15/09 → 28/09   14 ngày chạy, lưu lượng 90–134 Nm³/h          ~6,65 m³/ngày

Cửa sổ 30 ngày chia tổng lượng dùng cho cả những ngày nhà máy tắt máy, nên ra con
số lưng chừng — gần đúng một nửa, lại kèm nhãn tin cậy "cao". Và con số đó chạy
vào dự báo cạn lẫn khuyến nghị đặt hàng, nên cả hai tính như thể bồn cạn chậm gấp
đôi thực tế.

Dữ liệu dưới đây dựng lại đúng hình dạng đó, kể cả thứ làm cách tiếp cận ngây thơ
hỏng: cảm biến báo theo LƯỢNG TỬ 100 L và dao động qua lại quanh mức thật.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.domain import forecast as F
from app.domain.forecast import Sample, estimate_consumption

VN = ZoneInfo("Asia/Ho_Chi_Minh")
CAP = 60_000.0
STEP = timedelta(minutes=30)
PER_DAY_STEPS = 48
RUN_L = 6650.0
T0 = datetime(2026, 8, 28, 0, 0, tzinfo=VN)


def _q(v: float) -> float:
    """Làm tròn về lượng tử 100 L, như cảm biến của nguồn phút."""
    return round(v / 100.0) * 100.0


def _thang_that() -> list[Sample]:
    """18 ngày nghỉ rồi 14 ngày chạy, một lần nạp ở giữa, cảm biến có dao động."""
    out: list[Sample] = []
    v = 53_480.0
    i = 0
    # --- 18 ngày NGHỈ: trôi rất chậm (boil-off) + nhấp nháy một lượng tử
    for _ in range(18 * PER_DAY_STEPS):
        v -= 34.0 / PER_DAY_STEPS
        flicker = 100.0 if i % 7 == 3 else 0.0
        out.append(Sample(at=T0 + i * STEP, volume_l=_q(v) + flicker))
        i += 1
    # --- 14 ngày CHẠY, có một lần nạp vào ngày thứ 7
    for d in range(14):
        for k in range(PER_DAY_STEPS):
            if d == 7 and k == 0:
                v += 40_000.0  # xe bồn tới: một bước vượt xa ngưỡng nạp
            v -= RUN_L / PER_DAY_STEPS
            out.append(Sample(at=T0 + i * STEP, volume_l=_q(v)))
            i += 1
    return out


def _khong_loai_ngay_nghi(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dựng lại đúng hành vi CŨ để test chứng minh được nó bắt lỗi."""
    monkeypatch.setattr(F, "_idle_day_keys", lambda per_day, covered_s: set())


# ------------------------------------------------------------ ca thật


def test_thang_that_ra_toc_do_khi_chay_khong_bi_pha_loang() -> None:
    est = estimate_consumption(_thang_that(), capacity_l=CAP, tz=VN)
    assert est.daily_use_l is not None
    assert est.daily_use_l == pytest.approx(RUN_L, rel=0.03), (
        f"{est.daily_use_l:.0f} L/ngày — phải là tốc độ khi chạy, ~{RUN_L:.0f}"
    )


def test_cach_cu_that_su_bi_pha_loang_con_khoang_mot_nua(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Đối chứng: không có nó thì test trên không chứng minh được gì."""
    _khong_loai_ngay_nghi(monkeypatch)
    est = estimate_consumption(_thang_that(), capacity_l=CAP, tz=VN)
    assert est.daily_use_l is not None
    assert est.daily_use_l < 0.6 * RUN_L, (
        f"cách cũ ra {est.daily_use_l:.0f} — nếu không bị pha loãng thì dữ liệu "
        "test không tái hiện được bug"
    )


def test_bao_ra_so_ngay_nghi_da_loai() -> None:
    est = estimate_consumption(_thang_that(), capacity_l=CAP, tz=VN)
    assert est.idle_days == pytest.approx(18.0, abs=1.0)
    # coverage vẫn đo MẤT UPLOAD: thiết bị gửi đều cả những ngày nghỉ.
    assert est.coverage > 0.95


def test_do_lech_chuan_la_cua_ngay_chay_khong_phai_cua_lich_nghi() -> None:
    """Trộn ngày ~0 với ngày ~6650 cho ra sigma phản ánh LỊCH NGHỈ, không phải
    độ thất thường — và dự trữ an toàn sinh ra từ nó to vô lý."""
    est = estimate_consumption(_thang_that(), capacity_l=CAP, tz=VN)
    assert est.daily_use_sd_l is not None
    assert est.daily_use_sd_l < 0.1 * RUN_L


# ------------------------------------------------------------ không đổi gì khi không có nghỉ


def test_nha_may_chay_deu_thi_KET_QUA_Y_HET_CU(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cam kết của bản sửa: không có đợt nghỉ thì không một con số nào đổi."""
    samples = [
        Sample(at=T0 + i * STEP, volume_l=_q(58_000.0 - RUN_L * i / PER_DAY_STEPS))
        for i in range(8 * PER_DAY_STEPS)
    ]
    moi = estimate_consumption(samples, capacity_l=CAP, tz=VN)
    _khong_loai_ngay_nghi(monkeypatch)
    cu = estimate_consumption(samples, capacity_l=CAP, tz=VN)

    assert moi.idle_days == 0.0
    assert moi.daily_use_l == pytest.approx(cu.daily_use_l)
    assert moi.daily_use_sd_l == pytest.approx(cu.daily_use_sd_l)
    assert moi.confidence == cu.confidence


def test_dao_dong_mot_luong_tu_khong_bien_ngay_nghi_thanh_ngay_chay() -> None:
    """Lý do KHÔNG dùng _idle_windows: nó xét từng bước với ngưỡng 60 L, nên mỗi lần
    nhấp nháy 100 L đều bị coi là 'đang rút'. Tổng có dấu của cả ngày thì triệt
    tiêu dao động."""
    est = estimate_consumption(_thang_that(), capacity_l=CAP, tz=VN)
    # 18 ngày nghỉ đều có nhấp nháy; nếu có ngày nào lọt thành "ngày chạy" thì
    # idle_days sẽ tụt rõ rệt dưới 18.
    assert est.idle_days >= 17.0


# ------------------------------------------------------------ nghỉ hằng tuần


def test_nghi_chu_nhat_hang_tuan_thi_ra_muc_dung_mot_ngay_LAM_VIEC() -> None:
    """Hệ quả được ghi rõ trong docstring: đúng mô hình của trang Kế hoạch, nơi
    ngày nghỉ được đánh dấu riêng."""
    out: list[Sample] = []
    v = 58_000.0
    i = 0
    # T0 = 28/08/2026 là thứ Sáu; chạy 5 tuần.
    for d in range(35):
        chu_nhat = (T0 + timedelta(days=d)).weekday() == 6
        for _ in range(PER_DAY_STEPS):
            if not chu_nhat:
                v -= 1500.0 / PER_DAY_STEPS
            out.append(Sample(at=T0 + i * STEP, volume_l=_q(v)))
            i += 1
    est = estimate_consumption(out, capacity_l=CAP, tz=VN)
    assert est.daily_use_l == pytest.approx(1500.0, rel=0.05)
    assert est.idle_days == pytest.approx(5.0, abs=1.0)


# ------------------------------------------------------------ biên


def test_it_hon_ba_ngay_thi_khong_phan_biet_duoc_nghi_voi_chay_cham() -> None:
    samples = [
        Sample(at=T0 + i * STEP, volume_l=_q(50_000.0 - 3000.0 * i / PER_DAY_STEPS))
        for i in range(2 * PER_DAY_STEPS)
    ]
    est = estimate_consumption(samples, capacity_l=CAP, tz=VN)
    assert est.idle_days == 0.0


def test_percentile_tra_ve_mot_gia_tri_CO_THAT() -> None:
    xs = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    assert F._percentile(xs, 0.9) == 90.0
    assert F._percentile(xs, 0.9) in xs
    assert F._percentile([], 0.9) is None
