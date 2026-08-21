"""Test phần thuần của notifier: cửa chặn gửi lại, soạn email, lọc mã.

Cố ý KHÔNG chạm SMTP: ``send_email`` là I/O thuần, còn ba thứ dễ sai thầm lặng là
(1) cửa chặn gửi lại tính sai giờ, (2) tiêu đề email không nói được chuyện gì,
(3) mã cảnh báo hạ tầng lọt ra ngoài làm loãng cảnh báo gấp. Ba thứ đó test được
mà không cần mạng.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import Settings
from app.services.notifier import (
    NOTIFY_CODES,
    Notice,
    NotifyStats,
    _is_due,
    render_email,
    severity_rank,
)

UTC = ZoneInfo("UTC")
NOW = datetime(2026, 8, 21, 8, 0, tzinfo=UTC)


def _settings(**kw: object) -> Settings:
    base: dict[str, object] = {
        "app_tz": "Asia/Ho_Chi_Minh",
        "alert_resend_hours": 12,
        "smtp_host": "smtp.example.com",
        "smtp_from": "bot@example.com",
        "alert_email_to": "ops@example.com, boss@example.com",
    }
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Cấu hình
# --------------------------------------------------------------------------- #


def test_email_list_parsed_and_trimmed() -> None:
    s = _settings()
    assert s.alert_email_list == ["ops@example.com", "boss@example.com"]
    assert s.smtp_ready is True


def test_smtp_not_ready_when_missing_pieces() -> None:
    """Thiếu bất kỳ mảnh nào -> không gửi. Thà im lặng có log hơn là nổ giữa cron."""
    assert _settings(smtp_host="").smtp_ready is False
    assert _settings(alert_email_to="").smtp_ready is False
    assert _settings(smtp_from="", smtp_user="").smtp_ready is False
    assert _settings(notify_enabled=False).smtp_ready is False
    # Không có smtp_from nhưng có smtp_user thì vẫn gửi được (From = user).
    assert _settings(smtp_from="", smtp_user="bot@example.com").smtp_ready is True


# --------------------------------------------------------------------------- #
# Cửa chặn gửi lại
# --------------------------------------------------------------------------- #


def test_resend_window() -> None:
    w = timedelta(hours=12)
    assert _is_due(None, NOW, w) is True  # chưa từng gửi
    assert _is_due(NOW - timedelta(hours=11, minutes=59), NOW, w) is False
    assert _is_due(NOW - timedelta(hours=12), NOW, w) is True
    assert _is_due(NOW - timedelta(days=3), NOW, w) is True


def test_resend_window_tolerates_naive_timestamp() -> None:
    """Cột là timestamptz, nhưng một driver trả naive không được làm nổ TypeError."""
    naive = (NOW - timedelta(days=1)).replace(tzinfo=None)
    assert _is_due(naive, NOW, timedelta(hours=12)) is True


# --------------------------------------------------------------------------- #
# Lọc mã
# --------------------------------------------------------------------------- #


def test_notify_codes_exclude_infrastructure_noise() -> None:
    """WEAK_SIGNAL / PERCENT_MISMATCH là việc của người vận hành platform.

    Để chúng vào email sẽ làm loãng RUNOUT và HOLD_TIME — hai mã thật sự cần
    người ta hành động ngay.
    """
    assert "RUNOUT" in NOTIFY_CODES
    assert "HOLD_TIME" in NOTIFY_CODES
    assert "LOW_VOLUME" in NOTIFY_CODES
    assert "WEAK_SIGNAL" not in NOTIFY_CODES
    assert "PERCENT_MISMATCH" not in NOTIFY_CODES


def test_severity_rank_orders_critical_first() -> None:
    assert severity_rank("critical") < severity_rank("warning") < severity_rank("info")
    assert severity_rank("gì đó lạ") > severity_rank("info")


# --------------------------------------------------------------------------- #
# Soạn email
# --------------------------------------------------------------------------- #


def test_subject_leads_with_critical_and_counts_the_rest() -> None:
    notices = [
        Notice(
            "2604200016",
            "Bồn A - Long An",
            "RUNOUT",
            "critical",
            "Còn 0.8 ngày tới mức dự trữ (1.56 m³) — cần đặt hàng",
        ),
        Notice("2605090007", None, "HOLD_TIME", "warning", "Hold time còn 3.2 ngày"),
    ]
    subject, body = render_email(notices, _settings(), NOW)

    # Tên bồn và nội dung nằm ngay trên dòng preview, không cần mở email.
    assert subject.startswith("[NGHIÊM TRỌNG] Bồn A - Long An:")
    assert "Còn 0.8 ngày" in subject
    assert "(và 1 cảnh báo khác)" in subject

    # Giờ hiển thị là giờ Việt Nam (08:00 UTC -> 15:00 ICT), không phải UTC.
    assert "21/08/2026 15:00:00" in body
    assert "Asia/Ho_Chi_Minh" in body
    # Bồn chưa đặt tên thì rơi về PSN, không để trống.
    assert "2605090007 (2605090007)" in body
    assert "Tổng số cảnh báo: 2" in body
    assert "12 giờ" in body  # nói rõ cửa chặn gửi lại


def test_subject_says_canh_bao_when_nothing_critical() -> None:
    notices = [Notice("X", "Bồn X", "OFFLINE", "warning", "Thiết bị offline")]
    subject, body = render_email(notices, _settings(), NOW)
    assert subject.startswith("[CẢNH BÁO] Bồn X:")
    assert "+" not in subject  # một cảnh báo thì không có đuôi "+n"
    assert "[CẢNH BÁO] Bồn X (X) — OFFLINE" in body


def test_stats_summary_readable() -> None:
    s = NotifyStats(considered=5, suppressed=3, sent=2)
    assert "gửi=2" in s.summary() and "bị chặn=3" in s.summary()
    s2 = NotifyStats(
        considered=1, failed=1, reason="SMTPAuthenticationError: sai mật khẩu"
    )
    assert "SMTPAuthenticationError" in s2.summary()
