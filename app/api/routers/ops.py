"""Health, stats, alerts, admin — các endpoint vận hành."""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from sqlalchemy import text

from app.api.deps import AdminDep, SessionDep, SettingsDep, UserDep, to_terminal_out
from app.api.schemas import (
    ActionOut,
    AlertOut,
    CheckOut,
    HealthOut,
    IngestRunDetailOut,
    NotificationOut,
    SummaryOut,
    TerminalOut,
)
from app.domain.alerts import AlertThresholds, Severity, TerminalSnapshot, evaluate
from app.repositories import ingest_runs as runs_repo
from app.repositories import notifications as notif_repo
from app.repositories import telemetry as tel_repo
from app.repositories import terminals as term_repo
from app.services import notifier

log = logging.getLogger(__name__)
router = APIRouter(tags=["ops"])
UTC = ZoneInfo("UTC")
VERSION = "0.1.0"


def _thresholds(settings: SettingsDep) -> AlertThresholds:
    return AlertThresholds(
        stale_after=timedelta(minutes=settings.online_stale_minutes),
        low_volume_percent=Decimal(str(settings.alert_low_volume_percent)),
        low_battery_v=Decimal(str(settings.alert_low_battery_v)),
        low_signal_percent=Decimal(str(settings.alert_low_signal_percent)),
    )


def _snapshots(
    session: SessionDep, settings: SettingsDep
) -> tuple[list[TerminalOut], list[TerminalSnapshot], datetime]:
    now = datetime.now(tz=UTC)
    stale = timedelta(minutes=settings.online_stale_minutes)
    terms = term_repo.list_all(session)
    latest = tel_repo.latest_many(session, [t.psn for t in terms])
    outs = [
        to_terminal_out(t, latest.get(t.psn), now=now, stale_after=stale)
        for t in terms
    ]
    snaps = [
        TerminalSnapshot(
            psn=o.psn,
            last_seen_at=o.last_seen_at,
            volume_percent=o.volume_percent,
            fill_percent=o.fill_percent,
            battery_v=o.battery_v,
            signal_percent=o.signal_percent,
        )
        for o in outs
    ]
    return outs, snaps, now


@router.get("/health", response_model=HealthOut)
def health(
    request: Request, response: Response, session: SessionDep, settings: SettingsDep
) -> HealthOut:
    now = datetime.now(tz=UTC)
    overall = "ok"

    # --- database
    try:
        session.execute(text("SELECT 1"))
        db = CheckOut(ok=True)
    except Exception as exc:
        log.error("health: database không truy cập được: %s", exc)
        db = CheckOut(ok=False, detail="không kết nối được database")
        overall = "error"

    # --- migration
    mig = CheckOut(ok=True)
    if db.ok:
        try:
            current = session.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
            head = getattr(request.app.state, "alembic_head", None)
            if head and current != head:
                mig = CheckOut(ok=False, detail=f"schema ở {current}, code cần {head}")
                overall = "degraded" if overall == "ok" else overall
        except Exception:
            # Query lỗi làm abort transaction của Postgres; rollback để các check
            # sau (counts_by_status) không dính InFailedSqlTransaction rồi 500.
            session.rollback()
            mig = CheckOut(ok=False, detail="chưa có bảng alembic_version")
            overall = "degraded" if overall == "ok" else overall

    # --- ingest: đọc từ ingest_runs, KHÔNG suy từ MAX(telemetry.created_at).
    # Suy từ telemetry không phân biệt được "ingest chạy tốt, thiết bị offline" với
    # "ingest hỏng" — và cả hai thiết bị thật đang offline hàng tháng, nên cách đó
    # sẽ báo đỏ vĩnh viễn và không ai còn tin health nữa.
    last_at: datetime | None = None
    age: float | None = None
    paused = getattr(request.app.state, "ingest_paused_reason", None)
    ing = CheckOut(ok=True)
    if db.ok:
        try:
            run = runs_repo.last_success(session)
            if run is None or run.finished_at is None:
                ing = CheckOut(ok=False, detail="chưa có lần ingest thành công nào")
                overall = "degraded" if overall == "ok" else overall
            else:
                last_at = run.finished_at
                age = (now - last_at).total_seconds()
                budget = settings.ingest_interval_minutes * 60 * 3
                if age > budget:
                    ing = CheckOut(
                        ok=False,
                        detail=(
                            f"lần ingest cuối cách đây {age / 60:.0f} phút "
                            f"(ngưỡng {budget / 60:.0f} phút)"
                        ),
                    )
                    overall = "degraded" if overall == "ok" else overall
        except Exception as exc:
            ing = CheckOut(ok=False, detail=f"không đọc được ingest_runs: {exc}")
            overall = "degraded" if overall == "ok" else overall
    if paused:
        ing = CheckOut(ok=False, detail=f"job ingest đang bị tạm dừng: {paused}")
        overall = "degraded" if overall == "ok" else overall

    counts: dict[str, int] = {}
    if db.ok:
        try:
            counts = term_repo.counts_by_status(session)
        except Exception:
            # Bảng terminals chưa có (chưa migrate) hoặc lỗi khác — không để health 500.
            session.rollback()

    # 503 CHỈ khi error. degraded vẫn 200 để monitor phân biệt được "API sống nhưng
    # ingestion tắc" với "database mất" — gộp cả hai thành 503 là phá tín hiệu đó.
    if overall == "error":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthOut(
        status=overall,  # type: ignore[arg-type]
        version=VERSION,
        time=now.astimezone(settings.tzinfo),
        database=db,
        migration=mig,
        ingest=ing,
        # Số thiết bị online KHÔNG ảnh hưởng `status`: sức khoẻ thiết bị không phải
        # sức khoẻ platform.
        terminals_total=sum(counts.values()),
        terminals_online=counts.get("online", 0),
        terminals_offline=counts.get("offline", 0),
        last_ingest_at=last_at,
        last_ingest_age_seconds=age,
        scheduler_enabled=settings.scheduler_enabled,
        ingest_paused_reason=paused,
    )


