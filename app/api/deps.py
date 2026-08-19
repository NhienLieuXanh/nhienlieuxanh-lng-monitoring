"""Dependency của FastAPI: session DB, query param, admin guard, mapper."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.api.schemas import TerminalDetailOut, TerminalOut
from app.config import Settings, get_settings
from app.db.models import Telemetry, Terminal
from app.domain.alerts import fill_percent
from app.domain.status import derive_status

UTC = ZoneInfo("UTC")


def get_db(request: Request) -> Iterator[Session]:
    factory = request.app.state.session_factory
    session = factory()
    try:
        yield session
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def require_admin(
    settings: SettingsDep,
    x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
) -> None:
    """Guard cho /api/admin/*.

    Guard riêng cho /api/admin/*. Dashboard dùng session cookie (UserDep);
    token này không được nhúng vào JS.
    """
    expected = settings.admin_token
    if not expected:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ADMIN_TOKEN chưa được cấu hình; endpoint admin bị tắt",
        )
    if not x_admin_token or x_admin_token != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "X-Admin-Token không hợp lệ")


AdminDep = Annotated[None, Depends(require_admin)]


def require_user(request: Request) -> str:
    """Phiên đăng nhập của người xem dashboard. Health / login vẫn mở."""
    user = request.session.get("user")
    if not user or not isinstance(user, str):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "cần đăng nhập")
    return user


UserDep = Annotated[str, Depends(require_user)]


class HistoryQuery(BaseModel):
    """Tham số của /api/telemetry/{psn}.

    ``from`` là keyword Python nên field phải tên ``from_`` với alias.
    """

    model_config = ConfigDict(populate_by_name=True)

    from_: datetime | None = Field(None, alias="from")
    to: datetime | None = None
    page: int = Field(1, ge=1)
    limit: int = Field(100, ge=1, le=1000)
    order: Literal["asc", "desc"] = "desc"

    @model_validator(mode="after")
    def _check_range(self) -> HistoryQuery:
        if self.from_ and self.to and self.from_ > self.to:
            raise ValueError("`from` phải <= `to`")
        return self

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.limit

    @property
    def ascending(self) -> bool:
        return self.order == "asc"


def history_query(
    settings: SettingsDep,
    q: Annotated[HistoryQuery, Query()],
) -> HistoryQuery:
    """Điền default và localize datetime naive theo APP_TZ.

    Naive -> Asia/Ho_Chi_Minh, KHÔNG phải UTC. Người Việt gõ
    `from=2026-07-23T00:00` là muốn nửa đêm giờ Hà Nội; coi nó là UTC làm lệch 7
    giờ mọi cửa sổ và "hôm nay" trông sai mà không ai hiểu tại sao.
    """
    tz = settings.tzinfo
    to = q.to or datetime.now(tz=UTC)
    frm = q.from_ or (to - timedelta(hours=24))
    if frm.tzinfo is None:
        frm = frm.replace(tzinfo=tz)
    if to.tzinfo is None:
        to = to.replace(tzinfo=tz)
    if frm > to:
        raise HTTPException(422, "`from` phải <= `to`")

    span_days = (to - frm).total_seconds() / 86400.0
    if span_days > settings.max_history_span_days:
        # 422 chứ không phải cắt bớt im lặng: `total` cần COUNT(*) trên cả khoảng,
        # nên một khoảng vô hạn là một câu query chậm chứ không phải một câu hỏi hay.
        raise HTTPException(
            422,
            f"khoảng thời gian {span_days:.0f} ngày vượt giới hạn "
            f"{settings.max_history_span_days} ngày",
        )

    return HistoryQuery(
        from_=frm, to=to, page=q.page,
        limit=min(q.limit, settings.max_history_limit), order=q.order,
    )


HistoryQueryDep = Annotated[HistoryQuery, Depends(history_query)]


def to_terminal_out(
    term: Terminal,
    latest: Telemetry | None,
    *,
    now: datetime,
    stale_after: timedelta,
    detail: bool = False,
) -> TerminalOut:
    """Map ORM -> response, suy `status` lúc ĐỌC.

    Suy lại thay vì đọc cột `terminals.status`: cột đó là cache và có lỗi staleness
    không tránh được (thiết bị ngừng báo thì không ingest nào chạm row nó). Suy ở
    đây thì status không bao giờ sai, bất kể ingest chạy lần cuối khi nào.
    """
    fill = fill_percent(
        latest.volume_l if latest else None,
        term.capacity_l,
    )
    payload: dict[str, object] = {
        "psn": term.psn,
        "name": term.name,
        "status": derive_status(term.last_seen_at, now, stale_after).value,
        "last_seen_at": term.last_seen_at,
        "capacity_l": term.capacity_l,
        "medium_name": term.medium_name or (latest.medium_name if latest else None),
        "tank_type_name": term.tank_type_name
        or (latest.tank_type_name if latest else None),
        "volume_l": latest.volume_l if latest else None,
        "volume_percent": latest.volume_percent if latest else None,
        "fill_percent": _round2(fill),
        "pressure_mpa": latest.pressure_mpa if latest else None,
        "temperature_c": latest.temperature_c if latest else None,
        "battery_v": latest.battery_v if latest else None,
        "signal_percent": latest.signal_percent if latest else None,
        "level_mmwc": latest.level_mmwc if latest else None,
        "diff_pressure_kpa": latest.diff_pressure_kpa if latest else None,
        "vacuum_pa": latest.vacuum_pa if latest else None,
        "sampled_at": latest.sampled_at if latest else None,
    }
    if not detail:
        return TerminalOut(**payload)  # type: ignore[arg-type]
    return TerminalDetailOut(
        **payload,  # type: ignore[arg-type]
        id=term.id,
        modem_number=term.modem_number,
        sim_iccid=term.sim_iccid,
        hardware_version=term.hardware_version,
        software_version=term.software_version,
        device_model=term.device_model,
        device_type_name=term.device_type_name,
        created_at=term.created_at,
        updated_at=term.updated_at,
    )


def _round2(v: Decimal | None) -> Decimal | None:
    return None if v is None else v.quantize(Decimal("0.01"))
