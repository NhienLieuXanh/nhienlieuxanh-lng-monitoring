"""Cây exception của adapter Xingke.

Việc chia AuthError -> SessionExpired CHÍNH LÀ cơ chế cho yêu cầu "session hết
hạn thì log rõ và dừng job": tầng trong raise AuthError, client escalate lên
SessionExpired sau MỘT lần refresh thất bại, và scheduler chỉ nhìn SessionExpired.
"""

from __future__ import annotations

from typing import Any


class XingkeError(Exception):
    """Gốc. Không bao giờ để text của các exception này lọt ra response HTTP —
    chúng có thể nhúng URL vendor, PSN, hoặc token."""


class XingkeConfigError(XingkeError):
    """Thiếu/sai credential. Fail nhanh lúc khởi động, không retry."""


class XingkeTransientError(XingkeError):
    """5xx, 429, timeout, connect error. CÓ retry (tenacity)."""

    def __init__(self, status: int | None, detail: str = "") -> None:
        super().__init__(f"transient (status={status}): {detail}")
        self.status = status


class XingkeProtocolError(XingkeError):
    """Body không parse được thành JSON. KHÔNG retry — retry cũng vậy thôi."""


class XingkeApiError(XingkeError):
    """code != success. Lỗi business, KHÔNG retry."""

    def __init__(self, code: Any, msg: str, body: Any = None) -> None:
        super().__init__(f"api error code={code!r}: {msg}")
        self.code = code
        self.msg = msg
        self.body = body


class XingkeAuthError(XingkeError):
    """401/403 hoặc msg mang nghĩa auth. Có thể còn cứu được bằng re-login."""

    def __init__(self, code: Any, msg: str = "") -> None:
        super().__init__(f"auth rejected code={code!r}: {msg}")
        self.code = code
        self.msg = msg


class XingkeSessionExpired(XingkeAuthError):
    """Terminal. Đã thử refresh và thất bại, hoặc không thể refresh.

    Đây là exception DUY NHẤT làm scheduler pause job. Auth error không retryable
    theo định nghĩa, nên retry nó là thuần gây hại: một password sai biến thành
    hàng nghìn lần thử đăng nhập trên một account mà vendor đang audit.
    """

    def __init__(self, detail: str, remediation: str = "") -> None:
        super().__init__("session_expired", detail)
        self.detail = detail
        self.remediation = remediation


class XingkeForeignDataError(XingkeError):
    """Response chứa PSN không thuộc allowlist.

    Endpoint backstage của vendor bỏ qua org scope: device/list không filter trả
    về 3543 thiết bị của mọi khách hàng. Adapter drop chúng và đếm vào report;
    exception này chỉ dùng cho chế độ strict.
    """