@router.get("/stats/summary", response_model=SummaryOut)
def summary(session: SessionDep, settings: SettingsDep, _: UserDep) -> SummaryOut:
    outs, snaps, now = _snapshots(session, settings)
    th = _thresholds(settings)
    found = [a for s in snaps for a in evaluate(s, th, now)]
    volumes = [o.volume_l for o in outs if o.volume_l is not None]
    return SummaryOut(
        total=len(outs),
        online=sum(1 for o in outs if o.status == "online"),
        offline=sum(1 for o in outs if o.status == "offline"),
        # `alert` là số terminal CÓ cảnh báo — không phải số cảnh báo, và tuyệt đối
        # không phải "số offline" như placeholder của prototype.
        alert=len({a.psn for a in found}),
        critical=len({a.psn for a in found if a.severity is Severity.CRITICAL}),
        total_volume_l=sum(volumes, Decimal(0)) if volumes else None,
        generated_at=now.astimezone(settings.tzinfo),
    )


@router.get("/alerts", response_model=list[AlertOut])
def alerts(session: SessionDep, settings: SettingsDep, _: UserDep) -> list[AlertOut]:
    _, snaps, now = _snapshots(session, settings)
    th = _thresholds(settings)
    found = [a for s in snaps for a in evaluate(s, th, now)]
    order = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}
    found.sort(key=lambda a: (order[a.severity], a.psn))
    return [
        AlertOut(
            psn=a.psn,
            code=str(a.code),
            severity=str(a.severity),  # type: ignore[arg-type]
            message=a.message,
            value=a.value,
            threshold=a.threshold,
        )
        for a in found
    ]


@router.post("/admin/ingest/run", response_model=ActionOut)
def admin_run(request: Request, _: AdminDep) -> ActionOut:
    svc = request.app.state.ingestion
    stats = svc.run_cycle(trigger="api")
    return ActionOut(ok=not stats.errors, message=stats.summary())


@router.post("/admin/ingest/resume", response_model=ActionOut)
def admin_resume(request: Request, _: AdminDep) -> ActionOut:
    """Bật lại job sau khi đã sửa credential. Không cần restart process."""
    sched = getattr(request.app.state, "scheduler", None)
    request.app.state.ingest_paused_reason = None
    request.app.state.ingest_failures = 0
    if sched is None:
        return ActionOut(ok=False, message="scheduler đang tắt")
    sched.resume_job("ingest")
    return ActionOut(ok=True, message="job ingest đã được bật lại")


@router.get("/admin/ingest/runs", response_model=list[IngestRunDetailOut])
def admin_runs(
    session: SessionDep, _: AdminDep, limit: int = 20
) -> list[IngestRunDetailOut]:
    """Audit log ingest, KÈM mapping_report.

    Chỉ ở /admin/*: mapping_report chứa tên field vendor (``unmapped_keys``,
    ``resolved_from``) nên không được xuất hiện ở endpoint công khai.
    """
    return [
        IngestRunDetailOut.model_validate(r)
        for r in runs_repo.recent(session, limit=limit)
    ]


