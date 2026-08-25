"""GET /api/terminals và /api/terminals/{psn}."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import SessionDep, SettingsDep, UserDep, to_terminal_out
from app.api.schemas import Page, TerminalDetailOut, TerminalOut, TerminalUpdateIn
from app.repositories import telemetry as tel_repo
from app.repositories import terminals as term_repo

router = APIRouter(prefix="/terminals", tags=["terminals"])
UTC = ZoneInfo("UTC")


@router.get("", response_model=Page[TerminalOut])
def list_terminals(
    session: SessionDep,
    settings: SettingsDep,
    _: UserDep,
    status_filter: Annotated[
        Literal["online", "offline"] | None, Query(alias="status")
    ] = None,
    q: Annotated[str | None, Query(description="lọc theo PSN hoặc tên")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> Page[TerminalOut]:
    now = datetime.now(tz=UTC)
    stale = timedelta(minutes=settings.online_stale_minutes)

    terms = term_repo.list_all(session)
    if q:
        needle = q.strip().lower()
        terms = [
            t
            for t in terms
            if needle in t.psn.lower() or needle in (t.name or "").lower()
        ]

    # Một query cho toàn bộ "lần đọc mới nhất mỗi PSN" (DISTINCT ON), không N+1.
    latest = tel_repo.latest_many(session, [t.psn for t in terms])
    items = [
        to_terminal_out(t, latest.get(t.psn), now=now, stale_after=stale)
        for t in terms
    ]
    # Lọc status SAU khi map, vì status được suy lúc đọc chứ không lấy từ cột cache.
    if status_filter is not None:
        items = [i for i in items if i.status == status_filter]

    total = len(items)
    start = (page - 1) * limit
    return Page[TerminalOut](
        items=items[start : start + limit],
        page=page,
        limit=limit,
        total=total,
        has_next=start + limit < total,
    )


@router.get("/{psn}", response_model=TerminalDetailOut)
def get_terminal(psn: str, session: SessionDep, settings: SettingsDep, _: UserDep) -> TerminalDetailOut:
    term = term_repo.get_by_psn(session, psn)
    if term is None:
        # 404, KHÔNG phải 200 rỗng: trả rỗng cho một PSN gõ sai làm typo không phân
        # biệt được với "chưa có dữ liệu", và che mất lỗi thật.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Terminal not found")
    return to_terminal_out(
        term,
        tel_repo.latest_for(session, psn),
        now=datetime.now(tz=UTC),
        stale_after=timedelta(minutes=settings.online_stale_minutes),
        detail=True,
    )


@router.patch("/{psn}", response_model=TerminalDetailOut)
def update_terminal(
    psn: str,
    body: TerminalUpdateIn,
    session: SessionDep,
    settings: SettingsDep,
    _: UserDep,
) -> TerminalDetailOut:
    """Sửa tên / dung tích / toạ độ do người vận hành sở hữu. Cần phiên đăng nhập."""
    term = term_repo.update_operator(
        session,
        psn,
        name=body.name,
        capacity_l=body.capacity_l,
        # `location_sent` phân biệt "không gửi toạ độ" với "gửi null để xoá ghim".
        # Không có nó thì một ghim đặt sai không bao giờ bỏ được.
        location_sent=body.location_sent,
        latitude=body.latitude,
        longitude=body.longitude,
    )
    if term is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Terminal not found")
    session.commit()
    return to_terminal_out(
        term,
        tel_repo.latest_for(session, psn),
        now=datetime.now(tz=UTC),
        stale_after=timedelta(minutes=settings.online_stale_minutes),
        detail=True,
    )
