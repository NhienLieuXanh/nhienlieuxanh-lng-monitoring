"""Phân tích: thống kê bền, chất lượng dữ liệu, sức khoẻ thiết bị, bất thường.

Hai loại test được coi trọng nhất ở đây:

1. **Giá trị tính tay được.** Một độ dốc, một số ngày tới ngưỡng — nếu chỉ assert
   "không phải None" thì công thức sai vẫn xanh.
2. **Không kêu oan.** Một máy phát hiện bất thường kêu trên chuỗi sạch là một máy
   không ai còn nhìn. Có test riêng cho chuỗi sạch, và nó phải trả rỗng.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.domain import analytics as A
from app.domain.forecast import Sample

UTC = ZoneInfo("UTC")
T0 = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
CAP = 10425.0


def _series(
    *, start_l: float, per_day_l: float, hours: int, step_min: int = 30
) -> list[Sample]:
    """Chuỗi rút xuống đều, mỗi ``step_min`` phút một mẫu."""
    n = int(hours * 60 / step_min) + 1
    return [
        Sample(
            at=T0 + timedelta(minutes=i * step_min),
            volume_l=start_l - per_day_l * (i * step_min / 1440.0),
            pressure_mpa=0.1,
        )
        for i in range(n)
    ]


def _health(
    *, hours: int, step_min: int = 30, v0: float, mv_per_day: float, signal: float = 20.0
) -> list[A.HealthSample]:
    n = int(hours * 60 / step_min) + 1
    return [
        A.HealthSample(
            at=T0 + timedelta(minutes=i * step_min),
            battery_v=v0 + (mv_per_day / 1000.0) * (i * step_min / 1440.0),
            signal_percent=signal,
        )
        for i in range(n)
    ]


# --------------------------------------------------------------------------- #
# Thống kê bền
# --------------------------------------------------------------------------- #


class TestRobustStats:
    def test_theil_sen_recovers_known_slope(self):
        pts = [(float(i), 100.0 - 2.5 * i) for i in range(40)]
        fit = A.theil_sen(pts)
        assert fit is not None
        assert fit.slope_per_day == pytest.approx(-2.5, abs=1e-9)
        assert fit.intercept == pytest.approx(100.0, abs=1e-9)

    def test_theil_sen_survives_outlier_that_breaks_least_squares(self):
        """Lý do chọn Theil-Sen thay bình phương tối thiểu, đo bằng số.

        Một điểm rác (vendor gửi 0 thay cho null) kéo bình phương tối thiểu lệch hẳn,
        còn trung vị của độ dốc từng cặp gần như không nhúc nhích.
        """
        pts = [(float(i), 100.0 - 2.5 * i) for i in range(40)]
        pts[20] = (20.0, 0.0)  # điểm rác

        fit = A.theil_sen(pts)
        assert fit is not None
        assert fit.slope_per_day == pytest.approx(-2.5, abs=0.05)

        n = len(pts)
        mx = sum(x for x, _ in pts) / n
        my = sum(y for _, y in pts) / n
        ols = sum((x - mx) * (y - my) for x, y in pts) / sum(
            (x - mx) ** 2 for x, _ in pts
        )
        assert abs(ols - (-2.5)) > abs(fit.slope_per_day - (-2.5)) * 5

    def test_theil_sen_needs_three_points(self):
        assert A.theil_sen([(0.0, 1.0), (1.0, 2.0)]) is None

    def test_theil_sen_is_deterministic_when_thinned(self):
        """Lấy mẫu theo bước đều, không ngẫu nhiên: cùng vào phải cùng ra."""
        pts = [(float(i), 500.0 - 0.3 * i) for i in range(A.THEIL_SEN_MAX_POINTS * 3)]
        a, b = A.theil_sen(pts), A.theil_sen(pts)
        assert a is not None and b is not None
        assert a.slope_per_day == b.slope_per_day
        assert a.n == A.THEIL_SEN_MAX_POINTS

    def test_mad_ignores_a_wild_value(self):
        """Trung vị lệch = 0 trong khi stdev khổng lồ — đó là điểm của MAD."""
        assert A.mad([10.0, 10.0, 10.0, 10.0, 1000.0]) == 0.0


# --------------------------------------------------------------------------- #
# Chất lượng dữ liệu
# --------------------------------------------------------------------------- #


class TestQuality:
    def test_full_coverage_is_high_grade(self):
        s = _series(start_l=9000.0, per_day_l=500.0, hours=24 * 7)
        q = A.assess_quality(s, now=s[-1].at, window_days=7.0)
        assert q.grade == "cao"
        assert q.coverage == pytest.approx(1.0, abs=1e-9)
        assert q.cadence_minutes == pytest.approx(30.0, abs=1e-9)
        assert q.gaps == 0

    def test_missing_samples_lower_the_coverage(self):
        s = _series(start_l=9000.0, per_day_l=500.0, hours=24 * 7)
        thin = [x for i, x in enumerate(s) if i % 4 != 0]
        q = A.assess_quality(thin, now=s[-1].at, window_days=7.0)
        assert q.coverage < 0.85
        assert q.grade in ("thấp", "trung bình")

    def test_flatline_forces_low_grade_even_at_full_coverage(self):
        """Phủ 100% mà cảm biến kẹt thì tệ hơn phủ 60% trung thực."""
        s = _series(start_l=9000.0, per_day_l=500.0, hours=24 * 3)
        stuck = [
            Sample(at=x.at, volume_l=8000.0 if 20 <= i <= 40 else x.volume_l)
            for i, x in enumerate(s)
        ]
        q = A.assess_quality(stuck, now=s[-1].at, window_days=3.0)
        assert q.flatline_runs >= 1
        assert q.longest_flatline_hours is not None
        assert q.grade == "thấp"

    def test_too_few_samples_is_unusable_not_optimistic(self):
        s = _series(start_l=9000.0, per_day_l=500.0, hours=2)
        q = A.assess_quality(s, now=s[-1].at, window_days=1.0)
        assert q.samples < A.MIN_TREND_SAMPLES
        assert q.grade == "không dùng được"

    def test_single_sample_does_not_crash(self):
        q = A.assess_quality([Sample(at=T0, volume_l=100.0)], now=T0, window_days=1.0)
        assert q.grade == "không dùng được"
        assert q.coverage == 0.0
        assert q.cadence_minutes is None

    def test_coverage_is_measured_against_the_window_not_the_data(self):
        """Thiết bị chỉ báo 5/30 ngày phải hiện ~17%, KHÔNG phải 100%.

        Lỗi thật đã bắt được khi chạy endpoint trên dữ liệu thật: mẫu số là khoảng dữ
        liệu quan sát được nên một thiết bị chết từ lâu vẫn báo "độ phủ 100%" — đúng
        con số dối mà module này ra đời để chặn.
        """
        s = _series(start_l=9000.0, per_day_l=500.0, hours=24 * 5)
        q = A.assess_quality(s, now=s[-1].at, window_days=30.0)
        assert q.expected_samples == 30 * 48  # 30 ngày ở nhịp 30 phút
        assert q.coverage == pytest.approx(5.0 / 30.0, abs=0.02)
        assert q.grade == "không dùng được"
        assert any("chỉ trải" in r for r in q.reasons)

    def test_long_gap_is_counted_and_measured(self):
        a = _series(start_l=9000.0, per_day_l=500.0, hours=24)
        b = [
            Sample(at=x.at + timedelta(days=3), volume_l=x.volume_l)
            for x in _series(start_l=8000.0, per_day_l=500.0, hours=24)
        ]
        # `a` kết ở T0+24h, `b` mở ở T0+72h -> khoảng trống là 48 giờ, không phải 72.
        q = A.assess_quality(a + b, now=b[-1].at, window_days=5.0)
        assert q.gaps == 1
        assert q.longest_gap_hours == pytest.approx(48.0, abs=0.6)


# --------------------------------------------------------------------------- #
# Sức khoẻ thiết bị
# --------------------------------------------------------------------------- #


class TestDeviceHealth:
    def test_battery_slope_and_days_to_thresholds(self):
        """Pin bắt đầu 3.60 V tụt 5 mV/ngày; sau 20 ngày còn 3.50 V.

        Từ 3.50 V: tới ngưỡng cảnh báo 3.40 V là 20 ngày, tới 3.00 V là 100 ngày.
        """
        h = _health(hours=24 * 20, v0=3.60, mv_per_day=-5.0)
        r = A.assess_device_health(h, psn="X", now=h[-1].at)
        assert r.battery.volts_per_day == pytest.approx(-0.005, abs=1e-4)
        assert r.battery.current_v == pytest.approx(3.50, abs=1e-6)
        assert r.battery.days_to_warn == pytest.approx(20.0, abs=0.5)
        assert r.battery.days_to_dead == pytest.approx(100.0, abs=2.0)
        assert r.battery.confidence == "cao"
        assert r.likely_cause == "Pin cạn"

    def test_rising_battery_is_not_extrapolated(self):
        """Độ dốc dương là nhiễu hoặc vừa thay pin — không ngoại suy thành 900 ngày."""
        h = _health(hours=24 * 20, v0=3.40, mv_per_day=+3.0)
        r = A.assess_device_health(h, psn="X", now=h[-1].at)
        assert r.battery.volts_per_day is not None
        assert r.battery.volts_per_day > 0
        assert r.battery.days_to_warn is None
        assert r.battery.days_to_dead is None

    def test_too_few_samples_says_so_instead_of_guessing(self):
        h = _health(hours=2, v0=3.60, mv_per_day=-5.0)
        r = A.assess_device_health(h, psn="X", now=h[-1].at)
        assert r.risk == "chưa đủ dữ liệu"
        assert r.days_to_failure is None
        assert r.battery.confidence == "chưa đủ dữ liệu"

    def test_brief_silence_is_not_declared_dead(self):
        """Im 5 giờ chưa phải lý do để ai lái xe ra hiện trường.

        Ngưỡng cũ là 4 lần cadence = 2 giờ, nên một thiết bị trượt vài lần upload đã
        bị tuyên "đã ngừng báo, còn 0 ngày". Lỗi bắt được khi đọc output thật.
        """
        h = _health(hours=24 * 20, v0=3.65, mv_per_day=-0.05, signal=85.0)
        r = A.assess_device_health(h, psn="X", now=h[-1].at + timedelta(hours=5))
        assert r.silent_days is not None and r.silent_days < A.SILENT_DEAD_DAYS
        assert r.days_to_failure != 0.0
        assert r.likely_cause is None or "ngừng báo" not in r.likely_cause

    def test_silent_device_is_fact_not_forecast(self):
        """Đã im 84 ngày thì rủi ro là hiện trạng, và số ngày còn lại bằng 0."""
        h = _health(hours=24 * 20, v0=3.60, mv_per_day=-5.0)
        r = A.assess_device_health(h, psn="X", now=h[-1].at + timedelta(days=84))
        assert r.risk == "cao"
        assert r.days_to_failure == 0.0
        assert r.silent_days == pytest.approx(84.0, abs=0.01)
        assert r.likely_cause is not None and "ngừng báo" in r.likely_cause

    def test_weak_signal_is_reported_as_ratio_below_floor(self):
        h = _health(hours=24 * 10, v0=3.60, mv_per_day=-0.1, signal=5.0)
        r = A.assess_device_health(h, psn="X", now=h[-1].at)
        assert r.signal.below_floor_ratio == pytest.approx(1.0, abs=1e-9)
        assert r.risk in ("trung bình", "cao")

    def test_healthy_device_is_low_risk(self):
        h = _health(hours=24 * 20, v0=3.65, mv_per_day=-0.05, signal=85.0)
        r = A.assess_device_health(h, psn="X", now=h[-1].at)
        assert r.delivery_ratio == pytest.approx(1.0, abs=1e-9)
        assert r.risk == "thấp"

    def test_declining_delivery_predicts_failure(self):
        """Mẫu rơi dần là dấu hiệu SỚM NHẤT — thường trước khi pin tụt tới ngưỡng."""
        good = _health(hours=24 * 10, v0=3.65, mv_per_day=-0.05, signal=30.0)
        tail = [
            A.HealthSample(
                at=good[-1].at + timedelta(minutes=i * 120),
                battery_v=3.64,
                signal_percent=30.0,
            )
            for i in range(1, 121)
        ]
        r = A.assess_device_health(good + tail, psn="X", now=tail[-1].at)
        assert r.delivery_trend_per_day is not None
        assert r.delivery_trend_per_day < 0
        assert r.days_to_failure is not None
        assert r.likely_cause == "Mất dần khả năng truyền"


# --------------------------------------------------------------------------- #
# Bất thường
# --------------------------------------------------------------------------- #


class TestAnomalies:
    def test_clean_series_raises_nothing(self):
        """Test quan trọng nhất của nhóm này: không kêu oan."""
        s = _series(start_l=9000.0, per_day_l=500.0, hours=24 * 7)
        assert A.detect_anomalies(s, capacity_l=CAP) == []

    def test_sudden_drop_is_flagged_with_magnitude(self):
        s = _series(start_l=9000.0, per_day_l=500.0, hours=24 * 5)
        i = len(s) // 2
        hit = list(s)
        hit[i] = Sample(at=s[i].at, volume_l=(s[i].volume_l or 0.0) - 900.0)
        found = A.detect_anomalies(hit, capacity_l=CAP)
        drops = [a for a in found if a.kind == "sụt bất thường"]
        assert drops, f"không bắt được cú sụt 900 L: {found}"
        a = drops[0]
        assert a.at == s[i].at
        assert a.deviation_l is not None and a.deviation_l < -500
        assert a.z is not None and abs(a.z) >= A.ANOMALY_Z
        assert "rò rỉ" in a.note

    def test_flatline_is_caught_even_though_residuals_are_tiny(self):
        """Chuỗi kẹt khớp đường thẳng gần hoàn hảo — phần dư mù hoàn toàn ca này."""
        s = _series(start_l=9000.0, per_day_l=500.0, hours=24 * 3)
        stuck = [
            Sample(at=x.at, volume_l=8000.0 if 20 <= i <= 40 else x.volume_l)
            for i, x in enumerate(s)
        ]
        kinds = {a.kind for a in A.detect_anomalies(stuck, capacity_l=CAP)}
        assert "cảm biến kẹt" in kinds

    def test_short_series_returns_nothing_rather_than_noise(self):
        s = _series(start_l=9000.0, per_day_l=500.0, hours=2)
        assert A.detect_anomalies(s, capacity_l=CAP) == []


class TestChangePoints:
    def test_finds_a_slope_change_near_where_it_happened(self):
        first = _series(start_l=9000.0, per_day_l=200.0, hours=24 * 4)
        base = first[-1]
        second = [
            Sample(
                at=base.at + timedelta(minutes=30 * (i + 1)),
                volume_l=(base.volume_l or 0.0) - 2000.0 * ((i + 1) * 30 / 1440.0),
                pressure_mpa=0.1,
            )
            for i in range(int(24 * 4 * 60 / 30))
        ]
        cuts = A.change_points(first + second)
        assert cuts, "không phát hiện đổi chế độ tiêu thụ 200 -> 2000 L/ngày"
        boundary = len(first) - 1
        tol = 0.10 * (len(first) + len(second))
        assert min(abs(c - boundary) for c in cuts) <= tol

    def test_constant_slope_is_not_cut(self):
        """Không thay đổi thì không được cắt — nếu không thuật toán băm mọi chuỗi."""
        s = _series(start_l=9000.0, per_day_l=500.0, hours=24 * 8)
        assert A.change_points(s) == []

    def test_short_series_is_not_cut(self):
        s = _series(start_l=9000.0, per_day_l=500.0, hours=6)
        assert A.change_points(s) == []
