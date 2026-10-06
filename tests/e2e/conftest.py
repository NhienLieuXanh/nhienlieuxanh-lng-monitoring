"""E2E thật: server thật, Postgres thật (dựng bằng migration thật), trình duyệt thật.

Chạy: ``E2E=1 pytest tests/e2e`` (cần ``playwright install chromium`` một lần).
Không đặt ``E2E=1`` thì thư mục này không được thu thập — xem ``tests/conftest.py``.

Ba quyết định:

1. **DB riêng ``xingke_e2e``**, xoá sạch và dựng lại bằng ``alembic upgrade head``
   mỗi lần chạy — kiểm luôn migration, và không bao giờ đụng DB dev hay DB test của
   bộ unit (bộ đó dùng transaction rollback, còn server e2e thì commit thật).
2. **Không gọi nguồn dữ liệu nào**: scheduler tắt, nguồn phút tắt, allowlist rỗng,
   email tắt. Mọi số trên màn hình đến từ ``seed.py``.
3. **Đăng nhập bằng cookie phiên tự ký**: đăng nhập thật hỏi cổng của nhà cung cấp
   bằng mật khẩu thật — một bộ test tự động không được cầm mật khẩu đó. Server e2e
   chạy với ``SESSION_SECRET`` riêng của bộ test, nên cookie tự ký chỉ mở được đúng
   server này. Màn hình đăng nhập vẫn được test ở nhánh CHƯA đăng nhập.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from itsdangerous import TimestampSigner
from playwright.sync_api import Page
from sqlalchemy import URL, create_engine, make_url, text

from tests.e2e.seed import seed

ROOT = Path(__file__).resolve().parents[2]
SECRET = "e2e-session-secret-khong-dung-cho-production"
USER = "e2e-tester"


def _db_url() -> str:
    url = os.getenv("E2E_DATABASE_URL")
    if url:
        return url
    from app.config import get_settings

    s = get_settings()
    return URL.create(
        "postgresql+psycopg", username=s.db_user, password=s.db_password,
        host=s.db_host, port=s.db_port, database="xingke_e2e",
    ).render_as_string(hide_password=False)


def _ensure_db(url: str) -> str:
    """Tạo DB e2e nếu chưa có. User không có quyền CREATEDB (máy dev hiện tại) thì
    lùi về DB test của bộ unit — bộ đó tự drop/create bảng mỗi lần chạy nên dùng
    chung không để lại gì cho nhau. CI có DB ``xingke_e2e`` riêng."""
    u = make_url(url)
    admin = create_engine(u.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            if c.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": u.database}).scalar():
                return url
            try:
                c.execute(text(f'CREATE DATABASE "{u.database}"'))
                return url
            except Exception:
                return u.set(database="xingke_test").render_as_string(hide_password=False)
    finally:
        admin.dispose()


def _reset_db(url: str) -> str:
    url = _ensure_db(url)
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    eng.dispose()
    r = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT, env={**os.environ, "DATABASE_URL": url, "PYTHONIOENCODING": "utf-8"},
        capture_output=True, text=True, encoding="utf-8",
    )
    if r.returncode != 0:
        raise RuntimeError(f"alembic upgrade head thất bại:\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    return url


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def world() -> dict[str, Any]:
    url = _reset_db(_db_url())
    eng = create_engine(url)
    try:
        info = seed(eng)
    finally:
        eng.dispose()
    return {"db_url": url, **info}


@pytest.fixture(scope="session")
def server(world: dict[str, Any], tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    port = _free_port()
    log_path = tmp_path_factory.mktemp("e2e") / "server.log"
    env = {
        **os.environ,
        "DATABASE_URL": world["db_url"],
        "SESSION_SECRET": SECRET,
        "APP_ENV": "dev",
        "APP_TZ": "Asia/Ho_Chi_Minh",
        "SCHEDULER_ENABLED": "false",
        "INGEST_ON_STARTUP": "false",
        "NOTIFY_ENABLED": "false",
        "YOKOHAMA_ENABLED": "false",
        "XINGKE_ALLOWED_PSNS": "",
        "PYTHONIOENCODING": "utf-8",
    }
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(port), "--log-level", "warning"],
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        base = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 60
            while True:
                if proc.poll() is not None:
                    raise RuntimeError(f"server e2e chết khi khởi động:\n{log_path.read_text('utf-8')[-3000:]}")
                try:
                    if httpx.get(f"{base}/api/health", timeout=2).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError("server e2e không lên sau 60 giây")
                time.sleep(0.5)
            yield base
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    """Trình duyệt ở múi giờ và ngôn ngữ của người dùng thật.

    Trang tính "ngày" theo giờ của trình duyệt; máy CI để UTC thì ngày 01/10 của
    trang lệch 7 giờ với ngày của server (APP_TZ) — lần CI đầu tiên thấy đúng thế.
    Người dùng đều ở Việt Nam, nên bộ test phải đứng ở Việt Nam.
    """
    return {**browser_context_args, "timezone_id": "Asia/Ho_Chi_Minh", "locale": "vi-VN"}


def session_cookie(user: str = USER) -> str:
    """Cookie ``nlx_session`` đúng định dạng SessionMiddleware của Starlette."""
    data = base64.b64encode(json.dumps({"user": user}).encode("utf-8"))
    return TimestampSigner(SECRET).sign(data).decode("utf-8")


def _watch_errors(page: Page) -> list[str]:
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: errors.append(f"console.error: {m.text}") if m.type == "error" else None)
    return errors


@pytest.fixture
def app_page(page: Page, server: str) -> Iterator[Page]:
    """Trang đã đăng nhập, đứng ở Bảng điều khiển. Hỏng nếu có lỗi JS nào."""
    page.context.add_cookies([{
        "name": "nlx_session", "value": session_cookie(), "url": server,
    }])
    errors = _watch_errors(page)
    page.set_default_timeout(15_000)
    page.goto(f"{server}/ui/")
    page.locator('[data-view="dashboard"]').first.wait_for()
    yield page
    assert errors == [], "\n".join(errors)


@pytest.fixture
def anon_page(page: Page, server: str) -> Iterator[Page]:
    errors = _watch_errors(page)
    page.goto(f"{server}/ui/")
    yield page
    # 401 của lần hỏi phiên là chuyện bình thường khi chưa đăng nhập.
    assert [e for e in errors if "401" not in e] == [], "\n".join(errors)


def go(page: Page, view: str) -> None:
    page.locator(f'[data-view="{view}"]').first.click()
    page.locator(f"#view-{view}").wait_for(state="visible")
