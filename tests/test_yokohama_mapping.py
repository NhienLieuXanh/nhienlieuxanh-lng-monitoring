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


def test_zero_tren_ps1_ps2_la_gia_tri_that() -> None:
    """ĐỔI KỲ VỌNG có chủ ý (2026-09-04), dựa trên trang Main của cổng.

    Trước đây test này ghim ``ps1_bar is None`` cho pS1_Value = 0. Đối chiếu cổng
    sống cho thấy đó là sai: trang Main hiển thị "Pressure (PS1): 0.00 bar" và
    "Pressure Value (PS2): 0.00 bar" như GIÁ TRỊ (tô cam), và danh sách báo động
    7 ngày có PS1 25 lần + PS2 28 lần. 0,00 bar chính là điều kiện đang báo động,
    nên coi nó là thiếu dữ liệu là che đúng cái cần thấy.

    Áp suất BỒN (pT1) thì vẫn khác: LNG tự sinh áp nên 0 bar khi còn lỏng là hỏng
    cảm biến, không phải số đo. Ranh giới đó được giữ ở đây.
    """
    row = _load("minute.json")[0]
    assert row["pS1_Value"] == 0.0
    rep = MappingReport()
    r = normalize_reading(row, psn=PSN, vendor_tz=VN, report=rep)
    assert r is not None
    assert r.ps1_bar == Decimal("0.0"), "0 bar trên công tắc áp là số đo thật"
    assert r.ps2_bar == Decimal("0.0")
    assert r.gm_flow_rate_nm3h == Decimal("0.0"), "0 lưu lượng vẫn là số đo thật"

    zero_tank = dict(row)
    zero_tank["pT1_Value"] = 0.0
    rep2 = MappingReport()
    r2 = normalize_reading(zero_tank, psn=PSN, vendor_tz=VN, report=rep2)
    assert r2 is not None
    assert r2.pressure_mpa is None, "áp BỒN = 0 là hỏng cảm biến, không phải 0 bar"
    assert rep2.zero_as_missing >= 1


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
