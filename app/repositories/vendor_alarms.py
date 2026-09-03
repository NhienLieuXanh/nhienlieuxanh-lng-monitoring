"""Ghi bảng vendor_alarms. ON CONFLICT DO NOTHING — báo động là lịch sử bất biến."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import VendorAlarm
from app.domain.contracts import NormalizedAlarm

CHUNK = 500


def message_hash(message: str) -> str:
    return hashlib.md5(message.encode("utf-8"), usedforsecurity=False).hexdigest()


def to_row(alarm: NormalizedAlarm) -> dict:
    return {
        "site_code": alarm.site_code,
        "device_id": alarm.device_id,
        "raised_at": alarm.raised_at,
        "vendor_ts_raw": alarm.vendor_ts_raw,
        "message": alarm.message,
        "message_hash": message_hash(alarm.message),
        "symbol": alarm.symbol,
        "source": alarm.source,
    }


def bulk_insert(session: Session, alarms: Sequence[NormalizedAlarm]) -> tuple[int, int]:
    """Trả về (inserted, duplicates)."""
    if not alarms:
        return 0, 0
    rows = [to_row(a) for a in alarms]
    inserted = 0
    for start in range(0, len(rows), CHUNK):
        chunk = rows[start : start + CHUNK]
        stmt = (
            pg_insert(VendorAlarm)
            .values(chunk)
            .on_conflict_do_nothing(
                constraint="uq_vendor_alarms_natural"
            )
        )
        n = len(session.execute(stmt.returning(VendorAlarm.id)).fetchall())
        inserted += n
    return inserted, len(rows) - inserted
