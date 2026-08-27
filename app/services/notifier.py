"""Gửi cảnh báo ra ngoài app (email) + nhật ký chống gửi lại.

Vì sao cần: cảnh báo chỉ nằm trong dashboard thì chỉ có tác dụng khi có người
đang mở dashboard. Bồn cạn lúc 2 giờ sáng Chủ Nhật thì không ai mở. Đây là lý do
mọi nền tảng giám sát bồn ngoài thị trường đều đẩy cảnh báo qua email/SMS.

Bốn nguyên tắc, mỗi cái tránh một cách thất bại cụ thể:

1. **Một email cho cả vòng, không phải một email mỗi cảnh báo.** Hai bồn x ba mã
   cảnh báo = 6 email cùng lúc sẽ bị đọc như spam ngay lần đầu.
2. **Cửa chặn gửi lại theo (psn, mã)**, trạng thái nằm ở DB chứ không ở bộ nhớ
   process — trên serverless mỗi lần gọi là một process mới nên biến toàn cục
   không tồn tại quá một request.
3. **Không bao giờ làm vòng ingest thất bại.** Mọi lỗi SMTP bị bắt và ghi log;
   dữ liệu telemetry quan trọng hơn email, và một hộp thư sai cấu hình không được
   phép làm mất dữ liệu.
4. **Cảnh báo dùng CHUNG hàm suy với dashboard** (``domain/alerts.evaluate`` và
   ``domain/forecast.forecast_alerts``). Nếu mỗi bên tự suy thì email và màn hình
   sẽ nói khác nhau đúng vào lúc có sự cố.
"""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.domain import forecast as fc
from app.domain.alerts import AlertThresholds, TerminalSnapshot, evaluate
from app.domain.alerts import fill_percent as _fill_percent
from app.domain.smtp_errors import explain as explain_smtp
from app.repositories import notifications as notif_repo
from app.repositories import telemetry as tel_repo
from app.repositories import terminals as term_repo
from app.services.appconfig import ConfigLike, load_config

log = logging.getLogger(__name__)
UTC = ZoneInfo("UTC")

#: Chỉ những mã này được gửi ra ngoài. Cảnh báo hạ tầng như WEAK_SIGNAL hay
#: PERCENT_MISMATCH là việc của người vận hành platform, không phải việc khiến
#: ai đó phải thức dậy — để chúng vào email sẽ làm loãng những mã thật sự gấp.
NOTIFY_CODES = frozenset(
    {"RUNOUT", "HOLD_TIME", "LOW_VOLUME", "OFFLINE", "BOIL_OFF_HIGH", "LOW_BATTERY"}
)

_SEV_ORDER = {"critical": 0, "warning": 1, "info": 2}
_SEV_LABEL = {"critical": "NGHIÊM TRỌNG", "warning": "CẢNH BÁO", "info": "THÔNG TIN"}


@dataclass(frozen=True, slots=True)
class Notice:
    """Một cảnh báo đã chuẩn hoá, bất kể nó đến từ nguồn nào."""

    psn: str
    name: str | None
    code: str
    severity: str
    message: str


@dataclass(slots=True)
class NotifyStats:
    considered: int = 0
    #: Bị cửa chặn gửi lại. CỐ Ý chỉ đếm, không ghi vào bảng notifications: nó
    #: xảy ra mỗi vòng ingest nên ghi lại là 144 dòng/ngày/cảnh báo vô ích.
    suppressed: int = 0
    sent: int = 0
    failed: int = 0
    reason: str | None = None

    def summary(self) -> str:
        base = (
            f"cảnh báo={self.considered} gửi={self.sent} "
            f"bị chặn={self.suppressed} lỗi={self.failed}"
        )
        return base if self.reason is None else f"{base} ({self.reason})"


