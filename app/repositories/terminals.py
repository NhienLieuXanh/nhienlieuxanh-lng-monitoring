"""Đọc/ghi bảng terminals."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, literal_column, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import Terminal
from app.domain.contracts import NormalizedTerminal

log = logging.getLogger(__name__)

# Metadata do vendor cấp. CHỈ điền vào chỗ đang NULL — xem upsert().
_VENDOR_META = (
    "modem_number",
    "sim_iccid",
    "hardware_version",
    "software_version",
    "device_model",
    "device_type_name",
    "medium_name",
    "tank_type_name",
)


def upsert(
    session: Session,
    psn: str,
    *,
    meta: NormalizedTerminal | None = None,
    default_capacity_l: Decimal | None = None,
    default_name: str | None = None,
) -> tuple[UUID, bool]:
    """Tạo terminal nếu chưa có, trả về (id, vừa_được_tạo).

    Dùng on_conflict_do_update chứ không do_nothing: DO NOTHING không trả row khi
    conflict, nên sẽ phải SELECT lần hai để biết id. Một set_ tầm thường
    (updated_at) đảm bảo RETURNING luôn có id — một round trip, không race.

    RETURNING xmax = 0 là idiom Postgres phân biệt INSERT với UPDATE, cho ta
    terminals_created miễn phí.
    """
    values: dict[str, Any] = {
        "psn": psn,
        "name": default_name or f"Bồn LNG - {psn}",
        "capacity_l": (meta.capacity_l if meta else None) or default_capacity_l,
    }
    if meta is not None:
        for f in _VENDOR_META:
            values[f] = getattr(meta, f, None)

    stmt = pg_insert(Terminal).values(**values)
    # KHÔNG ghi đè `name` và `capacity_l` bằng giá trị vendor. Nếu ghi đè, người
    # vận hành đặt tên "Bồn A - Kho Long An" rồi lần ingest kế tiếp reset nó về
    # "Bồn LNG - 2604200016". Metadata vendor chỉ điền vào chỗ đang NULL.
    set_: dict[str, Any] = {"updated_at": func.now()}
    for f in _VENDOR_META:
        set_[f] = func.coalesce(getattr(Terminal, f), stmt.excluded[f])
    set_["capacity_l"] = func.coalesce(Terminal.capacity_l, stmt.excluded.capacity_l)
    set_["name"] = func.coalesce(Terminal.name, stmt.excluded.name)

    row = session.execute(
        stmt.on_conflict_do_update(index_elements=["psn"], set_=set_).returning(
            Terminal.id, literal_column("xmax = 0").label("inserted")
        )
    ).one()
    tid, created = row[0], bool(row[1])
    if created:
        # PSN lạ lần đầu: log INFO để một PSN gõ sai hiện ra thay vì âm thầm tạo
        # một "bồn ma" mà sau này không ai biết từ đâu ra.
        log.info("terminal mới được tạo cho PSN %s (id=%s)", psn, tid)
    return tid, created


def get_by_psn(session: Session, psn: str) -> Terminal | None:
    return session.execute(
        select(Terminal).where(Terminal.psn == psn)
    ).scalar_one_or_none()


def all_psns(session: Session) -> list[str]:
    return list(
        session.execute(select(Terminal.psn).order_by(Terminal.psn)).scalars().all()
    )


def list_all(session: Session) -> list[Terminal]:
    return list(
        session.execute(select(Terminal).order_by(Terminal.psn)).scalars().all()
    )


def bump_last_seen(session: Session, latest: dict[str, datetime]) -> int:
    """Cập nhật last_seen_at, CHỈ khi tiến về phía trước.

    Guard monotonic là bắt buộc: backfill dữ liệu tháng 6 không được kéo
    last_seen_at lùi lại và làm một thiết bị đang sống trông như đã chết.

    Lưu ý: UPDATE thô KHÔNG kích hoạt onupdate=func.now() của SQLAlchemy, nên
    updated_at phải set tường minh. Đúng với mọi bulk UPDATE trong codebase này.
    """
    n = 0
    for psn, ts in latest.items():
        res = session.execute(
            update(Terminal)
            .where(
                Terminal.psn == psn,
                (Terminal.last_seen_at.is_(None)) | (Terminal.last_seen_at < ts),
            )
            .values(last_seen_at=ts, updated_at=func.now())
        )
        n += res.rowcount or 0
    return n


def refresh_status_cache(session: Session, stale_after: timedelta) -> int:
    """Đồng bộ lại cột `status` cho MỌI terminal.

    Cần thiết vì cột lưu có lỗi staleness không tránh được: thiết bị ngừng báo thì
    không ingest nào chạm row đó, nên guard monotonic ở bump_last_seen() không bao
    giờ chạy và nó đứng mãi ở 'online'. API thì suy lại lúc đọc nên không bị, nhưng
    cột này vẫn cần đúng cho query SQL ad-hoc và alert phía DB.

    `WHERE status <> (CASE ...)` giữ cho phần lớn cycle là no-op: không dead tuple,
    không churn updated_at vô ích.
    """
    minutes = int(stale_after.total_seconds() // 60)
    computed = text(
        "CASE WHEN last_seen_at IS NOT NULL "
        "      AND last_seen_at >= now() - make_interval(mins => :mins) "
        "     THEN 'online' ELSE 'offline' END"
    ).bindparams(mins=minutes)
    res = session.execute(
        update(Terminal)
        .where(Terminal.status != computed)
        .values(status=computed, updated_at=func.now())
    )
    return res.rowcount or 0


def update_operator(
    session: Session,
    psn: str,
    *,
    name: str | None = None,
    capacity_l: Decimal | None = None,
) -> Terminal | None:
    """Sửa field do người vận hành sở hữu. Trả None nếu PSN không tồn tại.

    Ingest chỉ COALESCE vào chỗ NULL — một lần sửa ở đây là bền, không bị vòng
    ingest kế tiếp ghi đè.
    """
    term = get_by_psn(session, psn)
    if term is None:
        return None
    if name is not None:
        term.name = name
    if capacity_l is not None:
        term.capacity_l = capacity_l
    session.flush()
    return term


def counts_by_status(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(Terminal.status, func.count()).group_by(Terminal.status)
    ).all()
    return {str(s): int(c) for s, c in rows}
