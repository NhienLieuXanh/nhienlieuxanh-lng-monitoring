"""Cấu hình lõi của platform.

Cố ý KHÔNG chứa tên vendor nào ngoài một chuỗi chọn adapter. Credential và
setting của vendor nằm ở ``app/adapters/xingke/config.py`` để tầng core không
bao giờ phải biết ta đang nói chuyện với ai.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- app ----
    app_env: str = "dev"
    app_tz: str = "Asia/Ho_Chi_Minh"
    log_level: str = "INFO"

    # ---- database ----
    # Field rời thay vì một DSN: password chứa @ : / # % sẽ âm thầm phá DSN viết
    # tay, và pydantic.PostgresDsn percent-decode theo cách gây bất ngờ.
    # sqlalchemy.URL.create escape đúng.
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "xingke"
    db_user: str = "xingke_app"
    db_password: str = ""
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_echo: bool = False
    database_url: str | None = None  # override; thắng khi được set

    # ---- ingestion ----
    scheduler_enabled: bool = True
    ingest_interval_minutes: int = Field(10, ge=1)
    ingest_jitter_seconds: int = Field(30, ge=0)
    # Fetch hôm nay + N ngày trước, tính theo giờ VENDOR. Vendor UTC+8, công ty
    # UTC+7, lưu UTC — "hôm nay" nhập nhằng qua ba múi giờ. Cửa sổ 2 ngày phủ
    # mọi cách hiểu mà core service không cần biết TZ của vendor.
    ingest_days_back: int = Field(1, ge=0)
    ingest_on_startup: bool = False
    ingest_max_consecutive_failures: int = Field(10, ge=1)
    store_raw_payload: bool = True

    # ---- status / alert ----
    # 90 phút = 3 sample bị mất. Cadence vendor đo từ dữ liệu thật là 30 phút,
    # và thiết bị báo signal 15-20% nên mất upload là bình thường, không phải
    # ngoại lệ. Ngưỡng này CỐ Ý tách rời ingest_interval_minutes — nhập nhằng
    # hai cái là lỗi kinh điển làm status flap.
    online_stale_minutes: int = Field(90, ge=1)
    alert_low_volume_percent: float = 15.0
    # 3.6/3.64 V là bình thường với pin lithium primary 3.6 V còn tốt; ngưỡng
    # 3.5 V ngây thơ sẽ báo động trên thiết bị lành.
    alert_low_battery_v: float = 3.40
    alert_low_signal_percent: float = 10.0
    default_tank_capacity_l: float = 10425.0

    # ---- api ----
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    cors_origins: str = ""  # rỗng => same-origin, không phát header CORS
    expose_raw_payload: bool = False
    admin_token: str = ""
    session_secret: str = "change-me-session-secret"
    session_hours: int = Field(12, ge=1, le=168)
    max_history_limit: int = Field(1000, ge=1)
    max_history_span_days: int = Field(90, ge=1)

    # ---- adapter selection ----
    # Chỉ TÊN adapter nằm ở core; không setting nào của vendor lọt vào đây.
    xingke_adapter: str = "fake"  # "fake" | "live"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        return URL.create(
            "postgresql+psycopg",
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        ).render_as_string(hide_password=False)

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.app_tz)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_dev(self) -> bool:
        return self.app_env == "dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def assert_venv() -> None:
    """Từ chối chạy ngoài venv.

    Global site-packages của máy này CÓ sqlalchemy/alembic/pandas nhưng khác
    version với requirements.txt. Không có assert này thì ``import sqlalchemy``
    vẫn thành công và bạn sẽ debug hành vi khác nhau giữa hai shell.
    """
    if sys.prefix == sys.base_prefix:
        raise RuntimeError(
            "Đang chạy ngoài virtualenv. Global site-packages có sqlalchemy/"
            "alembic nhưng SAI version. Dùng: .\\.venv\\Scripts\\python.exe -m ..."
        )