def collect_notices(
    session: Session, settings: ConfigLike, now: datetime
) -> list[Notice]:
    """Gom cảnh báo thiết bị + cảnh báo dự báo cho mọi bồn.

    Dùng lại đúng hai hàm mà API dùng, nên không có khả năng email và dashboard
    lệch nhau. Chi phí: một lượt đọc chuỗi telemetry mỗi bồn — chấp nhận được vì
    hàm này chạy trong vòng ingest (10 phút một lần), không nằm trên đường phục
    vụ request của người dùng.
    """
    stale = timedelta(minutes=settings.online_stale_minutes)
    th = AlertThresholds(
        stale_after=stale,
        low_volume_percent=Decimal(str(settings.alert_low_volume_percent)),
        low_battery_v=Decimal(str(settings.alert_low_battery_v)),
        low_signal_percent=Decimal(str(settings.alert_low_signal_percent)),
        max_reading_age=timedelta(hours=settings.forecast_max_reading_age_hours),
    )
    terms = term_repo.list_all(session)
    latest = tel_repo.latest_many(session, [t.psn for t in terms])
    out: list[Notice] = []

    for t in terms:
        lt = latest.get(t.psn)
        snap = TerminalSnapshot(
            psn=t.psn,
            last_seen_at=t.last_seen_at,
            volume_percent=lt.volume_percent if lt else None,
            fill_percent=_fill_percent(lt.volume_l if lt else None, t.capacity_l),
            battery_v=lt.battery_v if lt else None,
            signal_percent=lt.signal_percent if lt else None,
        )
        for a in evaluate(snap, th, now):
            out.append(Notice(t.psn, t.name, str(a.code), str(a.severity), a.message))

        rows = tel_repo.series(
            session,
            t.psn,
            now - timedelta(days=settings.forecast_window_days),
            now,
        )
        f = fc.build_forecast(
            [fc.Sample(at=at, volume_l=v, pressure_mpa=p) for at, v, p in rows],
            psn=t.psn,
            volume_l=(
                None if (lt is None or lt.volume_l is None) else float(lt.volume_l)
            ),
            capacity_l=None if t.capacity_l is None else float(t.capacity_l),
            pressure_mpa=(
                None
                if (lt is None or lt.pressure_mpa is None)
                else float(lt.pressure_mpa)
            ),
            now=now,
            tz=settings.tzinfo,
            reserve_percent=settings.forecast_reserve_percent,
            lead_time_days=settings.forecast_lead_time_days,
            service_level=settings.forecast_service_level,
            relief_mpa=settings.lng_relief_pressure_mpa,
            max_fill_percent=settings.lng_max_fill_percent,
            reading_at=lt.sampled_at if lt else None,
            max_reading_age_days=settings.forecast_max_reading_age_hours / 24.0,
        )
        for fa in f.alerts:
            out.append(Notice(t.psn, t.name, fa.code, fa.severity, fa.message))

    picked = [n for n in out if n.code in NOTIFY_CODES]
    picked.sort(key=lambda n: (_SEV_ORDER.get(n.severity, 9), n.psn, n.code))
    return picked


def notify(session: Session, settings: ConfigLike, now: datetime) -> NotifyStats:
    """Điểm vào duy nhất: gom cảnh báo, lọc theo cửa chặn, gửi một email, ghi log.

    Cấu hình lấy qua ``appconfig.load``, KHÔNG đọc thẳng ``settings``: người vận
    hành đổi địa chỉ nhận trong trang Cài đặt thì vòng cảnh báo kế tiếp phải dùng
    ngay địa chỉ đó, không cần redeploy.
    """
    stats = NotifyStats()
    cfg = load_config(session, settings)
    try:
        notices = collect_notices(session, cfg, now)
    except Exception as exc:
        # Không để lỗi ở tầng cảnh báo làm hỏng vòng ingest.
        log.error("notify: không gom được cảnh báo: %s", exc)
        stats.reason = f"lỗi khi gom cảnh báo: {type(exc).__name__}"
        return stats

    stats.considered = len(notices)
    if not notices:
        return stats

    if not cfg.smtp_ready:
        # Ghi log rồi bỏ qua, KHÔNG raise: hộp thư chưa khai báo là chuyện cấu
        # hình, không phải sự cố dữ liệu.
        stats.reason = "SMTP/người nhận chưa cấu hình"
        log.warning(
            "notify: có %d cảnh báo nhưng %s — bỏ qua gửi email",
            len(notices),
            stats.reason,
        )
        return stats

    window = timedelta(hours=cfg.alert_resend_hours)
    last = notif_repo.last_sent_map(session)
    due = [n for n in notices if _is_due(last.get((n.psn, n.code)), now, window)]
    stats.suppressed = len(notices) - len(due)
    if not due:
        return stats

    subject, body = render_email(due, cfg, now)
    error: str | None = None
    try:
        send_email(cfg, subject, body)
    except Exception as exc:
        # Cùng câu dịch với màn hình Cài đặt: chuỗi này được ghi vào
        # notifications.detail và người vận hành đọc nó ở trang nhật ký thông báo,
        # nên nó phải nói việc cần làm chứ không chỉ nói máy chủ đã từ chối.
        error = explain_smtp(exc)
        log.error("notify: gửi email thất bại: %s", error)

    status = "failed" if error else "sent"
    for n in due:
        notif_repo.record(
            session,
            psn=n.psn,
            code=n.code,
            severity=n.severity,
            status=status,
            message=n.message,
            detail=error,
        )
    if error:
        stats.failed = len(due)
        stats.reason = error
    else:
        stats.sent = len(due)
        log.info(
            "notify: đã gửi %d cảnh báo tới %s", len(due), cfg.alert_email_list
        )
    return stats


