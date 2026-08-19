"""Auth cho Xingke: password login là chính, static token là fallback.

Đã xác minh trên hệ thật (DISCOVERY.md mục 1):

  * Login  : POST /ls/login, body {"userName":…, "password":…}
  * Token  : header ``Authorization: Bearer <token>``
  * Dạng   : UUID 36 ký tự, KHÔNG phải JWT -> opaque, session server-side, KHÔNG
             decode được expiry. Vì vậy không thể refresh chủ động theo exp; chỉ
             có thể phản ứng khi vendor trả 401.
  * Header : ``X-Requsted-With: XMLHttpRequst`` -- typo CỦA VENDOR, gửi y nguyên.

Mọi ``X-Token`` / ``Admin-Token`` / ``baseURL:"/prod-api"`` thấy trong bundle JS là
boilerplate chết của vue-element-admin, không dùng.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Protocol

import httpx

from app.adapters.xingke.config import XingkeSettings
from app.adapters.xingke.errors import (
    XingkeAuthError,
    XingkeConfigError,
    XingkeSessionExpired,
)

log = logging.getLogger(__name__)

# Typo của vendor. Không sửa: server so sánh chuỗi này y nguyên.
VENDOR_XHR_HEADER = {"X-Requsted-With": "XMLHttpRequst"}


class XingkeAuth(Protocol):
    def headers(self) -> dict[str, str]: ...

    def refresh(self, client: httpx.Client) -> bool:
        """Thử lấy credential mới. True nếu có cái mới để thử lại."""
        ...

    def invalidate(self) -> None: ...

    @property
    def kind(self) -> str: ...


class StaticTokenAuth:
    """Token dán từ ``localStorage.token`` của browser, đọc từ .env.

    Không refresh được: khi hết hạn thì raise SessionExpired kèm hướng dẫn cụ thể.
    Đây là fallback để hệ thống chạy được ngay mà không cần password trong .env,
    KHÔNG phải phương án lâu dài — token có thể bị bind IP (chưa xác minh) và chắc
    chắn có TTL không biết trước.
    """

    kind = "static_token"

    def __init__(self, token: str) -> None:
        if not token:
            raise XingkeConfigError("XINGKE_TOKEN rỗng")
        self._token = token

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", **VENDOR_XHR_HEADER}

    def refresh(self, client: httpx.Client) -> bool:
        raise XingkeSessionExpired(
            "token tĩnh đã hết hạn hoặc bị thu hồi",
            remediation=(
                "Đăng nhập www.xk-iot.cn trong browser, copy localStorage.token, "
                "cập nhật XINGKE_TOKEN trong .env, rồi POST /api/admin/ingest/resume. "
                "Muốn tự re-login thì set XINGKE_USERNAME + XINGKE_PASSWORD."
            ),
        )

    def invalidate(self) -> None:
        return None


class PasswordAuth:
    """Tự đăng nhập bằng username/password và cache token trong bộ nhớ.

    Token KHÔNG được ghi ra đĩa: nó tương đương một session sống, và ghi nó xuống
    file biến một secret ngắn hạn thành một secret dài hạn nằm trong backup.
    """

    kind = "password"

    def __init__(self, settings: XingkeSettings) -> None:
        if not settings.username or not settings.password:
            raise XingkeConfigError("thiếu XINGKE_USERNAME / XINGKE_PASSWORD")
        self._settings = settings
        self._token: str | None = None
        # Scheduler job và một lệnh CLI có thể cùng chạy trong một process; hai lần
        # login song song sẽ tạo hai session và có thể invalidate lẫn nhau.
        self._lock = threading.Lock()

    def headers(self) -> dict[str, str]:
        if self._token is None:
            return dict(VENDOR_XHR_HEADER)
        return {"Authorization": f"Bearer {self._token}", **VENDOR_XHR_HEADER}

    def invalidate(self) -> None:
        with self._lock:
            self._token = None

    def refresh(self, client: httpx.Client) -> bool:
        with self._lock:
            username = self._settings.username or ""
            password = self._settings.password
            secret = password.get_secret_value() if password else ""

            resp = post_vendor_login(client, username, secret)
            if resp.status_code in (401, 403):
                raise XingkeSessionExpired(
                    "vendor từ chối username/password",
                    remediation=(
                        "Kiểm tra XINGKE_USERNAME / XINGKE_PASSWORD. LƯU Ý: vendor "
                        "ghi log đăng nhập (userLoginLog) — đừng thử lại vòng lặp, "
                        "account có thể bị khoá."
                    ),
                )
            try:
                body: Any = resp.json()
            except Exception as exc:
                raise XingkeSessionExpired(
                    f"login trả về non-JSON (HTTP {resp.status_code})"
                ) from exc

            token = _find_token(body)
            if not token:
                code = body.get("code") if isinstance(body, dict) else None
                msg = body.get("msg") if isinstance(body, dict) else ""
                raise XingkeSessionExpired(
                    f"login không trả token (code={code!r} msg={msg!r})"
                )

            self._token = token
            # KHÔNG log token, kể cả một phần: log file bị copy vào chat và ticket.
            log.info("xingke: đăng nhập thành công, token đã cache trong bộ nhớ")
            return True


def post_vendor_login(client: httpx.Client, username: str, password: str) -> httpx.Response:
    """POST /login đúng như SPA vendor.

    Frontend gọi ``{Params: {userName, password}}`` — axios nhét Params vào
    *query string*, body JSON để trống. Gửi user/pass trong body thì vendor trả
    ``code:-1 operation failure`` dù mật khẩu đúng.
    """
    return client.post(
        "login",
        params={"userName": username, "password": password},
        json={},
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            **VENDOR_XHR_HEADER,
        },
    )


def _find_token(body: Any) -> str | None:
    """Tìm token trong response login mà không cố định một đường dẫn cứng.

    Envelope của vendor không nhất quán giữa các endpoint (xem envelope.py), nên dò
    theo tên key ở vài tầng an toàn hơn là giả định ``data.token``.
    SPA lưu ``data.access_token || data.value`` vào localStorage.token.
    """
    if not isinstance(body, dict):
        return None
    candidates = ("token", "accessToken", "access_token", "value")
    for scope in (body, body.get("data") if isinstance(body.get("data"), dict) else {}):
        if not isinstance(scope, dict):
            continue
        for key in candidates:
            val = scope.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def build_auth(settings: XingkeSettings) -> XingkeAuth:
    """Token tĩnh thắng nếu được set — cho phép hotfix qua .env không sửa code.

    Ngược lại dùng username/password (đường lâu dài, tự re-login được).
    """
    if settings.token is not None:
        tok = settings.token.get_secret_value()
        if tok.strip():
            log.info("xingke: dùng StaticTokenAuth (XINGKE_TOKEN được set)")
            return StaticTokenAuth(tok.strip())
    if settings.username and settings.password:
        log.info("xingke: dùng PasswordAuth (tự đăng nhập)")
        return PasswordAuth(settings)
    raise XingkeConfigError(
        "chưa cấu hình auth: cần XINGKE_TOKEN, hoặc XINGKE_USERNAME + XINGKE_PASSWORD"
    )


__all__ = [
    "VENDOR_XHR_HEADER",
    "PasswordAuth",
    "StaticTokenAuth",
    "XingkeAuth",
    "XingkeAuthError",
    "build_auth",
    "post_vendor_login",
]
