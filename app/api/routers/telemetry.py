"""GET /api/telemetry/{psn} và /api/telemetry/{psn}/latest."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.api.deps import HistoryQueryDep, SessionDep, UserDep
from app.api.schemas import Page, TelemetryOut
from app.repositories import telemetry as tel_repo
from app.repositories import terminals as term_repo

router = APIRouter(prefix="/telemetry", tags=["telemetry"])


def _require_terminal(session: SessionDep, psn: str) -> None:
    if term_repo.get_by_psn(session, psn) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Terminal not found")


@router.get("/{psn}/latest", response_model=TelemetryOut | None)
def latest(psn: str, session: SessionDep, _: UserDep) -> TelemetryOut | None:
    """Lần đọc mới nhất.

    404 chỉ khi PSN không tồn tại. PSN tồn tại nhưng chưa từng có số liệu -> 200 với
    body ``null``: một terminal vừa được provision là trạng thái BÌNH THƯỜNG, không
    phải lỗi, và buộc dashboard xử lý 404 cho nó là vô cớ.
    """
    _require_terminal(session, psn)
    row = tel_repo.latest_for(session, psn)
    return TelemetryOut.model_validate(row) if row is not None else None


@router.get("/{psn}", response_model=Page[TelemetryOut])
def history(
    psn: str, session: SessionDep, q: HistoryQueryDep, _: UserDep
) -> Page[TelemetryOut]:
    _require_terminal(session, psn)
    assert q.from_ is not None and q.to is not None  # history_query đã điền default
    rows, total = tel_repo.history(
        session,
        psn,
        q.from_,
        q.to,
        limit=q.limit,
        offset=q.offset,
        ascending=q.ascending,
    )
    return Page[TelemetryOut](
        items=[TelemetryOut.model_validate(r) for r in rows],
        page=q.page,
        limit=q.limit,
        total=total,
        has_next=q.offset + q.limit < total,
    )
