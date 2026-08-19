"""Rule cảnh báo. Hàm thuần, derive lúc đọc — không có bảng alerts.

Giai đoạn 1 KHÔNG persist alert và không có pipeline thông báo. Alert là một hàm
của lần đọc mới nhất, nên lưu lại chỉ tạo ra khả năng cache bị lệch so với dữ liệu.
Khi nào cần lịch sử alert hoặc ack/mute thì mới thêm bảng.

Thay thế placeholder của prototype (`stat-alert` = số thiết bị offline) — placeholder
đó sẽ bị hiểu là cảnh báo thật trong khi nó chỉ đếm lại một con số đã hiển thị.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from app.domain.contracts import TerminalStatus
from app.domain.status import derive_status


class Severity(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class AlertCode(StrEnum):
    OFFLINE = "OFFLINE"
    LOW_VOLUME = "LOW_VOLUME"
    LOW_BATTERY = "LOW_BATTERY"
    WEAK_SIGNAL = "WEAK_SIGNAL"
    PERCENT_MISMATCH = "PERCENT_MISMATCH"


@dataclass(frozen=True, slots=True)
class Alert:
    psn: str
    code: AlertCode
    severity: Severity
    message: str
    value: Decimal | None = None
    threshold: Decimal | None = None


@dataclass(frozen=True, slots=True)
class AlertThresholds:
    stale_after: timedelta
    low_volume_percent: Decimal
    low_battery_v: Decimal
    low_signal_percent: Decimal
    # Lệch quá ngưỡng này giữa volume_percent (vendor gửi) và fill_percent (ta tự
    # tính) là dấu hiệu vendor đổi thang hoặc capacity_l sai.
    percent_mismatch_points: Decimal = Decimal("5")


@dataclass(frozen=True, slots=True)
class TerminalSnapshot:
    """Đủ để đánh giá alert cho một terminal. Không phụ thuộc ORM hay Pydantic."""

    psn: str
    last_seen_at: datetime | None
    volume_percent: Decimal | None = None
    fill_percent: Decimal | None = None
    battery_v: Decimal | None = None
    signal_percent: Decimal | None = None


def evaluate(
    snap: TerminalSnapshot, th: AlertThresholds, now: datetime
) -> list[Alert]:
    """Sinh alert cho một terminal. Nhận `now` để test không cần mock clock."""
    out: list[Alert] = []

    if derive_status(snap.last_seen_at, now, th.stale_after) is TerminalStatus.OFFLINE:
        if snap.last_seen_at is None:
            detail = "chưa từng nhận dữ liệu"
        else:
            hours = (now - snap.last_seen_at).total_seconds() / 3600.0
            detail = f"không có dữ liệu {hours:.1f} giờ"
        out.append(Alert(snap.psn, AlertCode.OFFLINE, Severity.WARNING,
                         f"Thiết bị offline — {detail}"))

    # Ưu tiên fill_percent (server tự tính từ volume_l/capacity_l) hơn số vendor
    # gửi: nó là con số ta kiểm chứng được, và chính nó phát hiện lỗi thang 0-1
    # vs 0-100 mà không constraint nào bắt được.
    pct = snap.fill_percent if snap.fill_percent is not None else snap.volume_percent
    if pct is not None and pct < th.low_volume_percent:
        out.append(Alert(snap.psn, AlertCode.LOW_VOLUME, Severity.CRITICAL,
                         f"Mức LNG thấp: {pct:.2f}%", pct, th.low_volume_percent))

    if snap.battery_v is not None and snap.battery_v < th.low_battery_v:
        out.append(Alert(snap.psn, AlertCode.LOW_BATTERY, Severity.WARNING,
                         f"Pin yếu: {snap.battery_v} V",
                         snap.battery_v, th.low_battery_v))

    if snap.signal_percent is not None and snap.signal_percent < th.low_signal_percent:
        out.append(Alert(snap.psn, AlertCode.WEAK_SIGNAL, Severity.INFO,
                         f"Tín hiệu yếu: {snap.signal_percent}%",
                         snap.signal_percent, th.low_signal_percent))

    # Đối chứng hai nguồn. Không CHECK constraint nào bắt được lỗi thang vì 0.59
    # hợp lệ ở cả 0-1 và 0-100; chỉ có sự KHÔNG KHỚP giữa hai số tính độc lập mới
    # phát hiện được.
    if snap.volume_percent is not None and snap.fill_percent is not None:
        gap = abs(snap.volume_percent - snap.fill_percent)
        if gap > th.percent_mismatch_points:
            out.append(Alert(
                snap.psn, AlertCode.PERCENT_MISMATCH, Severity.WARNING,
                f"volume_percent vendor ({snap.volume_percent:.2f}) lệch "
                f"{gap:.2f} điểm so với tính từ dung tích ({snap.fill_percent:.2f}) "
                "— nghi vendor đổi thang hoặc capacity_l sai",
                gap, th.percent_mismatch_points,
            ))

    return out


def fill_percent(
    volume_l: Decimal | None, capacity_l: Decimal | None
) -> Decimal | None:
    """volume_l / capacity_l * 100, thang 0-100. None nếu không tính được."""
    if volume_l is None or capacity_l is None or capacity_l <= 0:
        return None
    return (volume_l / capacity_l) * Decimal(100)
