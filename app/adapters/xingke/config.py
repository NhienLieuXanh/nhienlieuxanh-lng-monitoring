"""Setting riêng của vendor Xingke.

Cố ý TÁCH khỏi app/config.py: credential vendor không được lẫn vào Settings lõi,
và tầng core không import file này.
"""

from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class XingkeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="xingke_",
        extra="ignore",
        case_sensitive=False,
    )

    base_url: str = "https://www.xk-iot.cn/ls/"

    # Đã xác minh thực nghiệm 2026-08-18: server render naive timestamp ở UTC+8.
    # Để là SETTING chứ không phải constant: đoán sai thì sửa .env, không sửa code.
    vendor_tz: str = "Asia/Shanghai"

    timeout_seconds: float = 30.0
    connect_timeout_seconds: float = 10.0

    # Throttle lịch sự. Vendor có bảng audit userLoginLog + operateLog.
    min_interval_seconds: float = 1.0

    # Guard chống loop vô hạn đập vào admin API nếu parse sai `total`.
    # Không thương lượng.
    max_pages: int = Field(50, ge=1)

    # 100 = max của UI vendor. Đừng vượt: có thể bị reject, và cũng chính là thứ
    # làm ta bị chú ý.
    page_size: int = Field(100, ge=1, le=100)

    # Token thắng nếu được set: cho phép hotfix qua .env không cần sửa code.
    # Giá trị = localStorage.token trong browser (UUID 36 ký tự, KHÔNG phải JWT
    # nên không decode được expiry).
    token: SecretStr | None = None
    username: str | None = None
    password: SecretStr | None = None

    # Header bổ sung, phủ mọi bất ngờ auth mà không cần thêm class mới.
    extra_headers: dict[str, str] = {}

    # BẮT BUỘC. Thi hành ở ranh giới adapter.
    allowed_psns: str = ""

    probe_psn: str = "2604200016"
    # KHÔNG dùng hôm nay: cả 2 thiết bị đã offline hàng tháng nên queryTime=today
    # trả rỗng và rất dễ chẩn đoán sai thành "auth lỗi".
    probe_date: str = "2026-07-23"

    @field_validator("base_url")
    @classmethod
    def _trailing_slash(cls, v: str) -> str:
        return v if v.endswith("/") else v + "/"

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.vendor_tz)

    @property
    def allowed_psn_set(self) -> frozenset[str]:
        return frozenset(p.strip() for p in self.allowed_psns.split(",") if p.strip())


@lru_cache
def get_xingke_settings() -> XingkeSettings:
    return XingkeSettings()
