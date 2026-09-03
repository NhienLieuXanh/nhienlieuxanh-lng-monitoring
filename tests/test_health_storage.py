"""Các tín hiệu của /api/health — hàm thuần, không cần DB.

Cố ý không cần DB: test cần DB tự skip khi thiếu TEST_DATABASE_URL, nên trên CI
chúng không chạy. Hai tín hiệu ở đây (dung lượng, độ mới của ingest) là thứ phải
được canh ở MỌI lần chạy suite.
"""

from __future__ import annotations

from app.api.routers.ops import ingest_freshness, storage_status
from app.config import Settings


def test_storage_ok_under_threshold() -> None:
    check = storage_status(10 * 1024 * 1024, warn_mb=400)
    assert check.ok is True


def test_storage_degraded_over_threshold() -> None:
    check = storage_status(500 * 1024 * 1024, warn_mb=400)
    assert check.ok is False
    assert check.detail is not None
    assert "400" in check.detail


def test_storage_unknown_size_is_not_error() -> None:
    check = storage_status(None, warn_mb=400)
    assert check.ok is True


# --------------------------------------------------------------------------- #
# Độ mới của ingest
# --------------------------------------------------------------------------- #


def test_ingest_fresh_is_ok() -> None:
    assert ingest_freshness(30 * 60, stale_after_minutes=720).ok is True


def test_ingest_never_ran_is_distinct_from_stale() -> None:
    """Chưa từng chạy được khác hẳn đã ngừng chạy — thông điệp phải nói rõ."""
    check = ingest_freshness(None, stale_after_minutes=720)
    assert check.ok is False
    assert check.detail is not None
    assert "chưa có" in check.detail


def test_the_53_hour_outage_would_now_be_caught() -> None:
    """Chốt chặn đúng sự cố thật đã lọt qua.

    Production báo ``ingest ok=True`` với ``last_ingest_age_seconds = 192233``
    (53,4 giờ) vì ngưỡng khi đó là ``ingest_interval_minutes × 3``, và
    INGEST_INTERVAL_MINUTES bị đặt 1440 hồi chỉ có cron ngày của Vercel — ngưỡng
    thành 72 giờ. Bài này giữ cho đúng con số đó không bao giờ lọt lại.
    """
    check = ingest_freshness(192233.7, stale_after_minutes=720)
    assert check.ok is False
    assert check.detail is not None
    assert "3204" in check.detail  # 192233 s = 3204 phút
    assert "720" in check.detail


def test_default_threshold_is_above_worst_observed_cron_gap() -> None:
    """Mặc định phải trên hẳn khoảng cách cron xấu nhất đã ĐO, không phải đoán.

    GitHub siết lịch ``*/30``: đo trên 30 lần chạy gần nhất, khoảng cách min 121 /
    trung vị 242 / max 440 phút. Ngưỡng dưới 440 sẽ báo động oan mỗi lần GitHub
    trễ; ngưỡng bằng cả ngày thì vô dụng như cũ.
    """
    worst_observed_gap_minutes = 440
    threshold = Settings(
        app_env="test", db_password="x", scheduler_enabled=False
    ).ingest_stale_after_minutes
    assert threshold > worst_observed_gap_minutes
    assert threshold < 24 * 60
    # Và nó KHÔNG được suy từ nhịp scheduler nữa.
    assert ingest_freshness(
        worst_observed_gap_minutes * 60 + 1, stale_after_minutes=threshold
    ).ok is True
