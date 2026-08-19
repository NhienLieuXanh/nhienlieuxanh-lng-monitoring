"""HTTP client cho Xingke: retry transient, re-login MỘT lần, throttle lịch sự.

Ba quyết định quan trọng:

1. **Retry chỉ cho transient.** Timeout / 5xx / 429 có retry với backoff. Lỗi auth
   và lỗi business KHÔNG. Retry một password sai biến một lỗi cấu hình thành hàng
   nghìn lần thử đăng nhập trên một account mà vendor đang ghi log audit.

2. **Re-login đúng một lần rồi bỏ.** Gặp 401 -> invalidate + refresh + thử lại một
   lần. Vẫn 401 -> XingkeSessionExpired (terminal). Không có vòng lặp.

3. **Throttle tối thiểu giữa các request.** Đây là API console quản trị của vendor,
   không phải API công khai có hợp đồng. Poll dày có thể bị coi là abuse.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.adapters.xingke.auth import VENDOR_XHR_HEADER, XingkeAuth
from app.adapters.xingke.config import XingkeSettings
from app.adapters.xingke.envelope import unwrap
from app.adapters.xingke.errors import (
    XingkeAuthError,
    XingkeSessionExpired,
    XingkeTransientError,
)

log = logging.getLogger(__name__)


class XingkeClient:
    """Bọc httpx.Client. Trả về ``data`` đã bóc envelope, hoặc raise."""

    def __init__(self, settings: XingkeSettings, auth: XingkeAuth) -> None:
        self._settings = settings
        self._auth = auth
        self._last_call = 0.0
        self._throttle_lock = threading.Lock()
        self._client = httpx.Client(
            base_url=settings.base_url,
            timeout=httpx.Timeout(
                settings.timeout_seconds, connect=settings.connect_timeout_seconds
            ),
            headers={
                "Accept": "application/json, text/plain, */*",
                **VENDOR_XHR_HEADER,
                **settings.extra_headers,
            },
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> XingkeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _throttle(self) -> None:
        with self._throttle_lock:
            gap = time.monotonic() - self._last_call
            wait = self._settings.min_interval_seconds - gap
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET + bóc envelope, có retry transient và re-login một lần."""
        return self._request_with_relogin("GET", path, params=params)

    def _request_with_relogin(
        self, method: str, path: str, **kw: Any
    ) -> Any:
        try:
            return self._request_retrying(method, path, **kw)
        except XingkeSessionExpired:
            # Đã terminal — không thử gì thêm.
            raise
        except XingkeAuthError as first:
            log.warning(
                "xingke: %s %s bị từ chối auth (code=%r); thử refresh MỘT lần",
                method, path, first.code,
            )
            self._auth.invalidate()
            # refresh() có thể tự raise SessionExpired (StaticTokenAuth luôn vậy).
            if not self._auth.refresh(self._client):
                raise XingkeSessionExpired(
                    f"không refresh được credential sau khi {path} trả auth error"
                ) from first
            try:
                return self._request_retrying(method, path, **kw)
            except XingkeAuthError as second:
                raise XingkeSessionExpired(
                    f"vẫn bị từ chối auth sau re-login (code={second.code!r})",
                    remediation=(
                        "Credential có thể đã bị thu hồi, hoặc token bị bind IP, "
                        "hoặc một lần đăng nhập khác đã invalidate session này. "
                        "Xem DISCOVERY.md mục 8."
                    ),
                ) from second

    @retry(
        retry=retry_if_exception_type(
            (XingkeTransientError, httpx.TimeoutException, httpx.TransportError)
        ),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=15),
        reraise=True,
    )
    def _request_retrying(self, method: str, path: str, **kw: Any) -> Any:
        self._throttle()
        headers = {**self._auth.headers(), **kw.pop("headers", {})}
        resp = self._client.request(method, path, headers=headers, **kw)
        return unwrap(resp)

    def ensure_authenticated(self) -> None:
        """Login trước nếu auth chưa có credential.

        PasswordAuth khởi tạo không có token; gọi trước để lỗi credential lộ ra ở
        chỗ dễ chẩn đoán (startup / CLI) thay vì giữa một vòng ingest.
        """
        if not self._auth.headers().get("Authorization"):
            self._auth.refresh(self._client)
