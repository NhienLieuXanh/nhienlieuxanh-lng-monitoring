"""Dự báo cạn, boil-off / hold time, nhật ký nạp, lịch giao. Đọc-thuần.

Không endpoint nào ở đây ghi vào DB. Toàn bộ phép tính nằm ở
``app/domain/forecast.py`` (hàm thuần, có test riêng); router chỉ lấy dữ liệu,
gọi domain, rồi map sang response model.

Tham số mô hình (cửa sổ lịch sử, lead time, mức phục vụ, áp van an toàn) đều cho
override qua query param nhưng **mặc định lấy từ Settings**. Lý do: người vận
hành cần thử "nếu lead time 2 ngày thì sao" mà không phải sửa .env và deploy lại,
nhưng con số dùng hằng ngày thì phải là cấu hình chứ không phải thứ ai cũng gõ
mỗi lần và mỗi người gõ một kiểu.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import SessionDep, SettingsDep, UserDep
from app.api.schemas import (
    AlertOut,
    ConsumptionOut,
    DeliveryPlanOut,
    DeliveryStopOut,
    DeliveryTripOut,
    ForecastOut,
    HoldTimeOut,
    IdleTrendOut,
    RefillOut,
    RunoutOut,
    SuggestionOut,
)
from app.db.models import Telemetry, Terminal
from app.domain import forecast as fc
from app.domain.status import derive_status
from app.repositories import telemetry as tel_repo
from app.repositories import terminals as term_repo
from app.services.appconfig import ConfigLike, load_config

log = logging.getLogger(__name__)
router = APIRouter(tags=["forecast"])
UTC = ZoneInfo("UTC")


# --------------------------------------------------------------------------- #
# Tham số mô hình
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ForecastParams:
    window_days: int
    reserve_l: float | None
    lead_time_days: float
    service_level: int
    relief_mpa: float
    max_fill_percent: float
    reserve_percent: float


def forecast_params(
    session: SessionDep,
    settings: SettingsDep,
    window_days: Annotated[int | None, Query(ge=1, le=365)] = None,
    reserve_l: Annotated[float | None, Query(ge=0)] = None,
    lead_time_days: Annotated[float | None, Query(ge=0, le=30)] = None,
    service_level: Annotated[int | None, Query()] = None,
    relief_mpa: Annotated[float | None, Query(gt=0, le=10)] = None,
    max_fill_percent: Annotated[float | None, Query(gt=0, le=100)] = None,
) -> ForecastParams:
    # Ngưỡng mặc định lấy từ cấu hình HIỆU LỰC (trang Cài đặt ghi đè .env), không
    # phải từ .env trực tiếp — nếu không thì đổi lead time trong app sẽ không có
    # tác dụng và người dùng không hiểu tại sao.
    cfg = load_config(session, settings)
    sl = service_level if service_level is not None else cfg.forecast_service_level
    if sl not in fc.Z_BY_SERVICE_LEVEL:
        # 422 chứ không im lặng lấy giá trị gần nhất: z-score sai làm dự trữ an
        # toàn sai, và đó là loại lỗi không ai phát hiện được từ con số đầu ra.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"service_level phải thuộc {sorted(fc.Z_BY_SERVICE_LEVEL)}",
        )
    return ForecastParams(
        window_days=window_days or cfg.forecast_window_days,
        reserve_l=reserve_l,
        lead_time_days=(
            lead_time_days
            if lead_time_days is not None
            else cfg.forecast_lead_time_days
        ),
        service_level=sl,
        relief_mpa=relief_mpa or cfg.lng_relief_pressure_mpa,
        max_fill_percent=max_fill_percent or cfg.lng_max_fill_percent,
        reserve_percent=cfg.forecast_reserve_percent,
    )


ParamsDep = Annotated[ForecastParams, Depends(forecast_params)]


# --------------------------------------------------------------------------- #
# Dựng dự báo
# --------------------------------------------------------------------------- #


def _samples(
    session: SessionDep, psn: str, *, now: datetime, window_days: int
) -> list[fc.Sample]:
    rows = tel_repo.series(
        session, psn, now - timedelta(days=window_days), now, bucket_minutes=30
    )
    return [fc.Sample(at=at, volume_l=v, pressure_mpa=p) for at, v, p in rows]


def _build(
    session: SessionDep,
    settings: ConfigLike,
    term: Terminal,
    latest: Telemetry | None,
    p: ForecastParams,
    now: datetime,
) -> fc.Forecast:
    cap = None if term.capacity_l is None else float(term.capacity_l)
    vol = None
    pres = None
    if latest is not None:
        vol = None if latest.volume_l is None else float(latest.volume_l)
        pres = None if latest.pressure_mpa is None else float(latest.pressure_mpa)
    return fc.build_forecast(
        _samples(session, term.psn, now=now, window_days=p.window_days),
        psn=term.psn,
        volume_l=vol,
        capacity_l=cap,
        pressure_mpa=pres,
        now=now,
        tz=settings.tzinfo,
        reserve_percent=p.reserve_percent,
        reserve_l=p.reserve_l,
        lead_time_days=p.lead_time_days,
        service_level=p.service_level,
        relief_mpa=p.relief_mpa,
        max_fill_percent=p.max_fill_percent,
        reading_at=latest.sampled_at if latest else None,
        max_reading_age_days=settings.forecast_max_reading_age_hours / 24.0,
    )


def _to_out(
    f: fc.Forecast,
    term: Terminal,
    latest: Telemetry | None,
    *,
    now: datetime,
    stale_after: timedelta,
) -> ForecastOut:
    return ForecastOut(
        psn=f.psn,
        name=term.name,
        # Suy status lúc đọc, giống deps.to_terminal_out — cột `terminals.status`
        # là cache và có lỗi staleness không tránh được.
        status=derive_status(term.last_seen_at, now, stale_after).value,  # type: ignore[arg-type]
        sampled_at=latest.sampled_at if latest else None,
        volume_l=f.volume_l,
        capacity_l=f.capacity_l,
        fill_percent=f.fill_percent,
        reserve_l=f.reserve_l,
        consumption=ConsumptionOut.model_validate(f.consumption),
        idle=IdleTrendOut.model_validate(f.idle),
        runout=RunoutOut.model_validate(f.runout),
        hold=HoldTimeOut.model_validate(f.hold),
        suggestion=SuggestionOut.model_validate(f.suggestion),
        # Mới nhất lên đầu: đây là nhật ký, người đọc quan tâm lần nạp vừa rồi.
        reading_age_days=f.reading_age_days,
        stale=f.stale,
        refills=[RefillOut.model_validate(r) for r in reversed(f.refills)],
        alerts=[
            AlertOut(
                psn=a.psn,
                code=a.code,
                severity=a.severity,  # type: ignore[arg-type]
                message=a.message,
                value=a.value,  # type: ignore[arg-type]
                threshold=a.threshold,  # type: ignore[arg-type]
            )
            for a in f.alerts
        ],
        generated_at=f.generated_at,
    )


def _get_term(session: SessionDep, psn: str) -> Terminal:
    term = term_repo.get_by_psn(session, psn)
    if term is None:
        # 404 cho PSN lạ ở MỌI route /{psn}: trả 200 rỗng làm một PSN gõ sai không
        # phân biệt được với "bồn này chưa có dữ liệu".
        raise HTTPException(status.HTTP_404_NOT_FOUND, "PSN không tồn tại")
    return term


# --------------------------------------------------------------------------- #
# Endpoint
# --------------------------------------------------------------------------- #


@router.get("/forecast", response_model=list[ForecastOut])
def forecast_all(
    session: SessionDep, settings: SettingsDep, p: ParamsDep, _: UserDep
) -> list[ForecastOut]:
    """Dự báo cho mọi bồn. Dashboard gọi endpoint này để hiện "còn N ngày"."""
    now = datetime.now(tz=UTC)
    cfg = load_config(session, settings)
    stale = timedelta(minutes=cfg.online_stale_minutes)
    terms = term_repo.list_all(session)
    latest = tel_repo.latest_many(session, [t.psn for t in terms])
    out: list[ForecastOut] = []
    for t in terms:
        f = _build(session, cfg, t, latest.get(t.psn), p, now)
        out.append(_to_out(f, t, latest.get(t.psn), now=now, stale_after=stale))
    return out


@router.get("/forecast/{psn}", response_model=ForecastOut)
def forecast_one(
    psn: str, session: SessionDep, settings: SettingsDep, p: ParamsDep, _: UserDep
) -> ForecastOut:
    now = datetime.now(tz=UTC)
    cfg = load_config(session, settings)
    term = _get_term(session, psn)
    latest = tel_repo.latest_for(session, psn)
    f = _build(session, cfg, term, latest, p, now)
    return _to_out(
        f, term, latest, now=now,
        stale_after=timedelta(minutes=cfg.online_stale_minutes),
    )


@router.get("/refills/{psn}", response_model=list[RefillOut])
def refills(
    psn: str,
    session: SessionDep,
    settings: SettingsDep,
    _: UserDep,
    window_days: Annotated[int, Query(ge=1, le=365)] = 90,
) -> list[RefillOut]:
    """Nhật ký nạp suy từ telemetry — không ai phải nhập tay, nên không sai lệch.

    Cửa sổ mặc định 90 ngày (rộng hơn cửa sổ dự báo 30 ngày) vì nhật ký là thứ
    người ta tra ngược về quá khứ, khác với dự báo chỉ quan tâm hiện tại.
    """
    now = datetime.now(tz=UTC)
    term = _get_term(session, psn)
    cap = None if term.capacity_l is None else float(term.capacity_l)
    samples = _samples(session, psn, now=now, window_days=window_days)
    events = fc.detect_refills(samples, capacity_l=cap)
    return [RefillOut.model_validate(e) for e in reversed(events)]


@router.get("/delivery-plan", response_model=DeliveryPlanOut)
def delivery_plan(
    session: SessionDep,
    settings: SettingsDep,
    p: ParamsDep,
    _: UserDep,
    truck_capacity_l: Annotated[float | None, Query(gt=0)] = None,
    horizon_days: Annotated[float, Query(gt=0, le=90)] = 7.0,
) -> DeliveryPlanOut:
    """Gom các bồn cần nạp trong ``horizon_days`` thành chuyến theo tải xe."""
    now = datetime.now(tz=UTC)
    cfg = load_config(session, settings)
    truck = truck_capacity_l or cfg.truck_capacity_l
    terms = term_repo.list_all(session)
    latest = tel_repo.latest_many(session, [t.psn for t in terms])
    forecasts = [_build(session, cfg, t, latest.get(t.psn), p, now) for t in terms]
    names = {t.psn: t.name for t in terms}
    trips = fc.plan_trips(
        forecasts, truck_capacity_l=truck, horizon_days=horizon_days, names=names
    )
    return DeliveryPlanOut(
        truck_capacity_l=truck,
        horizon_days=horizon_days,
        trips=[
            DeliveryTripOut(
                seq=t.seq,
                stops=[DeliveryStopOut.model_validate(s) for s in t.stops],
                total_l=t.total_l,
                truck_capacity_l=t.truck_capacity_l,
            )
            for t in trips
        ],
        total_l=sum(t.total_l for t in trips),
        stops=sum(len(t.stops) for t in trips),
        generated_at=now,
    )
