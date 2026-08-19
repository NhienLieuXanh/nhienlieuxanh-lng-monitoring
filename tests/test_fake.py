"""FakeAdapter tôn trọng hợp đồng TelemetryPort — không cần DB."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.adapters.fake import DEMO_PSNS, FakeAdapter, FakeAuthError

UTC = ZoneInfo("UTC")
SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_returns_only_requested_vendor_day():
    now = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)
    fake = FakeAdapter(days=2, fresh=True, now=now)
    day = now.astimezone(SHANGHAI).date()
    res = fake.fetch_telemetry(DEMO_PSNS[0], day)
    assert res.readings
    assert all(r.sampled_at.astimezone(SHANGHAI).date() == day for r in res.readings)
    empty = fake.fetch_telemetry(DEMO_PSNS[0], date(2020, 1, 1))
    assert empty.readings == []


def test_vendor_tz_is_shanghai():
    assert FakeAdapter().vendor_tz == SHANGHAI


def test_empty_switch_is_not_an_error():
    res = FakeAdapter(return_empty=True).fetch_telemetry(DEMO_PSNS[0], date(2026, 7, 23))
    assert res.readings == []
    assert res.report.rejected_rows == 0


def test_auth_switch_raises_without_importing_vendor():
    fake = FakeAdapter(raise_auth=True)
    try:
        fake.fetch_telemetry(DEMO_PSNS[0], date(2026, 7, 23))
    except FakeAuthError:
        return
    raise AssertionError("expected FakeAuthError")


def test_volume_percent_is_0_to_100_and_matches_capacity():
    fake = FakeAdapter(days=1, fresh=True)
    day = fake._anchor(DEMO_PSNS[0]).astimezone(SHANGHAI).date()
    res = fake.fetch_telemetry(DEMO_PSNS[0], day)
    assert res.readings
    for r in res.readings:
        assert r.volume_l is not None and r.capacity_l is not None
        assert r.volume_percent is not None
        derived = (r.volume_l / r.capacity_l) * 100
        assert abs(derived - r.volume_percent) < 0.02
        assert 0 <= r.volume_percent <= 100
