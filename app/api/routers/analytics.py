"""Phân tích: chất lượng dữ liệu, sức khoẻ thiết bị, bất thường. Đọc-thuần.

Không endpoint nào ở đây ghi vào DB. Toàn bộ phép tính nằm ở
``app/domain/analytics.py`` (hàm thuần, có test riêng); router chỉ lấy dữ liệu, gọi
domain, rồi map sang response model — cùng khuôn với ``routers/forecast.py``.

Cửa sổ mặc định 30 ngày, KHÁC cửa sổ dự báo. Suy pin và tỉ lệ mất mẫu là hiện tượng
hàng tuần: 7 ngày không đủ để thấy độ dốc, còn 90 ngày thì trộn giai đoạn trước và
sau một lần thay pin thành một đường trung bình vô nghĩa.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import SessionDep, SettingsDep, UserDep
from app.api.schemas import (
    AnalyticsOut,
    AnomalyOut,
    BatteryOut,
    DeviceHealthOut,
    QualityOut,
    SignalOut,
)
from app.domain import analytics as an
from app.domain.forecast import Sample
from app.repositories import telemetry as tel_repo
from app.repositories import terminals as term_repo
from app.services.appconfig import ConfigLike, load_config

router = APIRouter(tags=["analytics"])
UTC = ZoneInfo("UTC")

WindowDays = Annotated[float, Query(gt=0, le=180, description="Cửa sổ phân tích")]
DEFAULT_WINDOW_DAYS = 30.0


def _build(
    session: SessionDep,
    cfg: ConfigLike,
    psn: str,
    name: str | None,
    capacity_l: float | None,
    *,
    window_days: float,
    now: datetime,
) -> AnalyticsOut:
    start = an.window_start(now, window_days)

    vol = [
        Sample(at=at, volume_l=v, pressure_mpa=p)
        for at, v, p in tel_repo.series(session, psn, start, now)
    ]
    hs = [
        an.HealthSample(at=at, battery_v=b, signal_percent=s)
        for at, b, s in tel_repo.health_series(session, psn, start, now)
    ]

    quality = an.assess_quality(vol, now=now, window_days=window_days)
    # Ngưỡng pin/sóng lấy từ cấu hình vận hành, KHÔNG hard-code: chúng đã là ngưỡng
    # cảnh báo của hệ thống, nên trang phân tích phải nói cùng một con số. Hai chỗ
    # dùng hai ngưỡng khác nhau là cách chắc nhất để mất lòng tin vào cả hai.
    health = an.assess_device_health(
        hs,
        psn=psn,
        now=now,
        warn_v=float(cfg.alert_low_battery_v),
        floor_percent=float(cfg.alert_low_signal_percent),
    )
    anomalies = an.detect_anomalies(vol, capacity_l=capacity_l)

    pts = [s for s in vol if s.volume_l is not None]
    regimes = [pts[i].at for i in an.change_points(vol) if 0 <= i < len(pts)]

    return AnalyticsOut(
        psn=psn,
        name=name,
        capacity_l=capacity_l,
        window_days=window_days,
        quality=QualityOut(**asdict(quality)),
        health=DeviceHealthOut(
            psn=health.psn,
            name=name,
            samples=health.samples,
            battery=BatteryOut(**asdict(health.battery)),
            signal=SignalOut(**asdict(health.signal)),
            delivery_ratio=health.delivery_ratio,
            delivery_trend_per_day=health.delivery_trend_per_day,
            silent_days=health.silent_days,
            risk=health.risk,
            likely_cause=health.likely_cause,
            days_to_failure=health.days_to_failure,
            reasons=health.reasons,
        ),
        anomalies=[AnomalyOut(**asdict(a)) for a in anomalies],
        regime_changes=regimes,
        generated_at=now,
    )


#: Thứ tự ưu tiên khi xếp danh sách. "chưa đủ dữ liệu" đứng TRÊN "thấp" có chủ ý:
#: không biết gì về một thiết bị là một việc cần xử lý, không phải một tin tốt.
_RISK_ORDER = {"cao": 0, "trung bình": 1, "chưa đủ dữ liệu": 2, "thấp": 3}


@router.get("/analytics", response_model=list[AnalyticsOut])
def analytics_all(
    session: SessionDep,
    settings: SettingsDep,
    _: UserDep,
    window_days: WindowDays = DEFAULT_WINDOW_DAYS,
) -> list[AnalyticsOut]:
    """Phân tích mọi bồn, bồn rủi ro cao lên trước.

    Sắp theo rủi ro chứ không theo PSN: trang này tồn tại để trả lời "cần đi hiện
    trường ở đâu trước", và một danh sách theo số serial không trả lời câu đó.
    """
    now = datetime.now(tz=UTC)
    cfg = load_config(session, settings)
    out = [
        _build(
            session,
            cfg,
            t.psn,
            t.name,
            float(t.capacity_l) if t.capacity_l is not None else None,
            window_days=window_days,
            now=now,
        )
        for t in term_repo.list_all(session)
    ]
    return sorted(
        out,
        key=lambda a: (
            _RISK_ORDER.get(a.health.risk, 9),
            a.health.days_to_failure if a.health.days_to_failure is not None else 1e9,
        ),
    )


@router.get("/analytics/{psn}", response_model=AnalyticsOut)
def analytics_one(
    session: SessionDep,
    settings: SettingsDep,
    psn: str,
    _: UserDep,
    window_days: WindowDays = DEFAULT_WINDOW_DAYS,
) -> AnalyticsOut:
    term = term_repo.get_by_psn(session, psn)
    if term is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "không có bồn với PSN này")
    return _build(
        session,
        load_config(session, settings),
        term.psn,
        term.name,
        float(term.capacity_l) if term.capacity_l is not None else None,
        window_days=window_days,
        now=datetime.now(tz=UTC),
    )
