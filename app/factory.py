"""Lắp ráp adapter. Đây là NƠI DUY NHẤT phần còn lại của app biết vendor tồn tại.

Mọi module khác (services, api, repositories, domain) chỉ thấy TelemetryPort. Nhờ
vậy FakeAdapter là drop-in thật, và ``tests/test_isolation.py`` kiểm được luật đó
bằng máy chứ không bằng kỷ luật.
"""

from __future__ import annotations

import logging

from app.config import Settings
from app.domain.contracts import TelemetryPort

log = logging.getLogger(__name__)


class VendorLoginError(Exception):
    """Sai credential hoặc cổng telemetry từ chối. Message an toàn để hiện ra UI."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def verify_vendor_credentials(username: str, password: str, settings: Settings) -> str:
    """Xác thực tài khoản cổng telemetry. Trả về username đã chuẩn hoá.

    Đây là cổng đăng nhập đa người: bất kỳ account nào vendor chấp nhận đều vào
    được dashboard. Token vendor KHÔNG được giữ — ingest vẫn dùng credential
    máy chủ trong .env, tách khỏi phiên người xem.
    """
    user = username.strip()
    if not user or not password:
        raise VendorLoginError("nhập tài khoản và mật khẩu")

    import httpx

    from app.adapters.xingke.auth import VENDOR_XHR_HEADER, _find_token, post_vendor_login
    from app.adapters.xingke.config import get_xingke_settings
    from app.adapters.xingke.envelope import unwrap
    from app.adapters.xingke.errors import (
        XingkeApiError,
        XingkeAuthError,
        XingkeSessionExpired,
    )

    xs = get_xingke_settings()
    try:
        with httpx.Client(
            base_url=xs.base_url,
            timeout=httpx.Timeout(xs.timeout_seconds, connect=xs.connect_timeout_seconds),
            follow_redirects=False,
            headers={"Accept": "application/json", **VENDOR_XHR_HEADER},
        ) as client:
            resp = post_vendor_login(client, user, password)
            data = unwrap(resp)
    except (XingkeAuthError, XingkeSessionExpired):
        raise VendorLoginError("sai tài khoản hoặc mật khẩu") from None
    except XingkeApiError:
        raise VendorLoginError("sai tài khoản hoặc mật khẩu") from None
    except Exception:
        log.exception("login: không gọi được cổng telemetry")
        raise VendorLoginError("không kết nối được cổng telemetry") from None

    blob = data if isinstance(data, dict) else {}
    token = _find_token(blob) or _find_token({"data": blob})
    if not token:
        raise VendorLoginError("sai tài khoản hoặc mật khẩu")
    log.info("login: xác thực thành công user=%s", user)
    return user


def build_adapter(settings: Settings) -> tuple[TelemetryPort, tuple[type[BaseException], ...], list[str]]:
    """Trả về (adapter, các exception fatal, danh sách PSN).

    ``fatal_exc_types`` được TRẢ RA thay vì để IngestionService import: service
    không được biết tên module vendor. Scheduler chỉ cần biết "cái này fatal".
    """
    from app.adapters.xingke.adapter import XingkeAdapter
    from app.adapters.xingke.config import get_xingke_settings
    from app.adapters.xingke.errors import XingkeSessionExpired

    xs = get_xingke_settings()
    adapter = XingkeAdapter(xs, store_raw=settings.store_raw_payload)
    psns = sorted(xs.allowed_psn_set)
    log.info("adapter: live, %s PSN trong allowlist", len(psns))
    # CHỈ SessionExpired là fatal. XingkeAuthError thường vẫn cứu được bằng re-login
    # (client tự làm), nên nó không được làm dừng job.
    return adapter, (XingkeSessionExpired,), psns
