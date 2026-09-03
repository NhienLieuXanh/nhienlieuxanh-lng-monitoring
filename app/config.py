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

    # ---- dự báo / kế hoạch nạp ----
    # Cửa sổ lịch sử dùng để suy mức dùng/ngày. 30 ngày phủ được cả biến động
    # theo tuần (cuối tuần dùng ít) mà vẫn không kéo theo dữ liệu quá cũ từ một
    # chế độ vận hành khác.
    forecast_window_days: int = Field(30, ge=1, le=365)
    forecast_reserve_percent: float = 15.0
    # Thời gian từ lúc đặt tới lúc xe tới. Đầu vào của điểm đặt hàng lại.
    forecast_lead_time_days: float = Field(1.0, ge=0)
    # Mức phục vụ cho dự trữ an toàn. 95% là mặc định ngành kho vận.
    forecast_service_level: int = 95
    # Lần đọc cũ hơn mức này thì dự báo bị đánh dấu stale và KHÔNG phát cảnh
    # báo runout/hold time — xem forecast.MAX_READING_AGE_DAYS.
    forecast_max_reading_age_hours: float = Field(24.0, gt=0)
    # Áp suất van an toàn của bồn. Hold time đo từ áp hiện tại tới ngưỡng này —
    # con số này PHẢI khớp thông số bồn thật, mặc định chỉ là điểm khởi đầu.
    lng_relief_pressure_mpa: float = 0.8
    # Trần rót: bồn lạnh sâu phải chừa khoảng hơi cho giãn nở nhiệt.
    lng_max_fill_percent: float = 90.0
    truck_capacity_l: float = 20_000.0

    # ---- thông báo (email) ----
    # SMTP thuần thay vì SDK của một nhà cung cấp: không phát sinh chi phí, không
    # thêm dependency, và dùng được với bất kỳ hộp thư nào công ty đã có.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    alert_email_to: str = ""  # nhiều địa chỉ phân tách bằng dấu phẩy
    # Không gửi lại cùng một (PSN, mã cảnh báo) trong khoảng này. Ingest chạy mỗi
    # 10 phút, nên không có cửa chặn thì một bồn cạn sẽ gửi 144 email/ngày và
    # người nhận sẽ lọc thẳng vào thùng rác — mất luôn tác dụng.
    alert_resend_hours: int = Field(12, ge=1)
    notify_enabled: bool = True

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
    # Neon free 512 MB. Vượt ngưỡng này thì /api/health = degraded (vẫn HTTP 200).
    db_size_warn_mb: float = Field(400.0, gt=0)

    # ---- adapter selection ----
    # Chỉ có adapter THẬT (live). Không còn adapter giả trong sản phẩm — mọi dữ liệu
    # đều từ vendor. Giữ field để override/tương thích, mặc định live.
    xingke_adapter: str = "live"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            # Neon/Vercel/Heroku phát URL scheme `postgres://` hoặc `postgresql://`.
            # SQLAlchemy mặc định map cả hai sang driver psycopg2 (KHÔNG cài trên
            # dự án này — ta dùng psycopg3). Ép về `postgresql+psycopg://` để URL từ
            # nhà cung cấp DÙNG ĐƯỢC NGAY mà không phải sửa tay từng nơi.
            u = self.database_url
            for prefix in ("postgres://", "postgresql://"):
                if u.startswith(prefix) and not u.startswith("postgresql+"):
                    return "postgresql+psycopg://" + u[len(prefix):]
            return u
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
    def alert_email_list(self) -> list[str]:
        return [a.strip() for a in self.alert_email_to.split(",") if a.strip()]

    @property
    def smtp_ready(self) -> bool:
        """Đủ cấu hình để gửi email hay chưa.

        Thiếu cấu hình thì notifier **ghi log rồi bỏ qua** chứ không raise: một
        vòng ingest không được thất bại chỉ vì hộp thư chưa được khai báo.
        """
        return bool(
            self.notify_enabled
            and self.smtp_host
            and (self.smtp_from or self.smtp_user)
            and self.alert_email_list
        )

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
