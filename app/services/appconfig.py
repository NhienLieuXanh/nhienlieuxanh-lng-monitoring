"""Cấu hình hiệu lực = giá trị .env + phần người vận hành đặt trong app.

Vì sao tồn tại: ban đầu mọi thứ (email nhận cảnh báo, ngưỡng gửi lại, áp van an
toàn, lead time...) là biến môi trường. Đổi một địa chỉ email phải sửa env trên
Vercel rồi redeploy — với thứ thay đổi thường xuyên thì đó là thiết kế sai. Module
này để tầng gọi (notifier, router dự báo, export) **không cần biết** một giá trị
đến từ .env hay từ trang Cài đặt.

Thứ tự ưu tiên: **DB thắng .env**. Lý do: DB là thứ người dùng vừa bấm trong app,
nên nó phải là tiếng nói cuối cùng; .env là giá trị khởi tạo và là đường cấu hình
cho người triển khai trước khi có ai đăng nhập lần đầu.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import Settings
from app.repositories import app_settings as store

log = logging.getLogger(__name__)


#: Các field người vận hành được phép đặt trong app. Whitelist tường minh, KHÔNG
#: phải "mọi field của Settings": db_password, session_secret, admin_token và
#: credential vendor tuyệt đối không được sửa qua giao diện web.
OVERRIDABLE: tuple[str, ...] = (
    # --- thông báo ---
    "notify_enabled",
    "alert_email_to",
    "alert_resend_hours",
    "smtp_host",
    "smtp_port",
    "smtp_user",
    "smtp_password",
    "smtp_from",
    "smtp_starttls",
    # --- dự báo / kế hoạch ---
    "forecast_window_days",
    "forecast_reserve_percent",
    "forecast_lead_time_days",
    "forecast_service_level",
    "forecast_max_reading_age_hours",
    "lng_relief_pressure_mpa",
    "lng_max_fill_percent",
    "truck_capacity_l",
    # --- ngưỡng cảnh báo thiết bị ---
    "online_stale_minutes",
    "alert_low_volume_percent",
    "alert_low_battery_v",
    "alert_low_signal_percent",
)

#: Field là bí mật: ghi được, KHÔNG BAO GIỜ đọc ra khỏi API. Trang Cài đặt chỉ
#: thấy "đã lưu / chưa lưu". Cùng nguyên tắc với raw_payload ở api/schemas.py —
#: chặn ở nơi dữ liệu đi ra, không dựa vào kỷ luật của người sửa code sau.
SECRET_FIELDS = frozenset({"smtp_password"})


class EffectiveConfig:
    """Đọc như một ``Settings`` bình thường, nhưng field bị override thì lấy từ DB.

    Dùng ``__getattr__`` để mọi chỗ đang viết ``settings.x`` chạy nguyên xi — không
    phải sửa hàng chục call site. Hai property dẫn xuất (``alert_email_list``,
    ``smtp_ready``) BẮT BUỘC định nghĩa lại ở đây: nếu để delegate xuống env thì
    chúng sẽ tính trên ``alert_email_to`` của .env chứ không phải giá trị người
    dùng vừa lưu — một cái bug im lặng đúng vào tính năng quan trọng nhất.
    """

    __slots__ = ("_env", "_over")

    def __init__(self, env: Settings, overrides: dict[str, Any] | None = None) -> None:
        self._env = env
        self._over = {
            k: v for k, v in (overrides or {}).items() if k in OVERRIDABLE
        }

    def __getattr__(self, name: str) -> Any:
        if name in self._over:
            return self._over[name]
        return getattr(self._env, name)

    # --- property dẫn xuất phải tính trên giá trị ĐÃ override ---

    @property
    def alert_email_list(self) -> list[str]:
        raw = str(self.alert_email_to or "")
        return [a.strip() for a in raw.split(",") if a.strip()]

    @property
    def smtp_ready(self) -> bool:
        return bool(
            self.notify_enabled
            and self.smtp_host
            and (self.smtp_from or self.smtp_user)
            and self.alert_email_list
        )

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.app_tz)

    @property
    def stale_after(self) -> timedelta:
        return timedelta(minutes=int(self.online_stale_minutes))

    # --- tiện ích cho trang Cài đặt ---

    def source_of(self, name: str) -> str:
        """'app' nếu người vận hành đã đặt, 'env' nếu đang lấy từ biến môi trường.

        Hiện trên giao diện để trả lời được "vì sao giá trị này lại thế" — câu hỏi
        đầu tiên của bất kỳ ai mở một trang cấu hình có hai nguồn.
        """
        return "app" if name in self._over else "env"

    def public_values(self) -> dict[str, Any]:
        """Giá trị hiệu lực của mọi field, ĐÃ loại bí mật."""
        return {
            n: getattr(self, n) for n in OVERRIDABLE if n not in SECRET_FIELDS
        }

    def sources(self) -> dict[str, str]:
        return {n: self.source_of(n) for n in OVERRIDABLE}

    def has_secret(self, name: str) -> bool:
        return bool(self._over.get(name) or getattr(self._env, name, ""))


#: Nhận cả Settings thuần lẫn EffectiveConfig — hai thứ có cùng bề mặt ĐỌC. Hàm
#: nào chỉ đọc cấu hình thì khai kiểu này để dùng được với cả hai: test truyền
#: Settings dựng tay, đường chạy thật truyền EffectiveConfig lấy từ DB.
ConfigLike = Settings | EffectiveConfig


def load_config(session: Session, env: Settings) -> EffectiveConfig:
    """Cấu hình hiệu lực. Lỗi đọc DB KHÔNG được làm chết đường gọi.

    Bảng chưa migrate hoặc DB chớp nhoáng thì rơi về .env và ghi log, vì cấu hình
    là thứ phụ trợ: một trang Cài đặt chưa tạo bảng không được phép làm sập
    dashboard hay chặn vòng ingest.
    """
    try:
        return EffectiveConfig(env, store.load(session))
    except Exception as exc:
        session.rollback()
        log.warning("appconfig: không đọc được app_settings (%s) — dùng .env", exc)
        return EffectiveConfig(env, {})
