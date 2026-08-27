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
    #: Lần đọc cũ hơn mức này thì KHÔNG còn dùng để kết luận về thiết bị.
    #: Cùng một con số với ``forecast_max_reading_age_hours`` (mặc định 24 giờ) —
    #: cố ý dùng chung để cả sản phẩm có MỘT định nghĩa "số đo quá cũ để tin".
    max_reading_age: timedelta = timedelta(hours=24)
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


def elapsed_vi(delta: timedelta) -> str:
    """Khoảng thời gian đã trôi, CÙNG thang với ``fmtAgo()`` của dashboard.

    Làm tròn XUỐNG và tự đổi đơn vị khi vượt ngưỡng. Hai lý do, cả hai là lỗi thật
    đã quan sát được trên production:

    - ``{hours:.0f}`` không bao giờ đổi sang ngày, nên một bồn mất tín hiệu 80 ngày
      sinh ra chuỗi "không có dữ liệu trong 1935 giờ". Không ai đọc được, và chuỗi
      này đi vào email cảnh báo lẫn nhật ký kiểm toán.
    - ``:.0f`` làm tròn tới gần nhất (2.8 -> "3 giờ") còn dashboard làm tròn xuống
      ("2 giờ"), nên cùng một sự việc hiện hai con số khác nhau trên cùng một màn
      hình. Làm tròn xuống cũng không bao giờ nói QUÁ thời gian mất tín hiệu — điều
      quan trọng với chuỗi dùng làm bằng chứng.

    Ngưỡng 48 giờ khớp ``fmtAgo``: dưới hai ngày thì số giờ còn hành động được.
    """
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} phút"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} giờ"
    return f"{hours // 24} ngày"


def evaluate(
    snap: TerminalSnapshot, th: AlertThresholds, now: datetime
) -> list[Alert]:
    """Sinh alert cho một terminal. Nhận `now` để test không cần mock clock.

    **Lần đọc quá cũ thì không được kết luận như số hiện tại.** Đây là lỗi đã quan
    sát được trên production: bồn 2605090007 im 85 ngày, và API phát ra hai dòng
    cạnh nhau —

        LOW_VOLUME  critical  "Mức chứa thấp: 0.29%"
        OFFLINE     warning   "không có dữ liệu trong 85 ngày"

    Hệ thống vừa nói không biết gì về bồn suốt 85 ngày, vừa phát cảnh báo NGHIÊM
    TRỌNG về mức chứa dựa trên đúng con số 85 ngày tuổi đó. ``forecast.py`` đã có
    chốt chặn này (``stale`` -> không phát runout/hold); ``alerts.py`` thì không, nên
    hai tầng nói khác nhau về cùng một dữ liệu. Nếu hộp thư đã cấu hình thì nó gửi
    "mức chứa thấp nghiêm trọng" mỗi ngày về một bồn có thể đang đầy — loại cảnh báo
    làm người ta ngừng đọc cảnh báo.

    Xử lý phân biệt theo *đối tượng* của từng mã, không cắt hết:

    * ``LOW_BATTERY`` / ``WEAK_SIGNAL`` / ``PERCENT_MISMATCH`` nói về THIẾT BỊ. Khi
      thiết bị đã im thì ``OFFLINE`` đã mang đúng một hành động cần làm ("ra xem cái
      thiết bị"); thêm ba dòng nữa chỉ là nhiễu. -> bỏ.
    * ``LOW_VOLUME`` nói về BỒN, và "lần cuối nhìn thấy thì đã cạn" vẫn là thông tin
      thật, có thể còn đúng hơn theo thời gian. -> giữ, nhưng hạ xuống WARNING và
      ghi rõ tuổi của số đo. Không cắt (mất tín hiệu thật), không để CRITICAL (đòi
      hành động dựa trên thông tin không ai có).
    """
    out: list[Alert] = []
    age = None if snap.last_seen_at is None else now - snap.last_seen_at
    stale_reading = age is None or age > th.max_reading_age
    #: Chuỗi gắn vào message để không ai đọc số cũ như số hiện tại.
    aged = (
        " (chưa từng có số đo)"
        if age is None
        else f" — số đo {elapsed_vi(age)} trước, thiết bị đã ngoại tuyến"
    )

    if derive_status(snap.last_seen_at, now, th.stale_after) is TerminalStatus.OFFLINE:
        if snap.last_seen_at is None:
            detail = "chưa từng nhận được dữ liệu"
        else:
            detail = f"không có dữ liệu trong {elapsed_vi(now - snap.last_seen_at)}"
        out.append(Alert(snap.psn, AlertCode.OFFLINE, Severity.WARNING,
                         f"Thiết bị ngoại tuyến — {detail}"))

    # Ưu tiên fill_percent (server tự tính từ volume_l/capacity_l) hơn số vendor
    # gửi: nó là con số ta kiểm chứng được, và chính nó phát hiện lỗi thang 0-1
    # vs 0-100 mà không constraint nào bắt được.
    pct = snap.fill_percent if snap.fill_percent is not None else snap.volume_percent
    if pct is not None and pct < th.low_volume_percent:
        out.append(Alert(
            snap.psn,
            AlertCode.LOW_VOLUME,
            Severity.WARNING if stale_reading else Severity.CRITICAL,
            f"Mức chứa thấp: {pct:.2f}%" + (aged if stale_reading else ""),
            pct,
            th.low_volume_percent,
        ))

    # Ba mã dưới đây nói về THIẾT BỊ. Thiết bị đã im thì OFFLINE nói đủ.
    if stale_reading:
        return out

    if snap.battery_v is not None and snap.battery_v < th.low_battery_v:
        out.append(Alert(snap.psn, AlertCode.LOW_BATTERY, Severity.WARNING,
                         f"Điện áp pin thấp: {snap.battery_v} V",
                         snap.battery_v, th.low_battery_v))

    if snap.signal_percent is not None and snap.signal_percent < th.low_signal_percent:
        out.append(Alert(snap.psn, AlertCode.WEAK_SIGNAL, Severity.INFO,
                         f"Cường độ tín hiệu thấp: {snap.signal_percent}%",
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
