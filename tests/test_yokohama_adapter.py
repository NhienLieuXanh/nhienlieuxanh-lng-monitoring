"""Tuổi thọ cache theo cycle, trần stream, fatal types."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.adapters.yokohama.adapter import EST_BYTES_PER_DAY, YokohamaAdapter
from app.adapters.yokohama.config import YokohamaSettings
from app.adapters.yokohama.errors import YokohamaSchemaError

VN = ZoneInfo("Asia/Ho_Chi_Minh")
PSN = "YKH-TANK-01"
TODAY = datetime.now(tz=VN).date()
DAY = TODAY - timedelta(days=1)
DAY2 = TODAY


def _stamp(d: date, hm: str) -> str:
    return f"{d.strftime('%d/%m/%Y')} {hm}"


def _row(dt: str) -> dict:
    return {
        "dateTime": dt,
        "receivedAt": dt,
        "totalizer": 100000.0,
        "flowRate": 0.0,
        "pressure": 300.0,
        "temperature": 25.0,
        "tankVolume": 80.0,
        "tankNumber": 10,
        "tankPrecent": 48.0,
        "pT1_Value": 3.0,
        "pS1_Value": 3.0,
        "pS2_Value": 2.0,
        "tE1_Value": 25.0,
        "gD1_Value": 1.0,
        "gD2_Value": 0.2,
        "gD3_Value": 0.0,
    }


class _MemClient:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = list(rows or [])
        self.stream_calls = 0

    def iter_record_objects(self, params: dict) -> list:
        self.stream_calls += 1
        yield from list(self.rows)

    def get_json(self, path: str, params: dict | None = None) -> list:
        return []

    def close(self) -> None:
        return None


def _adapter(client: _MemClient) -> YokohamaAdapter:
    settings = YokohamaSettings(enabled=False, psn=PSN)
    return YokohamaAdapter(settings, client=client)


def test_begin_cycle_picks_up_new_minute_rows() -> None:
    client = _MemClient([_row(_stamp(DAY, "12:00"))])
    adapter = _adapter(client)
    first = adapter.fetch_telemetry(PSN, DAY)
    assert len(first.readings) == 1
    assert client.stream_calls == 1

    client.rows = [
        _row(_stamp(DAY, "12:02")),
        _row(_stamp(DAY, "12:01")),
        _row(_stamp(DAY, "12:00")),
    ]
    stale = adapter.fetch_telemetry(PSN, DAY)
    assert len(stale.readings) == 1
    assert client.stream_calls == 1

    adapter.begin_cycle()
    assert adapter._day_cache == {}
    assert adapter._seen == set()
    fresh = adapter.fetch_telemetry(PSN, DAY)
    assert len(fresh.readings) == 3
    assert client.stream_calls == 2


def test_one_cycle_two_days_one_stream() -> None:
    client = _MemClient(
        [
            _row(_stamp(DAY2, "00:30")),
            _row(_stamp(DAY, "12:00")),
        ]
    )
    adapter = _adapter(client)
    adapter.begin_cycle()
    older = adapter.fetch_telemetry(PSN, DAY)
    newer = adapter.fetch_telemetry(PSN, DAY2)
    assert len(older.readings) == 1
    assert len(newer.readings) == 1
    assert client.stream_calls == 1


def test_far_backfill_rejected_before_stream() -> None:
    client = _MemClient([_row(_stamp(TODAY, "12:00"))])
    adapter = _adapter(client)
    far = datetime.now(tz=VN).date() - timedelta(days=30)
    with pytest.raises(YokohamaSchemaError, match="ngày") as ei:
        adapter.fetch_telemetry(PSN, far)
    assert str(EST_BYTES_PER_DAY) in str(ei.value) or "byte" in str(ei.value).lower()
    assert client.stream_calls == 0


def test_schema_error_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.adapters.yokohama.config import YokohamaSettings as YS
    from app.config import Settings
    from app.factory import build_adapter

    monkeypatch.setattr(
        "app.adapters.yokohama.config.get_yokohama_settings",
        lambda: YS(enabled=True, base_url="https://example.test/"),
    )
    built = build_adapter(
        Settings(app_env="test", db_password="x", scheduler_enabled=False)
    )
    try:
        names = [t.__name__ for t in built.fatal_exc_types]
        assert names == ["XingkeSessionExpired"]
    finally:
        built.close()
