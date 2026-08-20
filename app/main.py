"""FastAPI app + lifespan. Adapter được khởi tạo ĐÚNG MỘT LẦN ở đây."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api import errors
from app.api.routers import api_router
from app.config import get_settings
from app.db.session import make_engine, make_session_factory
from app.factory import build_adapter
from app.logging_config import setup_logging
from app.scheduler import build_scheduler
from app.services.ingestion import IngestionService

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def _alembic_head() -> str | None:
    """Revision head mà CODE này mong đợi.

    So với alembic_version trong DB ở /api/health để bắt lớp bug "quên migrate" —
    thứ bình thường chỉ lộ ra dưới dạng UndefinedColumn lúc 3 giờ sáng.
    """
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
        return ScriptDirectory.from_config(cfg).get_current_head()
    except Exception as exc:
        log.debug("không xác định được alembic head: %s", exc)
        return None


def _init_state(app: FastAPI) -> None:
    """Khởi tạo state idempotent.

    Gọi được từ CẢ create_app() (lúc import — để chạy trên môi trường serverless
    như Vercel, nơi ``lifespan`` có thể KHÔNG được chạy) lẫn ``lifespan`` (server
    thường). Lần gọi thứ hai là no-op. ``create_engine`` là lazy nên gọi lúc import
    KHÔNG mở kết nối DB — an toàn.
    """
    if getattr(app.state, "_state_ready", False):
        return

    settings = get_settings()
    setup_logging(settings.log_level)

    # Cảnh báo phải nằm SAU setup_logging, nếu không nó fire trước khi handler log
    # được lắp và không bao giờ vào log — lưới an toàn im lặng.
    if settings.session_secret == "change-me-session-secret":
        log.warning(
            "SESSION_SECRET đang dùng giá trị mặc định công khai — ai biết default "
            "cũng forge được cookie nlx_session và bỏ qua đăng nhập. Đặt secret "
            "riêng trước khi cho máy khác truy cập."
        )

    app.state.settings = settings
    app.state.engine = make_engine(settings)
    app.state.session_factory = make_session_factory(app.state.engine)
    app.state.alembic_head = _alembic_head()
    app.state.ingest_failures = 0
    app.state.ingest_paused_reason = None
    app.state.scheduler = None

    # Adapter có thể fail nếu thiếu credential vendor. KHÔNG để nó làm chết cả
    # API/dashboard — chỉ ingestion không dùng được cho tới khi cấu hình xong.
    try:
        adapter, fatal_types, psns = build_adapter(settings)
        app.state.adapter = adapter
        app.state.ingestion = IngestionService(
            adapter,
            app.state.session_factory,
            settings,
            fatal_exc_types=fatal_types,
            psns=psns,
        )
    except Exception as exc:
        log.error("không khởi tạo được adapter/ingestion: %s", exc)
        app.state.adapter = None
        app.state.ingestion = None

    app.state._state_ready = True


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _init_state(app)
    settings = app.state.settings

    if settings.scheduler_enabled:
        # AsyncIOScheduler dùng chung event loop của uvicorn. KHÔNG chạy trên
        # serverless (Vercel): ở đó đặt SCHEDULER_ENABLED=false và để Vercel Cron
        # gọi /api/cron/ingest thay thế.
        app.state.scheduler = build_scheduler(app)
        app.state.scheduler.start()
        log.info(
            "scheduler đã bật: mỗi %s phút (jitter %ss, ingest_on_startup=%s)",
            settings.ingest_interval_minutes,
            settings.ingest_jitter_seconds,
            settings.ingest_on_startup,
        )
    else:
        log.info("scheduler TẮT (SCHEDULER_ENABLED=false)")

    try:
        yield
    finally:
        if app.state.scheduler is not None:
            # wait=False: trên Windows wait=True thường xuyên làm Ctrl+C treo.
            app.state.scheduler.shutdown(wait=False)
        if getattr(app.state, "adapter", None) is not None:
            app.state.adapter.close()
        if getattr(app.state, "engine", None) is not None:
            app.state.engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        # KHÔNG có tên vendor trong metadata OpenAPI: /docs bị chụp màn hình và chia sẻ.
        title="NLX LNG Monitoring - Internal API",
        description="API nội bộ theo dõi bồn LNG. Dữ liệu thuộc GAS Nhiên Liệu Xanh.",
        version="0.1.0",
        lifespan=lifespan,
    )

    errors.install(app)

    # Thêm Session trước CORS: add_middleware là LIFO, CORS phải là lớp ngoài.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="nlx_session",
        max_age=settings.session_hours * 3600,
        same_site="lax",
        # Secure cookie ngoài dev: production luôn chạy HTTPS (Vercel), nên gắn cờ
        # Secure để cookie phiên không bao giờ đi qua HTTP. Dev (localhost http) để
        # False cho tiện chạy thử.
        https_only=not settings.is_dev,
    )

    if settings.cors_origin_list:
        # Không bao giờ allow_origins=["*"] cùng allow_credentials=True — browser
        # từ chối, và với một tool nội bộ thì đó cũng là cấu hình sai.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH"],
            allow_headers=["*"],
        )

    # Router TRƯỚC static mount.
    app.include_router(api_router, prefix="/api")

    if STATIC_DIR.is_dir():
        # Mount ở /ui + redirect, KHÔNG mount ở "/": mount "/" thành catch-all âm
        # thầm nuốt mọi route gõ sai và có thể che /docs, /openapi.json.
        app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")

        @app.get("/", include_in_schema=False)
        def _root() -> RedirectResponse:
            return RedirectResponse("/ui/")

    # Khởi tạo state ngay lúc import: trên serverless (Vercel) lifespan có thể không
    # chạy, nên endpoint phải có sẵn engine/session_factory/ingestion. Idempotent
    # với lifespan (server thường), và create_engine lazy nên không mở kết nối DB.
    _init_state(app)
    return app


app = create_app()
