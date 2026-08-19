"""APScheduler + job wrapper. Chỗ DUY NHẤT catch rộng.

Exception không bắt trong một job APScheduler KHÔNG kill process — nhưng nó fire lại
mỗi interval mãi mãi. Với lỗi auth, điều đó nghĩa là đập liên tục vào một vendor
đang từ chối mình, trên một account mà vendor có bảng audit login. Vì vậy job này
PAUSE chính nó khi gặp lỗi fatal, và API vẫn sống.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from sqlalchemy import text

from app.config import Settings
from app.services.ingestion import FatalIngestError

log = logging.getLogger(__name__)

JOB_ID = "ingest"
# Khoá advisory: chống hai process cùng ingest. hashtext() cần một int64.
LOCK_KEY_SQL = text("SELECT pg_try_advisory_xact_lock(hashtext('lng_ingest'))")


def ingest_job(app: FastAPI) -> None:
    """Một vòng ingest. Không bao giờ để exception thoát ra scheduler."""
    settings: Settings = app.state.settings
    svc = app.state.ingestion
    sched: AsyncIOScheduler | None = getattr(app.state, "scheduler", None)

    # Belt-and-braces cho trường hợp có nhiều instance: lock tự release khi
    # transaction kết thúc, nên một process crash không kẹt lock mãi mãi.
    try:
        with app.state.session_factory() as session, session.begin():
            if not session.execute(LOCK_KEY_SQL).scalar():
                log.info("ingest: bỏ qua, một process khác đang giữ lock")
                return
    except Exception as exc:
        log.warning("ingest: không lấy được advisory lock (%s); vẫn tiếp tục", exc)

    try:
        stats = svc.run_cycle(trigger="scheduler")
    except FatalIngestError as exc:
        # Auth chết theo định nghĩa là không retry được, nên retry chỉ gây hại.
        log.critical(
            "ingest ĐÃ BỊ TẠM DỪNG: %s%s",
            exc.detail,
            f" | Cách xử lý: {exc.remediation}" if exc.remediation else "",
        )
        app.state.ingest_paused_reason = "vendor_auth_failed"
        if sched is not None:
            # pause_job, KHÔNG remove_job: để resume_job hoạt động mà không phải
            # dựng lại scheduler.
            sched.pause_job(JOB_ID)
        return
    except Exception:
        log.exception("ingest: vòng chạy thất bại ngoài dự kiến")
        app.state.ingest_failures = getattr(app.state, "ingest_failures", 0) + 1
        if app.state.ingest_failures >= settings.ingest_max_consecutive_failures:
            app.state.ingest_paused_reason = "too_many_consecutive_failures"
            if sched is not None:
                sched.pause_job(JOB_ID)
            log.critical(
                "ingest ĐÃ BỊ TẠM DỪNG sau %s lần thất bại liên tiếp",
                app.state.ingest_failures,
            )
        return

    # Chỉ reset counter khi cycle KHÔNG có lỗi. `psns_no_data` không tính là lỗi —
    # nếu tính thì thực tế "cả hai thiết bị offline hàng tháng" sẽ trip circuit
    # breaker ngay vòng đầu.
    app.state.ingest_failures = 0 if not stats.errors else (
        getattr(app.state, "ingest_failures", 0) + 1
    )


def build_scheduler(app: FastAPI) -> AsyncIOScheduler:
    settings: Settings = app.state.settings
    sched = AsyncIOScheduler(
        timezone=settings.tzinfo,
        job_defaults={
            # Một vòng chạy chậm không bao giờ overlap tick sau.
            "max_instances": 1,
            # Sau khi laptop sleep: chạy MỘT lần, không replay mọi tick đã mất.
            "coalesce": True,
            # Bỏ qua tick trễ hơn một phút thay vì fire một loạt dồn.
            "misfire_grace_time": 60,
        },
    )
    kwargs: dict[str, object] = {}
    if settings.ingest_on_startup:
        kwargs["next_run_time"] = datetime.now(tz=settings.tzinfo) + timedelta(
            seconds=10
        )

    sched.add_job(
        ingest_job,
        "interval",
        id=JOB_ID,
        replace_existing=True,
        minutes=settings.ingest_interval_minutes,
        # Không đập đúng đầu phút cùng mọi khách hàng khác của vendor.
        jitter=settings.ingest_jitter_seconds,
        args=[app],
        **kwargs,
    )

    def _on_problem(event: object) -> None:
        log.error("scheduler: job gặp sự cố: %r", event)

    sched.add_listener(_on_problem, EVENT_JOB_ERROR | EVENT_JOB_MISSED)
    return sched
