"""Tiêu thụ đo hai đường và phép đối chứng giữa chúng.

Mỗi bài ở đây tương ứng một chế độ hỏng THẬT đã quan sát hoặc suy ra được từ
nguồn đo phút, không phải một trường hợp bịa cho đủ:

- đồng hồ khí đứng           -> tỉ số về 0
- bộ đếm reset về 0          -> đã thấy trong dữ liệu thật
- một lần nạp không được ghi -> mức dâng che mất phần đã rút
- sai đơn vị 1000 lần        -> lỗi kinh điển khi trộn m³ với L, Nm³ với m³
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.domain.forecast import (
    RATIO_HI,
    RATIO_LO,
    VAPORIZATION_NM3_PER_M3,
    Sample,
    estimate_dual_consumption,
)

UTC = ZoneInfo("UTC")
T0 = datetime(2026, 1, 1, tzinfo=UTC)
CAP = 60_000.0  # bồn 60 m³
STEP = timedelta(minutes=30)
DROP_L = 154.0  # ~7,4 m³/ngày ở nhịp 30 phút; trên nhiễu (60 L), dưới ngưỡng nạp (1200 L)
N = 60  # 60 đoạn, 30 giờ -> vượt cả MIN_RATIO_SEGMENTS lẫn MIN_RATIO_ACTIVE_DAYS


def _series(
    n: int = N,
    *,
    ratio: float = VAPORIZATION_NM3_PER_M3,
    drop_l: float = DROP_L,
    start_l: float = 54_000.0,
    start_gas: float = 1_000_000.0,
) -> list[Sample]:
    """Chuỗi đều: mức tụt ``drop_l`` mỗi bước, khí tăng đúng theo ``ratio``."""
    out: list[Sample] = []
    vol = start_l
    gas = start_gas
    for i in range(n + 1):
        out.append(Sample(at=T0 + STEP * i, volume_l=vol, totalizer_nm3=gas))
        vol -= drop_l
        gas += (drop_l / 1000.0) * ratio
    return out


def test_khop_khi_ti_so_dung_moc_do_duoc() -> None:
    r = estimate_dual_consumption(_series(), capacity_l=CAP)
    assert r.verdict == "match"
    assert r.segments == N
    assert r.counter_resets == 0
    assert r.ratio_nm3_per_m3 is not None
    assert abs(r.ratio_nm3_per_m3 - VAPORIZATION_NM3_PER_M3) < 1.0
    # Hai con số được phát RIÊNG, không cái nào bị quy đổi thành cái nào.
    assert r.liquid_net_l is not None and r.gas_nm3 is not None
    assert abs(r.liquid_net_l - DROP_L * N) < 0.01


def test_dong_ho_khi_dung_bi_bat() -> None:
    """Totalizer không nhích trong khi mức vẫn tụt -> tỉ số 0."""
    s = [
        Sample(
            at=T0 + STEP * i,
            volume_l=54_000.0 - DROP_L * i,
            totalizer_nm3=1_000_000.0,
        )
        for i in range(N + 1)
    ]
    r = estimate_dual_consumption(s, capacity_l=CAP)
    assert r.verdict == "disagree"
    assert r.gas_nm3 == 0.0
    assert r.ratio_nm3_per_m3 == 0.0
    assert "đồng hồ khí" in r.detail


def test_sai_don_vi_nghin_lan_bi_bat() -> None:
    r = estimate_dual_consumption(
        _series(ratio=VAPORIZATION_NM3_PER_M3 * 1000), capacity_l=CAP
    )
    assert r.verdict == "disagree"
    assert r.ratio_nm3_per_m3 is not None and r.ratio_nm3_per_m3 > RATIO_HI


def test_lan_nap_khong_duoc_ghi_bi_bat() -> None:
    """Mức dâng dưới ngưỡng nạp thì không bị loại, nên nó ăn vào lượng đã rút.

    Đây là chế độ hỏng nguy hiểm nhất trong bốn cái: nó làm tiêu thụ TRÔNG NHỎ
    hơn thực tế, tức dự báo cạn muộn hơn thực tế. Đối chứng khí là thứ bắt được.
    """
    s = _series()
    bump = 1_000.0  # dưới refill_floor của bồn 60 m³ (1200 L) nên KHÔNG bị loại
    patched = [
        Sample(
            at=x.at,
            volume_l=(x.volume_l or 0) + (bump if i > N // 2 else 0),
            totalizer_nm3=x.totalizer_nm3,
        )
        for i, x in enumerate(s)
    ]
    r = estimate_dual_consumption(patched, capacity_l=CAP)
    assert r.liquid_net_l is not None
    # Lượng lỏng đo được bị hụt đúng bằng cú dâng.
    assert abs(r.liquid_net_l - (DROP_L * N - bump)) < 0.01
    assert r.ratio_nm3_per_m3 is not None
    assert r.ratio_nm3_per_m3 > VAPORIZATION_NM3_PER_M3


def test_bo_dem_reset_bi_dem_va_doan_do_bi_bo() -> None:
    """Reset về 0 đã thấy trong dữ liệu thật. Đoạn đó không dùng được cho CẢ hai số.

    Reset THẬT là về 0 rồi ĐẾM TIẾP, không phải về 0 rồi đứng — đứng là chế độ
    hỏng khác, đã có bài riêng ở trên. Mô phỏng sai chỗ này sẽ đòi hàm phán quyết
    "lệch" cho một cảm biến hoàn toàn lành.
    """
    s = _series()
    k = N // 2
    base_at_reset = s[k].totalizer_nm3 or 0.0
    patched = [
        Sample(
            at=x.at,
            volume_l=x.volume_l,
            totalizer_nm3=(
                (x.totalizer_nm3 or 0.0) - base_at_reset if i >= k else x.totalizer_nm3
            ),
        )
        for i, x in enumerate(s)
    ]
    r = estimate_dual_consumption(patched, capacity_l=CAP)
    assert r.counter_resets == 1
    assert r.segments == N - 1  # đúng một đoạn bị bỏ
    # Các đoạn còn lại vẫn nhất quán, nên phán quyết vẫn là khớp.
    assert r.verdict == "match"


def test_nap_that_bi_loai_khoi_ca_hai_so() -> None:
    """Đoạn chứa lần nạp thật bị loại y như ở estimate_consumption.

    Nếu chỉ loại ở phía lỏng mà vẫn cộng ở phía khí thì tỉ số lệch vì ĐỊNH NGHĨA,
    không vì dữ liệu — loại sai lệch đó là mục đích của bài này.
    """
    s = _series()
    k = N // 2
    patched = [
        Sample(
            at=x.at,
            volume_l=(x.volume_l or 0) + (20_000.0 if i >= k else 0.0),
            totalizer_nm3=x.totalizer_nm3,
        )
        for i, x in enumerate(s)
    ]
    r = estimate_dual_consumption(patched, capacity_l=CAP)
    assert r.refills_skipped == 1
    assert r.segments == N - 1
    assert r.verdict == "match"
    assert r.ratio_nm3_per_m3 is not None
    assert abs(r.ratio_nm3_per_m3 - VAPORIZATION_NM3_PER_M3) < 1.0


def test_nhieu_doi_xung_khong_lam_lech_ti_so() -> None:
    """Vì sao liquid_net_l không chặn nhiễu.

    Chặn nhiễu bỏ MỌI bước tụt nhỏ nhưng giữ MỌI bước tăng nhỏ — lệch một chiều,
    và với một tỉ số thì lệch một chiều là lệch thật. Tổng có dấu thì nhiễu triệt
    tiêu. Bài này thêm nhiễu ±50 L (dưới nhiễu sàn 60 L) so le và đòi tỉ số gần
    như không đổi.
    """
    base = estimate_dual_consumption(_series(), capacity_l=CAP)
    s = _series()
    noisy = [
        Sample(
            at=x.at,
            volume_l=(x.volume_l or 0) + (50.0 if i % 2 else -50.0),
            totalizer_nm3=x.totalizer_nm3,
        )
        for i, x in enumerate(s)
    ]
    r = estimate_dual_consumption(noisy, capacity_l=CAP)
    assert base.ratio_nm3_per_m3 is not None and r.ratio_nm3_per_m3 is not None
    assert abs(r.ratio_nm3_per_m3 - base.ratio_nm3_per_m3) < 5.0
    assert r.verdict == "match"


def test_cua_so_qua_ngan_khong_phat_ti_so() -> None:
    r = estimate_dual_consumption(_series(n=5), capacity_l=CAP)
    assert r.verdict == "insufficient"
    assert r.ratio_nm3_per_m3 is None
    assert "đoạn" in r.detail


def test_muc_khong_giam_thi_khong_lap_duoc_ti_so() -> None:
    s = [
        Sample(at=T0 + STEP * i, volume_l=54_000.0, totalizer_nm3=1_000_000.0 + i)
        for i in range(N + 1)
    ]
    r = estimate_dual_consumption(s, capacity_l=CAP)
    assert r.verdict == "insufficient"
    assert r.ratio_nm3_per_m3 is None
    assert "không giảm" in r.detail


def test_nguon_khong_co_dong_ho_khi_khong_bi_anh_huong() -> None:
    """Bồn Xingke không có totalizer -> hàm này im lặng, không phát số bịa."""
    s = [Sample(at=T0 + STEP * i, volume_l=54_000.0 - DROP_L * i) for i in range(N + 1)]
    r = estimate_dual_consumption(s, capacity_l=CAP)
    assert r.segments == 0
    assert r.liquid_net_l is None
    assert r.gas_nm3 is None
    assert r.verdict == "insufficient"


def test_dai_chap_nhan_rong_hon_bien_do_tung_doan_do_duoc() -> None:
    """Dải phải phủ được biên độ 382…1249 đã đo, nếu không nó báo oan.

    Đo trên 61 đoạn thật: tổng 513,9 Nm³/m³ nhưng từng đoạn trải 382…1249. Một
    dải hẹp quanh 514 sẽ đỏ liên tục trên dữ liệu lành.
    """
    assert RATIO_LO < 382
    assert RATIO_HI > 1249 * 0.8  # 1249 là cực trị MỘT đoạn, không phải tổng
    assert RATIO_LO < VAPORIZATION_NM3_PER_M3 < RATIO_HI
