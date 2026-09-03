"""Áp mapping.py lên payload raw. Thuần hàm, test bằng fixture JSON."""

from __future__ import annotations

import logging
from typing import Any
from zoneinfo import ZoneInfo

from app.adapters.yokohama import mapping as M
from app.domain.contracts import (
    MappingReport,
    NormalizedAlarm,
    NormalizedTelemetry,
    NormalizedTerminal,
    PercentSource,
)

log = logging.getLogger(__name__)

SOURCE = "ykh"


def normalize_reading(
    row: dict[str, Any],
    *,
    psn: str,
    vendor_tz: ZoneInfo,
    report: MappingReport,
    store_raw: bool = False,
    ts_order: str = "dmy",
) -> NormalizedTelemetry | None:
    index = M.build_index(row)
    M.record_unmapped(index, report)

    raw_ts = M.find_timestamp(index)
    try:
        sampled_at = M.parse_vendor_ts(raw_ts, vendor_tz, order=ts_order)
    except M.TimestampParseError as exc:
        report.rejected_rows += 1
        report.errors.append(("sampled_at", str(exc)))
        log.warning("ykh: loại dòng psn=%s vì timestamp: %s", psn, exc)
        return None

    values: dict[str, Any] = {}
    for spec in M.TELEMETRY_FIELDS:
        values[spec.target] = M.extract_number(index, spec, report)

    percent_source = (
        PercentSource.VENDOR if values.get("volume_percent") is not None else None
    )
    capacity_l = M.capacity_from_ratio(
        values.get("volume_l"), values.get("volume_percent")
    )

    return NormalizedTelemetry(
        source=SOURCE,
        psn=psn,
        sampled_at=sampled_at,
        vendor_ts_raw=str(raw_ts) if raw_ts is not None else None,
        volume_percent_source=percent_source,
        capacity_l=capacity_l,
        refill_counter=M.extract_refill_counter(index),
        raw_payload=dict(row) if store_raw else {},
        **values,
    )


def normalize_terminal(psn: str) -> NormalizedTerminal:
    return NormalizedTerminal(
        psn=psn,
        name=None,
        capacity_l=M.TANK_CAPACITY_L,
        medium_name="LNG",
        raw_payload={},
    )


def normalize_alarm(
    row: dict[str, Any],
    *,
    site_code: str,
    vendor_tz: ZoneInfo,
    ts_order: str = "dmy",
) -> NormalizedAlarm | None:
    raw_ts = row.get("createAt") or row.get("create_at")
    try:
        raised_at = M.parse_vendor_ts(raw_ts, vendor_tz, order=ts_order)
    except M.TimestampParseError:
        log.warning("ykh: loại báo động vì timestamp %r", raw_ts)
        return None
    device = str(row.get("deviceId") or row.get("device_id") or "").strip()
    message = str(row.get("message") or "").strip()
    if not device or not message:
        return None
    symbol = row.get("symbol")
    return NormalizedAlarm(
        source=SOURCE,
        site_code=site_code,
        device_id=device,
        raised_at=raised_at,
        vendor_ts_raw=str(raw_ts),
        message=message,
        symbol=None if symbol is None else str(symbol),
    )
