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
    # Cổng là ASP.NET và render ngày bằng ``DateTime.ToString()``, tức
    # ``CultureInfo.CurrentCulture`` — mà request localization lấy culture từ
    # ``Accept-Language``. Client không gửi header đó thì cổng dùng culture mặc
    # định của nó và trả mm/dd, trong khi browser lúc discovery gửi tiếng Việt và
    # nhận dd/mm. Đo được trên production 2026-09-03: cùng một endpoint trả
    # "09/03/2026" cho ngày 3 tháng 9, còn capture qua browser là "27/08/2026".
    # Ghim header để định dạng trên đường truyền là XÁC ĐỊNH, không phải đoán.
    accept_language: str = "vi-VN,vi;q=0.9"
    # Thứ tự ngày/tháng cổng thực sự gửi. Mặc định ``dmy`` vì đó là những gì
    # capture discovery ghi được; production ngày 2026-09-03 đo được ``mdy``, nên
    # môi trường nào thấy mm/dd phải đặt YOKOHAMA_TIMESTAMP_ORDER=mdy. Không tự
    # đoán: xem ghi chú ở mapping.TIMESTAMP_ORDERS.



    timeout_seconds: float = 30.0
    connect_timeout_seconds: float = 10.0
    max_stream_bytes: int = Field(8 * 1024 * 1024, ge=64)
    # Trần này phải NHỎ HƠN HẲN ngân sách của cả function, không bằng nó.
    # ``vercel.json`` cho ``app/main.py`` 60 s cho TOÀN BỘ một cycle: fetch nguồn
    # kia, stream nguồn này, lấy báo động, rồi ghi DB. Đặt trần stream bằng đúng
    # 60 s nghĩa là riêng nó có thể ăn hết ngân sách và function bị kill giữa
    # cycle — mất luôn dữ liệu nguồn kia, thứ đang chạy được.
    # Chọn một nửa. Nếu 25 s không đủ cho cửa sổ đang cấu hình thì stream bị cắt
    # và báo lỗi schema, mà lỗi đó KHÔNG fatal — nguồn này ngừng, nguồn kia vẫn
    # nạp. Đó là chiều hỏng đúng.
    # ``tests/test_deploy_budget.py`` giữ quan hệ này khỏi lệch âm thầm.
    max_stream_seconds: float = Field(25.0, gt=0)

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
