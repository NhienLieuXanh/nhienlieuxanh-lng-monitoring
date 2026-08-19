"""Suy trạng thái online/offline. Hàm thuần, không đọc clock.

Nhận `now` làm tham số thay vì gọi datetime.now() bên trong: test kiểm được
đúng biên (ngưỡng -1s / +1s / chính xác / None) mà không cần mock clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.domain.contracts import TerminalStatus


def derive_status(
    last_seen_at: datetime | None,
    now: datetime,
    stale_after: timedelta,
) -> TerminalStatus:
    if last_seen_at is None:
        return TerminalStatus.OFFLINE
    return (
        TerminalStatus.ONLINE
        if (now - last_seen_at) <= stale_after
        else TerminalStatus.OFFLINE
    )
