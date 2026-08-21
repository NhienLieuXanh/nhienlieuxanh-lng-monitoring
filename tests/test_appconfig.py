"""Test lớp cấu hình hiệu lực: DB ghi đè .env, và bí mật không lọt ra API.

Ba thứ dễ sai thầm lặng ở đây, và cả ba đều test được không cần DB:

1. Property dẫn xuất (``alert_email_list``, ``smtp_ready``) tính trên giá trị .env
   thay vì giá trị người dùng vừa lưu — bug im lặng đúng vào tính năng email.
2. Một field không nằm trong whitelist vẫn ghi đè được -> lỗ hổng sửa
   ``session_secret`` hay credential vendor qua giao diện web.
3. ``smtp_password`` lọt vào response.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.schemas import SettingsIn
from app.config import Settings
from app.services.appconfig import OVERRIDABLE, SECRET_FIELDS, EffectiveConfig


def _env(**kw: object) -> Settings:
    base: dict[str, object] = {
        "app_tz": "Asia/Ho_Chi_Minh",
        "alert_email_to": "env@example.com",
        "alert_resend_hours": 12,
        "smtp_host": "env-smtp.example.com",
        "smtp_port": 587,
        "lng_relief_pressure_mpa": 0.8,
        "forecast_lead_time_days": 1.0,
    }
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Ghi đè
# --------------------------------------------------------------------------- #


def test_db_overrides_env() -> None:
    cfg = EffectiveConfig(_env(), {"lng_relief_pressure_mpa": 1.6})
    assert cfg.lng_relief_pressure_mpa == 1.6
    assert cfg.source_of("lng_relief_pressure_mpa") == "app"
    # Field không đặt thì rơi về .env, và nói rõ nguồn.
    assert cfg.smtp_host == "env-smtp.example.com"
    assert cfg.source_of("smtp_host") == "env"


def test_no_override_reads_exactly_env() -> None:
    env = _env()
    cfg = EffectiveConfig(env, {})
    for name in OVERRIDABLE:
        assert getattr(cfg, name) == getattr(env, name), name


def test_unknown_and_forbidden_keys_are_ignored() -> None:
    """Whitelist là hàng rào thật, không phải tài liệu.

    ``session_secret``/``db_password``/credential vendor KHÔNG được sửa qua giao
    diện web dù ai gửi gì vào bảng app_settings.
    """
    cfg = EffectiveConfig(
        _env(),
        {
            "session_secret": "hacked",
            "db_password": "hacked",
            "admin_token": "hacked",
            "khong_ton_tai": 1,
            "smtp_host": "app-smtp.example.com",
        },
    )
    assert cfg.session_secret == _env().session_secret != "hacked"
    assert cfg.db_password == _env().db_password
    assert cfg.admin_token == _env().admin_token
    assert cfg.smtp_host == "app-smtp.example.com"  # field hợp lệ vẫn ghi đè


# --------------------------------------------------------------------------- #
# Property dẫn xuất PHẢI tính trên giá trị đã override
# --------------------------------------------------------------------------- #


def test_email_list_uses_override_not_env() -> None:
    cfg = EffectiveConfig(
        _env(), {"alert_email_to": "a@x.com, b@x.com , "}
    )
    assert cfg.alert_email_list == ["a@x.com", "b@x.com"]
    # Nếu delegate xuống env thì kết quả sẽ là ["env@example.com"] — đúng cái bug
    # mà test này tồn tại để chặn.
    assert "env@example.com" not in cfg.alert_email_list


def test_smtp_ready_uses_override() -> None:
    base = {"smtp_host": "s.example.com", "smtp_from": "bot@x.com",
            "alert_email_to": "a@x.com", "notify_enabled": True}
    assert EffectiveConfig(_env(), base).smtp_ready is True
    # Tắt thông báo trong app -> không gửi, dù .env đủ điều kiện.
    assert EffectiveConfig(_env(), {**base, "notify_enabled": False}).smtp_ready is False
    # Xoá địa chỉ nhận trong app -> không gửi.
    assert EffectiveConfig(_env(), {**base, "alert_email_to": ""}).smtp_ready is False


def test_tz_and_stale_after_follow_override() -> None:
    cfg = EffectiveConfig(_env(), {"online_stale_minutes": 240})
    assert cfg.stale_after.total_seconds() == 240 * 60
    assert str(cfg.tzinfo) == "Asia/Ho_Chi_Minh"


# --------------------------------------------------------------------------- #
# Bí mật
# --------------------------------------------------------------------------- #


def test_secret_never_in_public_values() -> None:
    cfg = EffectiveConfig(_env(), {"smtp_password": "sieu-mat"})
    pub = cfg.public_values()
    assert "smtp_password" not in pub
    assert "sieu-mat" not in str(pub)
    # Nhưng vẫn biết được là ĐÃ lưu — trang Cài đặt cần hiện "đã lưu / chưa lưu".
    assert cfg.has_secret("smtp_password") is True
    assert EffectiveConfig(_env(), {}).has_secret("smtp_password") is False
    assert "smtp_password" in SECRET_FIELDS


def test_sources_covers_every_overridable_field() -> None:
    cfg = EffectiveConfig(_env(), {})
    assert set(cfg.sources()) == set(OVERRIDABLE)


# --------------------------------------------------------------------------- #
# Validate ở tầng API
# --------------------------------------------------------------------------- #


def test_email_validation_rejects_garbage() -> None:
    """Chặn địa chỉ rác lúc LƯU, không để tới lúc bấm Gửi thử mới biết."""
    for bad in ["khong-co-a-móc", "a@@x.com", "a@localhost", "a b@x.com", "@x.com"]:
        with pytest.raises(ValidationError):
            SettingsIn(alert_email_to=bad)


def test_email_validation_normalises_spacing() -> None:
    v = SettingsIn(alert_email_to="  a@x.com ,b@y.vn  ").alert_email_to
    assert v == "a@x.com, b@y.vn"


def test_service_level_must_be_in_z_table() -> None:
    for bad in (0, 55, 100, 97):
        with pytest.raises(ValidationError):
            SettingsIn(forecast_service_level=bad)
    assert SettingsIn(forecast_service_level=95).forecast_service_level == 95


def test_unknown_field_is_rejected_not_ignored() -> None:
    """extra='forbid': gõ sai tên field bị 422, không im lặng bỏ qua.

    Một trang cấu hình nhận rồi bỏ qua là cách tệ nhất — người dùng tưởng đã lưu.
    """
    with pytest.raises(ValidationError):
        SettingsIn(smtp_hostname="typo.example.com")  # type: ignore[call-arg]


def test_exclude_unset_distinguishes_missing_from_null() -> None:
    """Không gửi field != gửi null. Đây là nền của cơ chế merge ở repository."""
    assert SettingsIn(smtp_host="s.example.com").model_dump(exclude_unset=True) == {
        "smtp_host": "s.example.com"
    }
    assert SettingsIn(smtp_host=None).model_dump(exclude_unset=True) == {
        "smtp_host": None
    }


def test_ranges_are_enforced() -> None:
    with pytest.raises(ValidationError):
        SettingsIn(smtp_port=0)
    with pytest.raises(ValidationError):
        SettingsIn(alert_resend_hours=0)
    with pytest.raises(ValidationError):
        SettingsIn(lng_relief_pressure_mpa=0)
    with pytest.raises(ValidationError):
        SettingsIn(lng_max_fill_percent=101)
    with pytest.raises(ValidationError):
        SettingsIn(truck_capacity_l=0)