def _is_due(last: datetime | None, now: datetime, window: timedelta) -> bool:
    if last is None:
        return True
    if last.tzinfo is None:
        # Cột là timestamptz nên bình thường không xảy ra; guard để một driver
        # trả naive không làm phép so sánh nổ TypeError giữa lúc có sự cố.
        last = last.replace(tzinfo=UTC)
    return (now - last) >= window


def render_email(
    notices: list[Notice], settings: ConfigLike, now: datetime
) -> tuple[str, str]:
    """Soạn tiêu đề + nội dung text thuần.

    Text thuần, không HTML: đọc được trên mọi client kể cả thông báo đẩy của điện
    thoại, và không có ảnh nào bị chặn làm mất nội dung. Tiêu đề đặt mức độ và tên
    bồn lên trước để đọc được ngay trên dòng preview mà không cần mở email.
    """
    crit = [n for n in notices if n.severity == "critical"]
    head = crit[0] if crit else notices[0]
    tag = "NGHIÊM TRỌNG" if crit else "CẢNH BÁO"
    who = head.name or head.psn
    extra = f" (và {len(notices) - 1} cảnh báo khác)" if len(notices) > 1 else ""
    subject = f"[{tag}] {who}: {head.message}{extra}"

    local = now.astimezone(settings.tzinfo)
    lines = [
        f"Thời điểm: {local:%d/%m/%Y %H:%M:%S} (giờ {settings.app_tz})",
        f"Tổng số cảnh báo: {len(notices)}",
        "",
    ]
    for n in notices:
        label = _SEV_LABEL.get(n.severity, n.severity.upper())
        lines.append(f"[{label}] {n.name or n.psn} ({n.psn}) — {n.code}")
        lines.append(f"    {n.message}")
    lines += [
        "",
        f"Hệ thống không nhắc lại cùng một cảnh báo trong {settings.alert_resend_hours} giờ.",
        "Thư tự động từ hệ thống giám sát bồn LNG. Vui lòng không trả lời thư này.",
    ]
    return subject, "\n".join(lines)


def send_email(settings: ConfigLike, subject: str, body: str) -> None:
    """Gửi qua SMTP. Raise nếu thất bại — người gọi quyết định xử lý thế nào.

    ``timeout`` là bắt buộc, không phải tuỳ chọn: hàm này chạy trong một function
    serverless có trần thời gian, và một SMTP không phản hồi sẽ làm cả vòng ingest
    bị kill thay vì chỉ mất một email.
    """
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = ", ".join(settings.alert_email_list)
    msg.set_content(body)

    host, port = settings.smtp_host, settings.smtp_port
    if port == 465:
        # 465 là SMTPS (TLS ngay từ đầu); 587 là SMTP + STARTTLS. Gọi starttls()
        # trên 465 sẽ lỗi, nên phải phân nhánh theo cổng.
        with smtplib.SMTP_SSL(host, port, timeout=20) as s:
            _login_send(s, settings, msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as s:
            if settings.smtp_starttls:
                s.starttls()
            _login_send(s, settings, msg)


def _login_send(s: smtplib.SMTP, settings: ConfigLike, msg: EmailMessage) -> None:
    if settings.smtp_user and settings.smtp_password:
        s.login(settings.smtp_user, settings.smtp_password)
    s.send_message(msg)


def severity_rank(sev: str) -> int:
    """Thứ tự để sắp cảnh báo. Dùng chung với API để hai nơi không sắp khác nhau."""
    return _SEV_ORDER.get(sev, 9)
