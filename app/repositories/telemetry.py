"""Đọc/ghi bảng telemetry."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import Telemetry
from app.domain.contracts import MEASURE_FIELDS, NormalizedTelemetry

log = logging.getLogger(__name__)

CHUNK = 500

# Cột được phép cập nhật ở chế độ --repair. `sampled_at`/`psn` là khoá nên không có
# ở đây; `created_at` là thời điểm ta ghi nên cũng không.
_REPAIRABLE = (*MEASURE_FIELDS, "volume_percent_source", "medium_name",
               "tank_type_name", "vendor_ts_raw", "raw_payload")


def to_row(reading: NormalizedTelemetry, terminal_id: UUID) -> dict[str, Any]:
    d = reading.model_dump(exclude={"capacity_l"})
    d["terminal_id"] = terminal_id
    # StrEnum -> str để psycopg khỏi phải đoán.
    src = d.get("volume_percent_source")
    d["volume_percent_source"] = str(src) if src is not None else None
    return d


def dedupe(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Loại trùng (psn, sampled_at) TRONG batch, giữ bản cuối.

    Với ON CONFLICT DO NOTHING việc này về mặt kỹ thuật là optional, nhưng nó (a)
    làm số liệu thống kê trung thực và (b) BẮT BUỘC ngay khi ai đó chuyển sang
    DO UPDATE — Postgres raise "ON CONFLICT DO UPDATE command cannot affect row a
    second time" nếu batch có trùng. Làm luôn để bỏ mìn.
    """
    seen: dict[tuple[str, datetime], dict[str, Any]] = {}
    for r in rows:
        seen[(r["psn"], r["sampled_at"])] = r
    return list(seen.values()), len(rows) - len(seen)


def bulk_upsert(
    session: Session, rows: list[dict[str, Any]], *, repair: bool = False
) -> tuple[int, int]:
    """Upsert idempotent. Trả về (inserted, duplicates).

    Đếm bằng RETURNING chứ không bằng rowcount: DO NOTHING không cho feedback
    per-row và rowcount không đáng tin giữa các driver, còn RETURNING chỉ phát ra
    một dòng cho mỗi dòng THỰC SỰ được ghi.
    """
    if not rows:
        return 0, 0

    deduped, intra = dedupe(rows)
    inserted = 0
    conflicts = 0

    for start in range(0, len(deduped), CHUNK):
        chunk = deduped[start : start + CHUNK]
        stmt = pg_insert(Telemetry).values(chunk)
        if repair:
            # COALESCE(excluded, current): điền chỗ đang NULL nhưng KHÔNG BAO GIỜ
            # ghi NULL lên một giá trị thật. Vendor đôi khi trả null cho field mà
            # lần trước có số; không có COALESCE thì --repair sẽ xoá dữ liệu tốt.
            stmt = stmt.on_conflict_do_update(
                index_elements=["psn", "sampled_at"],
                set_={
                    c: func.coalesce(stmt.excluded[c], getattr(Telemetry, c))
                    for c in _REPAIRABLE
                },
            )
            # DO UPDATE phát RETURNING cho MỌI row (cả insert lẫn update), nên đếm
            # len() sẽ tính update thành insert. `xmax = 0` là idiom Postgres phân
            # biệt: true = vừa insert, false = update một row đã có (một dup).
            rows_out = session.execute(
                stmt.returning(literal_column("xmax = 0").label("inserted"))
            ).all()
            ins = sum(1 for r in rows_out if r.inserted)
            inserted += ins
            conflicts += len(rows_out) - ins
        else:
            # Mặc định: một điểm đo là sự thật lịch sử bất biến. DO NOTHING chỉ phát
            # RETURNING cho row THỰC SỰ được insert; row conflict không xuất hiện.
            stmt = stmt.on_conflict_do_nothing(index_elements=["psn", "sampled_at"])
            n = len(session.execute(stmt.returning(Telemetry.psn)).fetchall())
            inserted += n
            conflicts += len(chunk) - n

    return inserted, conflicts + intra


def latest_for(session: Session, psn: str) -> Telemetry | None:
    """Lần đọc mới nhất. Một backward index-scan trên pk_telemetry."""
    return session.execute(
        select(Telemetry)
        .where(Telemetry.psn == psn)
        .order_by(Telemetry.sampled_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def latest_many(session: Session, psns: list[str]) -> dict[str, Telemetry]:
    """Lần đọc mới nhất cho nhiều PSN, một query.

    DISTINCT ON là idiom Postgres cho latest-per-group; nó dùng đúng index
    (psn, sampled_at) nên không cần window function hay N+1.
    """
    if not psns:
        return {}
    rows = session.execute(
        select(Telemetry)
        .where(Telemetry.psn.in_(psns))
        .order_by(Telemetry.psn, Telemetry.sampled_at.desc())
        .distinct(Telemetry.psn)
    ).scalars()
    return {r.psn: r for r in rows}


def _history_stmt(psn: str, start: datetime, end: datetime) -> Select[Any]:
    return select(Telemetry).where(
        Telemetry.psn == psn,
        Telemetry.sampled_at >= start,
        Telemetry.sampled_at <= end,
    )


def history(
    session: Session,
    psn: str,
    start: datetime,
    end: datetime,
    *,
    limit: int,
    offset: int,
    ascending: bool,
) -> tuple[list[Telemetry], int]:
    total = session.execute(
        select(func.count()).select_from(_history_stmt(psn, start, end).subquery())
    ).scalar_one()
    order = Telemetry.sampled_at.asc() if ascending else Telemetry.sampled_at.desc()
    items = (
        session.execute(
            _history_stmt(psn, start, end).order_by(order).limit(limit).offset(offset)
        )
        .scalars()
        .all()
    )
    return list(items), int(total)


def max_sampled_at(session: Session, psns: list[str]) -> dict[str, datetime]:
    if not psns:
        return {}
    rows = session.execute(
        select(Telemetry.psn, func.max(Telemetry.sampled_at))
        .where(Telemetry.psn.in_(psns))
        .group_by(Telemetry.psn)
    ).all()
    return {psn: ts for psn, ts in rows if ts is not None}


def count_all(session: Session) -> int:
    return int(session.execute(select(func.count()).select_from(Telemetry)).scalar_one())

