"""Lắp ráp adapter. Đây là NƠI DUY NHẤT phần còn lại của app biết vendor tồn tại.

Mọi module khác (services, api, repositories, domain) chỉ thấy TelemetryPort. Nhờ
vậy adapter có thể hoán đổi, và ``tests/test_isolation.py`` kiểm được luật đó
bằng máy chứ không bằng kỷ luật.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.config import Settings
from app.domain.contracts import TelemetryPort, VendorAlarmPort

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


@dataclass
class BuiltAdapters:
    """Kết quả lắp ráp. close() đóng mọi port, không chỉ primary."""

    primary: TelemetryPort
    fatal_exc_types: tuple[type[BaseException], ...]
    psns: list[str]
    by_psn: dict[str, TelemetryPort] = field(default_factory=dict)
    alarm_port: VendorAlarmPort | None = None

    def probe(self) -> dict:
        fn = getattr(self.primary, "probe", None)
        if fn is None:
            return {}
        return fn()

    def close(self) -> None:
        seen: set[int] = set()
        for port in self.by_psn.values():
            i = id(port)
            if i in seen:
                continue
            seen.add(i)
            port.close()
        if id(self.primary) not in seen:
            self.primary.close()


def build_adapter(settings: Settings) -> BuiltAdapters:
    """Lắp mọi TelemetryPort. Service nhận resolve(psn), không if-source.

    ``fatal_exc_types`` được TRẢ RA thay vì để IngestionService import: service
    không được biết tên module vendor. Scheduler chỉ cần biết "cái này fatal".
    """
    from app.adapters.xingke.adapter import XingkeAdapter
    from app.adapters.xingke.config import get_xingke_settings
    from app.adapters.xingke.errors import XingkeSessionExpired

    xs = get_xingke_settings()
    xingke = XingkeAdapter(xs, store_raw=settings.store_raw_payload)
    by_psn: dict[str, TelemetryPort] = dict.fromkeys(
        sorted(xs.allowed_psn_set), xingke
    )
    fatal: tuple[type[BaseException], ...] = (XingkeSessionExpired,)
    alarm_port: VendorAlarmPort | None = None
    primary: TelemetryPort = xingke

    from app.adapters.yokohama.config import get_yokohama_settings

    ys = get_yokohama_settings()
    if ys.enabled:
        from app.adapters.yokohama.adapter import YokohamaAdapter

        yoko = YokohamaAdapter(ys, store_raw=False)
        for psn in ys.psn_list:
            if psn in by_psn:
                raise RuntimeError(
                    f"PSN {psn} trùng hai nguồn telemetry — không lắp được"
                )
            by_psn[psn] = yoko
        alarm_port = yoko
        if not xs.allowed_psn_set:
            primary = yoko
            log.warning(
                "adapter: nguồn phút bật nhưng allowlist nguồn kia rỗng — "
                "chỉ ingest PSN nguồn phút"
            )
        log.info("adapter: nguồn phút bật, psn=%s", ",".join(ys.psn_list))

    psns = list(by_psn) if by_psn else sorted(xs.allowed_psn_set)
    log.info("adapter: live, %s PSN", len(psns))
    return BuiltAdapters(
        primary=primary,
        fatal_exc_types=fatal,
        psns=psns,
        by_psn=by_psn,
        alarm_port=alarm_port,
    )
