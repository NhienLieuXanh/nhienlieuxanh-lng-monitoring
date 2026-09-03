"""Test báo cáo bản in. Không cần DB: thay sáu phụ thuộc bằng dữ liệu giả.

Vì sao đáng viết: hàm sinh báo cáo dựng một tài liệu vài trăm dòng bằng f-string
và gọi cả tầng dự báo lẫn tầng phân tích. Không có test này thì mọi lỗi trong đó
chỉ lộ ra khi có người bấm nút trên production — và đây là tài liệu mang đi trình ký.

Bốn tính chất được khoá lại ở đây, mỗi cái là một cách hỏng đã lường trước:

1. **Không rò tên vendor, không có chữ Trung.** Cùng ràng buộc với các endpoint
   JSON, nhưng ``test_no_vendor_leak`` chỉ quét JSON nên không phủ được HTML.
2. **Tên bồn được escape.** Tên do người dùng tự đặt và đi thẳng vào HTML.
3. **Kỳ báo cáo neo vào lần đo cuối**, kèm câu giải thích khi số liệu đã cũ.
4. **Sức khoẻ thiết bị tính theo hiện tại**, không theo mốc cuối kỳ — nếu lấy mốc
   cuối kỳ thì một thiết bị chết hàng tháng vẫn được báo là im 0 ngày.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.api.routers import report as rp

UTC = ZoneInfo("UTC")
VN = ZoneInfo("Asia/Ho_Chi_Minh")
CAP = Decimal("10425")

#: Lần đo cuối cách "bây giờ" 40 ngày — tái hiện đúng tình trạng thật của đội bồn
#: hiện tại, nơi mặc định "30 ngày tính từ hôm nay" cho ra báo cáo trống.
STALE_DAYS = 40


class _Cfg:
    """Cấu hình tối thiểu mà hàm báo cáo đọc tới. Giá trị lấy từ mặc định thật."""

    tzinfo = VN
    app_tz = "Asia/Ho_Chi_Minh"
    online_stale_minutes = 90
    forecast_window_days = 30
    forecast_reserve_percent = 15.0
    forecast_lead_time_days = 2.0
    forecast_service_level = 0.95
    forecast_max_reading_age_hours = 48.0
    # Kiểu phải KHỚP app/config.py: tất cả là float. Dùng Decimal ở đây sẽ làm
    # test nổ TypeError trong hold_time và che mất lỗi thật của báo cáo.
    lng_relief_pressure_mpa = 0.8
    lng_max_fill_percent = 90.0
    alert_low_battery_v = 3.40
    alert_low_signal_percent = 10.0


def _terminal(psn: str, name: str | None, last_seen: datetime | None):
    return SimpleNamespace(psn=psn, name=name, capacity_l=CAP, last_seen_at=last_seen)


def _latest(at: datetime, vol: str, pres: str):
    return SimpleNamespace(
        sampled_at=at,
        volume_l=Decimal(vol),
        pressure_mpa=Decimal(pres),
        temperature_c=Decimal("-158.3"),
        volume_percent=Decimal("0.5"),
    )


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    """Nối dây một hệ hai bồn: một bồn tụt mức đều, một bồn chưa từng gửi số liệu."""
    now = datetime(2026, 8, 24, 6, 0, tzinfo=UTC)
    end = now - timedelta(days=STALE_DAYS)

    # Bồn A: 720 điểm cách nhau 1 giờ (30 ngày), mức tụt đều, kèm một lần nạp ở
    # giữa để đường tiêu thụ có cả đoạn nạp mà thuật toán phải loại ra.
    #
    # Hai tham số dưới đây phải khớp NGƯỠNG THẬT của domain, không được đặt tuỳ ý:
    #
    # * Nhịp <= forecast.MAX_GAP (3 giờ). Thưa hơn thì mọi cặp điểm bị coi là
    #   khoảng trống, active_days = 0, và mọi cột tiêu thụ ra rỗng.
    # * Bước sụt > forecast.noise_floor_l (0,1% dung tích ~ 10,4 L). Nhỏ hơn thì
    #   từng bước bị bỏ như nhiễu cảm biến, drawdown = 0, và tiêu thụ vẫn ra rỗng.
    #
    # Sai một trong hai thì test vẫn xanh mà không kiểm được gì — đúng loại fixture
    # tự làm mình vô dụng.
    N, STEP_H, DROP_L = 360, 2, 47.0
    a_series: list[tuple[datetime, Decimal | None, Decimal | None]] = []
    vol = 9000.0
    for i in range(N):
        at = end - timedelta(hours=STEP_H * (N - 1 - i))
        if i == N // 2:
            vol = 9000.0  # xe bồn tới
        a_series.append((at, Decimal(str(round(vol, 1))), Decimal("0.42")))
        vol -= DROP_L

    # Pin suy ~0,006 V/ngày: đủ để tầng phân tích thấy độ dốc, không dốc đến mức
    # phi thực tế khiến mọi ngưỡng đều bị vượt và test không phân biệt được gì.
    a_health = [
        (at, Decimal("3.58") - Decimal("0.0005") * i, Decimal("18"))
        for i, (at, _, _) in enumerate(a_series)
    ]

    terms = [
        # Dấu ngoặc nhọn trong tên là CỐ Ý: nó phải ra khỏi hàm dưới dạng đã escape.
        _terminal("2604200016", 'Bồn A <script>alert("x")</script>', end),
        _terminal("2605090007", None, None),
    ]
    latest = {"2604200016": _latest(end, str(a_series[-1][1]), "0.42")}

    monkeypatch.setattr(rp.term_repo, "list_all", lambda s: terms)
    monkeypatch.setattr(
        rp.term_repo,
        "get_by_psn",
        lambda s, psn: next((t for t in terms if t.psn == psn), None),
    )
    monkeypatch.setattr(rp.tel_repo, "latest_many", lambda s, psns: latest)
    monkeypatch.setattr(
        rp.tel_repo,
        "series",
        lambda s, psn, a, b, **kw: a_series if psn == "2604200016" else [],
    )
    monkeypatch.setattr(
        rp.tel_repo,
        "health_series",
        lambda s, psn, a, b, **kw: a_health if psn == "2604200016" else [],
    )
    monkeypatch.setattr(rp, "load_config", lambda s, st: _Cfg())
    monkeypatch.setattr(
        rp.notifier,
        "collect_notices",
        lambda s, cfg, now: [
            SimpleNamespace(
                psn="2604200016",
                name="Bồn A",
                code="LOW_VOLUME",
                severity="critical",
                message="Mức thấp: 26.6% dưới ngưỡng 15%",
            ),
            SimpleNamespace(
                psn="2605090007",
                name=None,
                code="OFFLINE",
                severity="warning",
                message="không có dữ liệu trong 40 ngày",
            ),
        ],
    )
    return SimpleNamespace(now=now, end=end, terms=terms)


def _render(psn: str | None = None, window_days: int = 30) -> str:
    return rp.export_report(
        session=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
        user="nguyen-the.son",
        psn=psn,
        window_days=window_days,
    ).body.decode("utf-8")


class TestDungKhuon:
    def test_du_bay_muc_va_khoi_chu_ky(self, wired) -> None:
        html = _render()
        for heading in (
            "Tóm tắt điều hành",
            "Hiện trạng từng bồn",
            "Tiêu thụ và dự báo",
            "Chất lượng dữ liệu và sức khoẻ thiết bị",
            "Nhật ký nạp trong kỳ",
            "Cảnh báo đang mở",
            "Ghi chú phương pháp",
        ):
            assert heading in html
        for role in ("Người lập", "Người kiểm tra", "Người phê duyệt"):
            assert role in html
        assert "nguyen-the.son" in html

    def test_co_css_in_a4(self, wired) -> None:
        html = _render()
        assert "size:A4" in html
        assert "display:table-header-group" in html  # thead lặp lại mỗi trang

    def test_ma_bao_cao_va_tieu_de(self, wired) -> None:
        html = _render()
        assert re.search(r"BC-LNG-\d{8}-\d{4}", html)
        assert "<title>" in html and "Báo cáo giám sát bồn LNG" in html


class TestTrungThuc:
    def test_neo_ky_vao_lan_do_cuoi_va_noi_ra(self, wired) -> None:
        """Kỳ báo cáo phải kết ở lần đo cuối, và phải giải thích vì sao."""
        html = _render()
        assert "Kỳ báo cáo kết ở lần đo cuối" in html
        # Ngày kết kỳ là ngày của lần đo cuối theo giờ Việt Nam, không phải hôm nay.
        assert wired.end.astimezone(VN).strftime("%d/%m/%Y") in html

    def test_suc_khoe_thiet_bi_tinh_theo_hien_tai(self, wired) -> None:
        """Thiết bị im 40 ngày không được báo là rủi ro thấp.

        Đây là bẫy đã sửa: nếu truyền mốc cuối kỳ vào ``assess_device_health`` thì
        "đã im bao nhiêu ngày" luôn ra 0 và mọi thiết bị chết đều trông bình thường.
        """
        html = _render()
        health = html.split("sức khoẻ thiết bị")[1].split("Nhật ký nạp")[0]
        assert 'pill ok">thấp' not in health

    def test_o_trong_la_khong_do_duoc_khong_phai_bang_khong(self, wired) -> None:
        """Bồn chưa từng gửi số liệu ra dấu gạch, không ra số 0."""
        html = _render()
        row = next(ln for ln in html.split("<tr>") if "2605090007" in ln)
        assert "—" in row
        assert ">0,000<" not in row

    def test_khong_in_ty_le_vo_nghia_khi_chua_suy_duoc_nhip_do(self, wired) -> None:
        """Bồn chưa đủ hai lần đo phải ra "0 / —", không ra "0 / 0".

        Domain đặt expected_samples = 0 để nói "chưa suy được nhịp đo". In thẳng
        con số đó lên giấy biến nó thành một tỉ lệ mà người đọc phải tự đoán nghĩa.
        """
        html = _render()
        health = html.split("sức khoẻ thiết bị")[1].split("Nhật ký nạp")[0]
        assert "/ 0<" not in health
        assert "/ —" in health

    def test_nhan_du_lieu_cu(self, wired) -> None:
        html = _render()
        assert "CŨ" in html

    def test_do_tin_cay_va_tieu_thu_khong_mau_thuan(self, wired) -> None:
        """Bồn có 360 lần đo tốt phải ra một con số tiêu thụ, không ra ô trống.

        Bảo vệ chống một lớp lỗi đã gặp: cột "Độ phủ" báo 100% trong khi cột
        "Tiêu thụ/ngày" báo "—" ngay cùng một hàng. Hai ô cạnh nhau nói ngược nhau
        là thứ khiến người đọc mất tin vào cả tờ báo cáo.
        """
        fc_sec = _render().split("Tiêu thụ và dự báo")[1].split("Chất lượng dữ liệu")[0]
        row = next(r for r in fc_sec.split("<tr>") if "2604200016" in r)
        assert ">none<" not in row  # độ tin cậy phải có giá trị thật
        assert row.count("—") < 3  # phần lớn cột dự báo phải có số

    def test_muc_gap_dung_tu_ngu_cua_dashboard(self, wired) -> None:
        """Nhãn mức gấp phải là bốn giá trị thật của Urgency, không phải chữ thô.

        Bốn giá trị là now/soon/ok/unknown. Trước đây bảng tra dùng nhãn tự đặt nên
        báo cáo in ra chữ "unknown" và ô "Bồn cần đặt gấp" luôn bằng 0.
        """
        html = _render()
        assert ">unknown<" not in html
        assert any(lbl in html for lbl in rp._URG_LABEL.values())


class TestAnToan:
    def test_ten_bon_duoc_escape(self, wired) -> None:
        """Tên bồn do người dùng đặt; nó không được chạy như mã trong trang."""
        html = _render()
        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html

    def test_khong_ro_ten_vendor(self, wired) -> None:
        html = _render()
        low = html.lower()
        for leak in ("xingke", "xk-iot", "raw_payload", "backstage"):
            assert leak not in low

    def test_khong_co_chu_trung(self, wired) -> None:
        """Cùng ràng buộc với các endpoint JSON, nhưng test kia chỉ quét JSON."""
        cjk = [c for c in _render() if "一" <= c <= "鿿"]
        assert cjk == []

    def test_khong_luu_vao_may_tim_kiem(self, wired) -> None:
        resp = rp.export_report(
            session=None,  # type: ignore[arg-type]
            settings=None,  # type: ignore[arg-type]
            user="u",
            psn=None,
            window_days=30,
        )
        assert resp.headers["x-robots-tag"] == "noindex, nofollow"


class TestPhamVi:
    def test_mot_bon_thi_chi_bao_cao_bon_do(self, wired) -> None:
        html = _render(psn="2604200016")
        assert "Bồn 2604200016" in html
        # Cảnh báo của bồn khác phải bị lọc khỏi mục Cảnh báo.
        alerts = html.split("Cảnh báo đang mở")[1].split("Ghi chú phương pháp")[0]
        assert "2605090007" not in alerts

    def test_psn_khong_ton_tai_ra_404(self, wired) -> None:
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei:
            _render(psn="0000000000")
        assert ei.value.status_code == 404


class TestDinhDangSo:
    def test_dau_phay_thap_phan_va_o_trong(self) -> None:
        assert rp._n(1.5, 1) == "1,5"
        assert rp._n(None) == "—"
        assert rp._m3(10425.0, 3) == "10,425"
        assert rp._pct(26.55, 1) == "26,6%"

    def test_thieu_file_logo_thi_lui_ve_chu_khong_ra_anh_vo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rp, "_logo_data_uri", lambda: None)
        got = rp._brand_block()
        assert "<img" not in got
        assert "GAS" in got and "Nhiên Liệu Xanh" in got
