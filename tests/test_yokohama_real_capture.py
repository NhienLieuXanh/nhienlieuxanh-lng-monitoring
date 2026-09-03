"""Opt-in: mapping trên bản chụp thật ở var/yoko/ (gitignore). Bỏ qua nếu không có."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.adapters.yokohama.mapping import TANK_CAPACITY_L
from app.adapters.yokohama.normalizer import normalize_alarm, normalize_reading
from app.domain.contracts import MappingReport
from app.repositories.vendor_alarms import to_row

CAPTURE = Path(__file__).resolve().parent.parent / "var" / "yoko"
VN = ZoneInfo("Asia/Ho_Chi_Minh")
PSN = "YKH-TANK-01"

pytestmark = pytest.mark.skipif(
    not (CAPTURE / "rec.json").is_file(),
    reason="không có bản chụp thật ở var/yoko/",
)


def test_real_daily_ratio_is_60_m3() -> None:
    rows = json.loads((CAPTURE / "rec.json").read_text(encoding="utf-8"))
    assert rows
    row = rows[0]
    rep = MappingReport()
    r = normalize_reading(row, psn=PSN, vendor_tz=VN, report=rep)
    assert r is not None
    assert r.capacity_l == TANK_CAPACITY_L
    vol = Decimal(str(row["tankPrecent"]))
    pct = Decimal(str(row["tankVolume"]))
    assert abs(vol / (pct / Decimal("100")) - Decimal("60")) < Decimal("0.2")


def test_real_valve_alarms_four_keys() -> None:
    alarms = json.loads((CAPTURE / "alarm.json").read_text(encoding="utf-8"))
    sv = [a for a in alarms if a.get("deviceId") in ("SV3", "SV4")]
    assert len(sv) >= 4
    keys = set()
    for raw in sv[:4]:
        alarm = normalize_alarm(raw, site_code="YKH", vendor_tz=VN)
        assert alarm is not None
        d = to_row(alarm)
        keys.add((d["device_id"], d["raised_at"], d["message_hash"]))
    assert len(keys) == 4