@router.post("/admin/db/sync", response_model=ActionOut)
def admin_db_sync(
    request: Request,
    session: SessionDep,
    _: AdminDep,
    stamp: Annotated[str | None, Query(max_length=64)] = None,
) -> ActionOut:
    """Áp schema còn thiếu lên DB. CHỈ tiến về phía trước — không có downgrade.

    Tồn tại vì một ràng buộc hạ tầng thật: máy trong mạng công ty mở được TCP 5432
    tới Neon nhưng handshake wire protocol bị firewall reset, nên ``alembic upgrade
    head`` không chạy được từ local. Trước đây việc này được làm bằng một endpoint
    tạm rồi gỡ đi — nghĩa là lần sau lại phải phát minh lại. Giữ nó lại, có guard,
    là lựa chọn trung thực hơn.

    Hai đường, ưu tiên đường đúng:

    1. Nếu ``alembic.ini`` + thư mục ``migrations`` có trong bundle -> chạy
       ``alembic upgrade head`` THẬT. Đây là đường duy nhất xử lý được ALTER.
    2. Nếu không -> ``create_all(checkfirst=True)``: tạo bảng còn thiếu, KHÔNG
       chạm bảng đã có. Đủ cho migration thuần bổ sung bảng (như
       ``notifications``), và **không đủ** cho migration có ALTER COLUMN — lúc đó
       phải chạy alembic từ một mạng nói được Postgres tới Neon.

    ``stamp`` cập nhật ``alembic_version`` sau đường 2, để một lần chạy alembic
    thật về sau không cố tạo lại bảng đã có.
    """
    engine = request.app.state.engine

    ini = Path(__file__).resolve().parents[3] / "alembic.ini"
    if ini.is_file():
        try:
            from alembic import command
            from alembic.config import Config

            command.upgrade(Config(str(ini)), "head")
            return ActionOut(ok=True, message="alembic upgrade head: xong")
        except Exception as exc:
            log.error("admin/db/sync: alembic upgrade thất bại: %s", exc)
            # Rơi xuống đường 2 thay vì bỏ cuộc: create_all vẫn có thể cứu được
            # trường hợp migration chỉ thêm bảng.

    from sqlalchemy import inspect as sa_inspect

    from app.db.base import Base

    before = set(sa_inspect(engine).get_table_names())
    Base.metadata.create_all(bind=engine, checkfirst=True)
    created = sorted(set(sa_inspect(engine).get_table_names()) - before)

    stamped = ""
    if stamp:
        session.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version "
                 "(version_num VARCHAR(32) NOT NULL "
                 "CONSTRAINT alembic_version_pkc PRIMARY KEY)")
        )
        session.execute(text("DELETE FROM alembic_version"))
        session.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
            {"v": stamp},
        )
        session.commit()
        stamped = f", alembic_version = {stamp}"

    return ActionOut(
        ok=True,
        message=f"create_all: tạo mới {created or 'không có bảng nào'}{stamped}",
    )


@router.get("/admin/notifications", response_model=list[NotificationOut])
def admin_notifications(
    session: SessionDep,
    _: AdminDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    psn: Annotated[str | None, Query(max_length=32)] = None,
) -> list[NotificationOut]:
    """Nhật ký thông báo đã gửi — trả lời "đã báo cho ai, lúc nào, nội dung gì"."""
    return [
        NotificationOut.model_validate(r)
        for r in notif_repo.recent(session, limit=limit, psn=psn)
    ]


@router.post("/admin/notify/run", response_model=ActionOut)
def admin_notify_run(
    session: SessionDep, settings: SettingsDep, _: AdminDep
) -> ActionOut:
    """Chạy vòng gửi cảnh báo ngay. Dùng để kiểm tra cấu hình SMTP thật.

    Vẫn đi qua cửa chặn gửi lại: nếu vừa gửi cách đây một giờ thì lệnh này sẽ báo
    "bị chặn" chứ không gửi lại. Đó là hành vi đúng — một nút test bỏ qua cửa chặn
    sẽ là nút spam.
    """
    stats = notifier.notify(session, settings, datetime.now(tz=UTC))
    session.commit()
    return ActionOut(ok=stats.failed == 0, message=stats.summary())


