"""Setting riêng của nguồn đo phút. Tách khỏi app/config.py cùng lý do Xingke."""

from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class YokohamaSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="yokohama_",
        extra="ignore",
        case_sensitive=False,
    )

    # Tắt mặc định: bật tường minh trên môi trường đã biết nguồn này.
    enabled: bool = False
    # Rỗng trong repo: địa chỉ nội bộ không được hard-code. Bật nguồn thì phải
    # đặt YOKOHAMA_BASE_URL trong .env (file đó đã gitignore).
    base_url: str = ""
    psn: str = "YKH-TANK-01"
    site_code: str = "YKH"
    # Đo trực tiếp: LAST UPDATE lệch +6,99 h so với UTC.
    vendor_tz: str = "Asia/Ho_Chi_Minh"
    timeout_seconds: float = 30.0
    connect_timeout_seconds: float = 10.0
    max_stream_bytes: int = Field(8 * 1024 * 1024, ge=64)
    # 20 s chưa đo với stream cả ngày (~1,9 MB). Trần byte mới là chốt thật.
    max_stream_seconds: float = Field(60.0, gt=0)

    @model_validator(mode="after")
    def _enabled_needs_url(self) -> YokohamaSettings:
        if self.enabled and not self.base_url.strip():
            raise ValueError(
                "đặt YOKOHAMA_BASE_URL trong .env; địa chỉ nội bộ không được ghi vào repo"
            )
        return self

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.vendor_tz)

    @property
    def psn_list(self) -> list[str]:
        p = self.psn.strip()
        return [p] if p else []


@lru_cache
def get_yokohama_settings() -> YokohamaSettings:
    return YokohamaSettings()
