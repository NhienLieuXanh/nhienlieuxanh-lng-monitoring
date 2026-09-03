"""Tín hiệu dung lượng telemetry trên /api/health — không cần DB."""

from __future__ import annotations

from app.api.routers.ops import storage_status


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
