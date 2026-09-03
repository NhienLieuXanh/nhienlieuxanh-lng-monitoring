"""Mapping nguồn đo phút trên fixture cắt gọn — không mạng, không DB."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.adapters.yokohama.mapping import (
    TANK_CAPACITY_L,
    FieldSpec,
    assert_mapping_sane,
    parse_vendor_ts,
)
from app.adapters.yokohama.normalizer import normalize_alarm, normalize_reading
from app.domain.contracts import MappingReport, NormalizedTelemetry
from app.repositories.vendor_alarms import to_row

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "yokohama"
VN = ZoneInfo("Asia/Ho_Chi_Minh")
PSN = "YKH-TANK-01"


def _load(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_daily_volume_is_m3_not_percent() -> None:
    """tankPrecent là m³, tankVolume là % — 48 m³ / 80% = 60 m³ dung tích."""
    row = _load("daily.json")[0]
    vol_m3 = Decimal(str(row["tankPrecent"]))
    pct = Decimal(str(row["tankVolume"]))
    rep = MappingReport()
    r = normalize_reading(row, psn=PSN, vendor_tz=VN, report=rep)
    assert r is not None
    assert r.volume_l == vol_m3 * Decimal("1000")
    assert r.volume_percent == pct
    assert r.capacity_l == TANK_CAPACITY_L
    assert r.pressure_mpa == Decimal(str(row["pT1_Value"])) * Decimal("0.1")
    assert r.ps1_bar == Decimal(str(row["pS1_Value"]))
    assert r.gm_flow_rate_nm3h == Decimal("0.0")
    assert r.refill_counter == int(row["tankNumber"])
    assert r.sampled_at.tzinfo is not None
    assert r.vendor_ts_raw == row["dateTime"]


def test_swapped_names_rejected_by_volume_hi() -> None:
    """Đọc ngược tên field: mức% × 1000 vượt hi=61 000 L → loại volume_l."""
    row = dict(_load("daily.json")[0])
    row["tankPrecent"], row["tankVolume"] = row["tankVolume"], row["tankPrecent"]
    rep = MappingReport()
    r = normalize_reading(row, psn=PSN, vendor_tz=VN, report=rep)
    assert r is not None
    assert r.volume_l is None
    assert any(f == "volume_l" for f, _ in rep.errors)


def test_zero_pressure_is_missing_not_zero() -> None:
    row = _load("minute.json")[0]
    assert row["pS1_Value"] == 0.0
    rep = MappingReport()
    r = normalize_reading(row, psn=PSN, vendor_tz=VN, report=rep)
    assert r is not None
    assert r.ps1_bar is None
    assert r.ps2_bar is None
    assert r.gm_flow_rate_nm3h == Decimal("0.0")
    assert rep.zero_as_missing >= 2


def test_capacity_ratio_60_m3() -> None:
    row = _load("daily.json")[0]
    vol = Decimal(str(row["tankPrecent"]))
    pct = Decimal(str(row["tankVolume"]))
    cap_m3 = vol / (pct / Decimal("100"))
    assert abs(cap_m3 - Decimal("60")) < Decimal("0.1")


def test_timestamp_is_ict() -> None:
    ts = parse_vendor_ts("01/01/2020 07:00", VN)
    assert ts == datetime(2020, 1, 1, 0, 0, tzinfo=ZoneInfo("UTC"))


def test_extra_forbid_typo() -> None:
    with pytest.raises(ValidationError):
        NormalizedTelemetry(
            source="ykh",
            psn=PSN,
            sampled_at=datetime(2026, 8, 27, 0, 1, tzinfo=ZoneInfo("UTC")),
            raw_payload={},
            gm_totalizer_Nm3=Decimal("1"),  # type: ignore[call-arg]
        )


def test_assert_mapping_sane_rejects_unknown_target() -> None:
    bogus = (
        FieldSpec("not_a_real_column", ("tankPrecent",), zero_is_missing=True),
    )
    with pytest.raises(RuntimeError, match="NormalizedTelemetry"):
        assert_mapping_sane(bogus)


def test_four_valve_alarms_same_second_are_distinct() -> None:
    rows = [a for a in _load("alarms.json") if a["deviceId"] in ("SV3", "SV4")]
    assert len(rows) == 4
    keys = set()
    for raw in rows:
        alarm = normalize_alarm(raw, site_code="YKH", vendor_tz=VN)
        assert alarm is not None
        d = to_row(alarm)
        keys.add((d["device_id"], d["raised_at"], d["message_hash"]))
    assert len(keys) == 4
