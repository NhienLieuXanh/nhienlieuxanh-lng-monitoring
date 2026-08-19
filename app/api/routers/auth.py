"""Đăng nhập bằng tài khoản cổng telemetry. Không giữ password / token vendor."""

from __future__ import annotations

import logging
import time
from collections import defaultdict

from fastapi import APIRouter, HTTPException, Request, status

from app.api.deps import SettingsDep, UserDep
from app.api.schemas import LoginIn, UserOut
from app.factory import VendorLoginError, verify_vendor_credentials

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

# Chống gõ sai vòng lặp: vendor ghi userLoginLog. 5 lần / 10 phút / IP.
_FAILS: dict[str, list[float]] = defaultdict(list)
_WINDOW = 600.0
_MAX_FAILS = 5


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _too_many(ip: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _FAILS[ip] if now - t < _WINDOW]
    _FAILS[ip] = recent
    return len(recent) >= _MAX_FAILS


def _record_fail(ip: str) -> None:
    _FAILS[ip].append(time.monotonic())


@router.post("/login", response_model=UserOut)
def login(body: LoginIn, request: Request, settings: SettingsDep) -> UserOut:
    ip = _client_ip(request)
    if _too_many(ip):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "thử đăng nhập quá nhiều lần; đợi vài phút",
        )
    try:
        user = verify_vendor_credentials(body.username, body.password, settings)
    except VendorLoginError as exc:
        _record_fail(ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, exc.message) from None
    request.session.clear()
    request.session["user"] = user
    return UserOut(username=user)


@router.post("/logout")
def logout(request: Request) -> dict[str, bool]:
    request.session.clear()
    return {"ok": True}


@router.get("/me", response_model=UserOut)
def me(user: UserDep) -> UserOut:
    return UserOut(username=user)
