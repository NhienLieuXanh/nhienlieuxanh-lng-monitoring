"""Logic đối chiếu TZ — không gọi mạng."""

from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "verify_tz.py"
_spec = importlib.util.spec_from_file_location("verify_tz", _SCRIPT)
assert _spec and _spec.loader
verify_tz = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify_tz)

UTC = ZoneInfo("UTC")


def test_shanghai_hypothesis_matches_known_measurement():
    """Repro thí nghiệm 2026-08-18: server 15:03:18, UTC 07:03:17 → Shanghai."""
    naive = datetime(2026, 8, 18, 15, 3, 18)
    utc_now = datetime(2026, 8, 18, 7, 3, 17, tzinfo=UTC)
    scores = verify_tz.score_timezones(naive, utc_now)
    by_name = {n: (d, ok) for n, d, ok in scores}
    assert by_name["Asia/Shanghai"][1] is True
    assert by_name["Asia/Ho_Chi_Minh"][1] is False
    assert by_name["UTC"][1] is False
    ok, msg = verify_tz.verdict("Asia/Shanghai", scores)
    assert ok, msg
    bad, _ = verify_tz.verdict("Asia/Ho_Chi_Minh", scores)
    assert bad is False


def test_parse_gateway_timestamp():
    ts = verify_tz.parse_gateway_timestamp(
        {"timestamp": "2026-08-18 15:03:18", "status": 405}
    )
    assert ts == datetime(2026, 8, 18, 15, 3, 18)
    assert ts.tzinfo is None


def test_parse_gateway_timestamp_missing():
    with pytest.raises(ValueError):
        verify_tz.parse_gateway_timestamp({"status": 405})


def test_terminal_update_schema_requires_a_field():
    from pydantic import ValidationError

    from app.api.schemas import TerminalUpdateIn

    with pytest.raises(ValidationError):
        TerminalUpdateIn()
    with pytest.raises(ValidationError):
        TerminalUpdateIn(name="   ")
    got = TerminalUpdateIn(name="  Bồn A  ")
    assert got.name == "Bồn A"
    assert TerminalUpdateIn(capacity_l="10425").capacity_l == 10425
