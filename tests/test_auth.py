"""Đăng nhập đa người: tài khoản cổng telemetry + cookie phiên."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from app.api.deps import UserDep, get_settings
from app.api.routers import auth as auth_mod
from app.api.routers.auth import router as auth_router
from app.config import Settings
from app.factory import VendorLoginError, verify_vendor_credentials


@pytest.fixture(autouse=True)
def _clear_fails() -> None:
    auth_mod._FAILS.clear()


@pytest.fixture
def fake_settings() -> Settings:
    return Settings(
        app_env="test",
        db_password="x",
        xingke_adapter="live",
        scheduler_enabled=False,
        session_secret="test-session-secret",
    )


@pytest.fixture
def client(fake_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Đăng nhập giờ LUÔN xác thực với vendor thật (không còn đường demo trong sản
    # phẩm). Trong test, thay bằng stub để kiểm cookie phiên + rate limit mà không
    # gọi mạng: mật khẩu "demo" là đúng, còn lại sai.
    def _verify(username: str, password: str, settings: Settings) -> str:
        u = username.strip()
        if not u or not password:
            raise VendorLoginError("nhập tài khoản và mật khẩu")
        if password != "demo":
            raise VendorLoginError("sai tài khoản hoặc mật khẩu")
        return u

    monkeypatch.setattr(auth_mod, "verify_vendor_credentials", _verify)

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-session-secret")
    app.include_router(auth_router, prefix="/api")

    @app.get("/api/secret")
    def secret(user: UserDep) -> dict[str, str]:
        return {"user": user}

    app.dependency_overrides[get_settings] = lambda: fake_settings
    return TestClient(app)


def test_me_requires_session(client: TestClient) -> None:
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/secret").status_code == 401


def test_login_logout_roundtrip(client: TestClient) -> None:
    bad = client.post("/api/auth/login", json={"username": "son", "password": "sai"})
    assert bad.status_code == 401

    ok = client.post("/api/auth/login", json={"username": "son", "password": "demo"})
    assert ok.status_code == 200
    assert ok.json() == {"username": "son"}

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json() == {"username": "son"}
    assert client.get("/api/secret").json() == {"user": "son"}

    assert client.post("/api/auth/logout").json() == {"ok": True}
    assert client.get("/api/auth/me").status_code == 401


def test_find_token_accepts_vendor_value_key() -> None:
    from app.adapters.xingke.auth import _find_token

    assert _find_token({"access_token": "aaa"}) == "aaa"
    assert _find_token({"data": {"value": "bbb"}}) == "bbb"
    assert _find_token({"userInfo": {}}) is None


def test_live_login_sends_query_params(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class _Resp:
        status_code = 200

        def json(self) -> dict:
            return {
                "code": 200,
                "msg": "ok",
                "data": {"access_token": "tok-1", "userInfo": {"realName": "A"}},
            }

    class _Client:
        def __init__(self, **kw: object) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def post(self, path: str, **kw: object) -> _Resp:
            captured["path"] = path
            captured["kw"] = kw
            return _Resp()

    monkeypatch.setattr("httpx.Client", _Client)
    settings = Settings(
        app_env="test",
        db_password="x",
        xingke_adapter="live",
        scheduler_enabled=False,
    )
    assert verify_vendor_credentials("alice", "secret", settings) == "alice"
    assert captured["path"] == "login"
    assert captured["kw"]["params"] == {"userName": "alice", "password": "secret"}


def test_login_rate_limit(client: TestClient) -> None:
    for _ in range(5):
        assert (
            client.post(
                "/api/auth/login", json={"username": "son", "password": "sai"}
            ).status_code
            == 401
        )
    blocked = client.post(
        "/api/auth/login", json={"username": "son", "password": "demo"}
    )
    assert blocked.status_code == 429
