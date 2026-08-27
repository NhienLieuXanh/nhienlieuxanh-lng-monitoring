"""Rule status và alert. Hàm thuần — không mock clock, không cần DB."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.domain.alerts import (
    AlertCode,
    AlertThresholds,
    Severity,
    TerminalSnapshot,
    elapsed_vi,
    evaluate,
    fill_percent,
)
from app.domain.contracts import TerminalStatus
from app.domain.status import derive_status

UTC = ZoneInfo("UTC")
NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
STALE = timedelta(minutes=90)

TH = AlertThresholds(
    stale_after=STALE,
    low_volume_percent=Decimal("15"),
    low_battery_v=Decimal("3.40"),
    low_signal_percent=Decimal("10"),
)


class TestDeriveStatus:
    """Kiểm đúng biên. `now` là tham số nên không cần freeze clock."""

    def test_never_seen_is_offline(self):
        assert derive_status(None, NOW, STALE) is TerminalStatus.OFFLINE

    def test_exactly_at_threshold_is_online(self):
        assert derive_status(NOW - STALE, NOW, STALE) is TerminalStatus.ONLINE

    def test_one_second_past_threshold_is_offline(self):
        last = NOW - STALE - timedelta(seconds=1)
        assert derive_status(last, NOW, STALE) is TerminalStatus.OFFLINE

    def test_one_second_inside_threshold_is_online(self):
        last = NOW - STALE + timedelta(seconds=1)
        assert derive_status(last, NOW, STALE) is TerminalStatus.ONLINE

    def test_real_devices_are_offline(self):
        """Cả hai thiết bị thật stale hàng tháng — đây là trạng thái mặc định."""
        for last in (
            datetime(2026, 7, 23, 8, 3, 29, tzinfo=UTC),
            datetime(2026, 6, 2, 14, 17, 3, tzinfo=UTC),
        ):
            assert derive_status(last, NOW, STALE) is TerminalStatus.OFFLINE


class TestFillPercent:
    def test_matches_vendor_percent_on_real_data(self):
        """Đối chứng: 61/10425*100 = 0.5851, vendor gửi 0.59. Khớp tới 2 chữ số."""
        assert round(fill_percent(Decimal(61), Decimal(10425)), 2) == Decimal("0.59")
        assert round(fill_percent(Decimal(30), Decimal(10425)), 2) == Decimal("0.29")

    def test_none_when_not_computable(self):
        assert fill_percent(None, Decimal(10425)) is None
        assert fill_percent(Decimal(61), None) is None
        assert fill_percent(Decimal(61), Decimal(0)) is None


class TestAlerts:
    def test_real_device_alerts_low_volume_and_offline(self):
        """Bồn thật 0.59% đầy + offline hàng tháng => đúng ra PHẢI có hai alert.

        Đây chính là điều dashboard cũ che mất khi vẽ 0.59% thành "59%".
        """
        snap = TerminalSnapshot(
            psn="2604200016",
            last_seen_at=datetime(2026, 7, 23, 8, 3, 29, tzinfo=UTC),
            volume_percent=Decimal("0.59"),
            fill_percent=Decimal("0.59"),
            battery_v=Decimal("3.60"),
            signal_percent=Decimal("20"),
        )
        codes = {a.code for a in evaluate(snap, TH, NOW)}
        assert AlertCode.OFFLINE in codes
        assert AlertCode.LOW_VOLUME in codes

    def test_healthy_battery_does_not_alert(self):
        """3.6 V là bình thường cho pin lithium primary 3.6 V còn tốt.

        Ngưỡng 3.5 V ngây thơ sẽ báo động trên thiết bị lành; 3.40 thì không.
        """
        snap = TerminalSnapshot(
            psn="X", last_seen_at=NOW, fill_percent=Decimal("50"),
            battery_v=Decimal("3.60"), signal_percent=Decimal("50"),
        )
        assert evaluate(snap, TH, NOW) == []

    def test_low_battery_alerts_below_threshold(self):
        snap = TerminalSnapshot(
            psn="X", last_seen_at=NOW, fill_percent=Decimal("50"),
            battery_v=Decimal("3.39"), signal_percent=Decimal("50"),
        )
        codes = {a.code for a in evaluate(snap, TH, NOW)}
        assert codes == {AlertCode.LOW_BATTERY}

    def test_percent_mismatch_detected(self):
        """Nếu ai đó hiểu volume_percent là phân số rồi nhân 100, hai số lệch xa.

        Không CHECK constraint nào bắt được lỗi thang này (0.59 hợp lệ ở cả hai) —
        chỉ sự KHÔNG KHỚP giữa hai số tính độc lập mới phát hiện được.
        """
        snap = TerminalSnapshot(
            psn="X", last_seen_at=NOW,
            volume_percent=Decimal("59"),     # bị nhân 100
            fill_percent=Decimal("0.59"),     # server tự tính
        )
        codes = {a.code for a in evaluate(snap, TH, NOW)}
        assert AlertCode.PERCENT_MISMATCH in codes

    def test_no_mismatch_alert_on_real_agreeing_data(self):
        snap = TerminalSnapshot(
            psn="X", last_seen_at=NOW,
            volume_percent=Decimal("0.59"), fill_percent=Decimal("0.5851"),
        )
        codes = {a.code for a in evaluate(snap, TH, NOW)}
        assert AlertCode.PERCENT_MISMATCH not in codes

    def test_missing_values_do_not_alert(self):
        """None nghĩa là "vendor không đo", không phải "bằng 0"."""
        snap = TerminalSnapshot(psn="X", last_seen_at=NOW)
        assert evaluate(snap, TH, NOW) == []

    def test_critical_severity_for_low_volume(self):
        snap = TerminalSnapshot(
            psn="X", last_seen_at=NOW, fill_percent=Decimal("1")
        )
        alerts = evaluate(snap, TH, NOW)
        assert [a.severity for a in alerts] == [Severity.CRITICAL]


class TestSoDoQuaCu:
    """Lần đọc quá cũ thì không được kết luận như số hiện tại.

    Tình huống thật trên production: bồn 2605090007 im 85,6 ngày, API phát ra
    ``LOW_VOLUME critical`` cạnh ``OFFLINE warning "không có dữ liệu trong 85 ngày"``.
    Hai dòng đó mâu thuẫn nhau về cùng một dữ liệu.
    """

    #: 85,6 ngày im — đúng bằng bồn 2605090007 trên production.
    OLD = NOW - timedelta(days=85, hours=14)

    def _snap(self, last_seen):  # type: ignore[no-untyped-def]
        return TerminalSnapshot(
            psn="2605090007",
            last_seen_at=last_seen,
            volume_percent=Decimal("0.29"),
            fill_percent=Decimal("0.29"),
            battery_v=Decimal("3.39"),      # dưới ngưỡng -> lẽ ra có LOW_BATTERY
            signal_percent=Decimal("5"),    # dưới ngưỡng -> lẽ ra có WEAK_SIGNAL
        )

    def test_low_volume_ha_xuong_warning_va_ghi_ro_tuoi(self) -> None:
        alerts = evaluate(self._snap(self.OLD), TH, NOW)
        lv = next(a for a in alerts if a.code is AlertCode.LOW_VOLUME)
        # Hạ cấp, KHÔNG cắt: "lần cuối nhìn thấy thì đã cạn" vẫn là thông tin thật.
        assert lv.severity is Severity.WARNING
        # Và phải nói ra tuổi của số đo, nếu không người đọc vẫn hiểu là số hiện tại.
        assert "85 ngày trước" in lv.message
        assert "ngoại tuyến" in lv.message

    def test_canh_bao_ve_thiet_bi_bi_bo(self) -> None:
        """OFFLINE đã mang đúng một hành động; ba dòng nữa chỉ là nhiễu."""
        codes = {a.code for a in evaluate(self._snap(self.OLD), TH, NOW)}
        assert AlertCode.OFFLINE in codes
        assert AlertCode.LOW_VOLUME in codes
        assert AlertCode.LOW_BATTERY not in codes
        assert AlertCode.WEAK_SIGNAL not in codes
        assert AlertCode.PERCENT_MISMATCH not in codes

    def test_so_do_moi_thi_khong_doi_gi(self) -> None:
        """Chốt chặn chỉ được kích hoạt khi số đo CŨ — không làm hỏng ca bình thường."""
        alerts = evaluate(self._snap(NOW), TH, NOW)
        codes = {a.code for a in alerts}
        assert codes == {
            AlertCode.LOW_VOLUME,
            AlertCode.LOW_BATTERY,
            AlertCode.WEAK_SIGNAL,
        }
        lv = next(a for a in alerts if a.code is AlertCode.LOW_VOLUME)
        assert lv.severity is Severity.CRITICAL
        assert "trước" not in lv.message

    def test_ngay_qua_nguong_la_cu(self) -> None:
        """Ngưỡng là max_reading_age (24 giờ), KHÔNG phải stale_after (90 phút).

        Hai ngưỡng khác nhau có chủ ý: 2 giờ mất tín hiệu thì thiết bị đã "ngoại
        tuyến" nhưng thể tích đo 2 giờ trước vẫn dùng được để cảnh báo; 25 giờ thì
        không.
        """
        vua_du = evaluate(self._snap(NOW - timedelta(hours=23, minutes=59)), TH, NOW)
        assert next(a for a in vua_du if a.code is AlertCode.LOW_VOLUME).severity is Severity.CRITICAL
        assert AlertCode.LOW_BATTERY in {a.code for a in vua_du}

        qua_nguong = evaluate(self._snap(NOW - timedelta(hours=24, minutes=1)), TH, NOW)
        assert next(a for a in qua_nguong if a.code is AlertCode.LOW_VOLUME).severity is Severity.WARNING
        assert AlertCode.LOW_BATTERY not in {a.code for a in qua_nguong}

    def test_chua_tung_co_so_do(self) -> None:
        snap = TerminalSnapshot(psn="X", last_seen_at=None, fill_percent=Decimal("1"))
        alerts = evaluate(snap, TH, NOW)
        lv = next(a for a in alerts if a.code is AlertCode.LOW_VOLUME)
        assert lv.severity is Severity.WARNING
        assert "chưa từng có số đo" in lv.message


class TestElapsedVi:
    """Chuỗi thời lượng trong cảnh báo. Dùng CHUNG thang với fmtAgo() ở dashboard."""

    def test_escalates_to_days(self):
        """Lỗi thật trên production: 80 ngày mất tín hiệu in ra "1935 giờ"."""
        assert elapsed_vi(timedelta(days=80, hours=15)) == "80 ngày"
        assert elapsed_vi(timedelta(days=30)) == "30 ngày"

    def test_boundary_at_48_hours(self):
        """Khớp fmtAgo: dưới 48 giờ còn hiện giờ, từ 48 trở lên đổi sang ngày."""
        assert elapsed_vi(timedelta(hours=47, minutes=59)) == "47 giờ"
        assert elapsed_vi(timedelta(hours=48)) == "2 ngày"

    def test_boundary_at_60_minutes(self):
        assert elapsed_vi(timedelta(minutes=59)) == "59 phút"
        assert elapsed_vi(timedelta(minutes=60)) == "1 giờ"

    def test_rounds_down_never_overstates(self):
        """2.8 giờ là "2 giờ", không phải "3 giờ".

        Làm tròn gần nhất khiến dashboard và cảnh báo hiện hai con số khác nhau cho
        cùng một sự việc, và khiến chuỗi nói QUÁ thời gian mất tín hiệu — chuỗi này
        đi vào email và nhật ký kiểm toán nên không được nói quá.
        """
        assert elapsed_vi(timedelta(hours=2, minutes=48)) == "2 giờ"
        assert elapsed_vi(timedelta(seconds=59)) == "0 phút"

    def test_used_by_offline_alert(self):
        snap = TerminalSnapshot(psn="X", last_seen_at=NOW - timedelta(days=80))
        msg = next(
            a.message for a in evaluate(snap, TH, NOW) if a.code is AlertCode.OFFLINE
        )
        assert "80 ngày" in msg
        assert "giờ" not in msg