@router.get("/cron/ingest", response_model=ActionOut, include_in_schema=False)
def cron_ingest(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
) -> ActionOut:
    """Kích hoạt một vòng ingest từ Vercel Cron.

    Thay cho APScheduler trên serverless (Vercel không giữ process chạy nền). Vercel
    Cron gọi GET kèm header ``Authorization: Bearer <CRON_SECRET>`` tự động khi biến
    môi trường CRON_SECRET được set. Không có secret => từ chối, để endpoint này
    không thành một nút bấm ingest công khai.
    """
    secret = os.environ.get("CRON_SECRET")
    if not secret:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "CRON_SECRET chưa cấu hình; cron ingest bị tắt",
        )
    if authorization != f"Bearer {secret}":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unauthorized")

    svc = getattr(request.app.state, "ingestion", None)
    if svc is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ingestion chưa sẵn sàng (thiếu cấu hình vendor)",
        )
    try:
        # trigger='scheduler': đây là đường ingest định kỳ; CHECK của ingest_runs chỉ
        # cho phép scheduler|cli|api nên dùng lại 'scheduler' cho đúng ngữ nghĩa.
        stats = svc.run_cycle(trigger="scheduler")
    except Exception as exc:  # gồm cả FatalIngestError (session hết hạn)
        log.error("cron ingest thất bại: %s", exc)
        return ActionOut(ok=False, message=f"ingest thất bại: {type(exc).__name__}")

    # Gửi cảnh báo NGAY SAU khi ingest, trong cùng lần gọi cron: đây là chỗ duy
    # nhất trên serverless có sẵn dữ liệu vừa mới nhất và một tiến trình đang
    # chạy. Bọc try/except riêng vì email không bao giờ được phép làm một vòng
    # ingest thành công trông như thất bại — dữ liệu quan trọng hơn thông báo.
    notify_msg = ""
    try:
        n = notifier.notify(session, settings, datetime.now(tz=UTC))
        session.commit()
        notify_msg = f" | {n.summary()}"
    except Exception as exc:
        session.rollback()
        log.error("cron: gửi cảnh báo thất bại: %s", exc)
        notify_msg = f" | cảnh báo lỗi: {type(exc).__name__}"

    return ActionOut(ok=not stats.errors, message=stats.summary() + notify_msg)


# Trần số ngày mỗi lần backfill: mỗi ngày là 1 HTTP call/PSN có throttle ~1s, mà
# function serverless có trần thời gian (maxDuration). Chia nhỏ để không timeout.
_BACKFILL_MAX_DAYS = 20


@router.post("/admin/backfill", response_model=ActionOut)
def admin_backfill(
    request: Request,
    _: AdminDep,
    from_: Annotated[str, Query(alias="from")],
    to: Annotated[str, Query()],
    psn: Annotated[str | None, Query()] = None,
) -> ActionOut:
    """Kéo lịch sử THẬT từ vendor về DB cho khoảng ngày [from, to] (giờ vendor).

    Chạy từ server (Vercel) vì máy local bị firewall chặn Postgres tới Neon. Endpoint
    vận hành chính thức: dùng lại IngestionService.backfill (idempotent, ON CONFLICT
    DO NOTHING) nên gọi lại an toàn. Mỗi lần tối đa 20 ngày để không timeout serverless.
    """
    svc = getattr(request.app.state, "ingestion", None)
    if svc is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ingestion chưa sẵn sàng (thiếu cấu hình vendor)",
        )
    try:
        start = date.fromisoformat(from_)
        end = date.fromisoformat(to)
    except ValueError as exc:
        raise HTTPException(422, "from/to phải dạng YYYY-MM-DD") from exc
    if end < start:
        raise HTTPException(422, "from phải <= to")
    if (end - start).days + 1 > _BACKFILL_MAX_DAYS:
        raise HTTPException(
            422,
            f"khoảng > {_BACKFILL_MAX_DAYS} ngày; chia nhỏ để tránh timeout serverless",
        )

    psns = [psn] if psn else list(getattr(svc, "_configured_psns", None) or [])
    if not psns:
        raise HTTPException(422, "không có PSN nào để backfill (thiếu XINGKE_ALLOWED_PSNS)")
    try:
        stats = svc.backfill(psns, start, end, trigger="api")
    except Exception as exc:  # gồm cả FatalIngestError
        log.error("backfill thất bại: %s", exc)
        return ActionOut(ok=False, message=f"backfill thất bại: {type(exc).__name__}")
    return ActionOut(ok=not stats.errors, message=stats.summary())
