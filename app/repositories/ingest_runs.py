"""Đọc/ghi bảng ingest_runs — audit log của ingestion."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import IngestRun

# Trạng thái tính là "ingest đã chạy được". 'partial' được tính vào: một thiết bị
# lỗi trong khi các thiết bị khác OK vẫn là ingestion đang hoạt động, và coi nó là
# thất bại sẽ làm health đỏ vì một lý do không liên quan.
OK_STATUSES = ("success", "partial")


def record(
    session: Session,
    *,
    trigger: str,
    status: str,
    started_at: datetime,
    finished_at: datetime,
    fetched: int = 0,
    inserted: int = 0,
    duplicates: int = 0,
    terminals_created: int = 0,
    errors: list[str] | None = None,
    params: dict[str, Any] | None = None,
    mapping_report: dict[str, Any] | None = None,
) -> IngestRun:
    errs = errors or []
    run = IngestRun(
        trigger=trigger,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        fetched=fetched,
        inserted=inserted,
        duplicates=duplicates,
        terminals_created=terminals_created,
        error_count=len(errs),
        # Giới hạn độ dài: error_summary là để người đọc, không phải để lưu trace.
        error_summary="\n".join(errs)[:4000] or None,
        params=params or {},
        mapping_report=mapping_report or {},
    )
    session.add(run)
    session.flush()
    return run


def last_success(session: Session) -> IngestRun | None:
    return session.execute(
        select(IngestRun)
        .where(IngestRun.status.in_(OK_STATUSES), IngestRun.finished_at.is_not(None))
        .order_by(IngestRun.finished_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def last_run(session: Session) -> IngestRun | None:
    return session.execute(
        select(IngestRun).order_by(IngestRun.id.desc()).limit(1)
    ).scalar_one_or_none()


def recent(session: Session, limit: int = 20) -> list[IngestRun]:
    return list(
        session.execute(select(IngestRun).order_by(IngestRun.id.desc()).limit(limit))
        .scalars()
        .all()
    )
