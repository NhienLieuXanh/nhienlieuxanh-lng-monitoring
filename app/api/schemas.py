"""Response model của API nội bộ. Đây là firewall chống rò tên vendor.

Không bao giờ trả ORM object trực tiếp. Ba thứ TUYỆT ĐỐI không được ra khỏi đây:

  * ``raw_payload``  — JSON vendor nguyên bản, key tiếng Trung/pinyin. Vector rò
                       nghiêm trọng nhất: một ``response_model=None`` bất cẩn là đủ.
  * ``source``       — giá trị 'xingke' rò chính tên vendor.
  * text exception   — có thể nhúng URL vendor, PSN, hoặc token.

Enforce bằng ``tests/test_isolation.py`` chứ không bằng code review.

Tên field ở ``TerminalOut`` cố ý KHỚP ĐÚNG những gì dashboard prototype đang đọc,
để không cần một lớp mapping trong JS.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TelemetryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    psn: str
    sampled_at: datetime
    level_mmwc: Decimal | None = None
    diff_pressure_kpa: Decimal | None = None
    pressure_mpa: Decimal | None = None
    volume_l: Decimal | None = None
    # Thang 0-100. 0.59 nghĩa là 0.59% đầy.
    volume_percent: Decimal | None = None
    temperature_c: Decimal | None = None
    vacuum_pa: Decimal | None = None
    signal_percent: Decimal | None = None
    battery_v: Decimal | None = None
    medium_name: str | None = None
    tank_type_name: str | None = None


class TerminalOut(BaseModel):
    psn: str
    name: str | None = None
    status: Literal["online", "offline"]
    last_seen_at: datetime | None = None
    capacity_l: Decimal | None = None
    medium_name: str | None = None
    tank_type_name: str | None = None

    # Số vendor gửi.
    volume_l: Decimal | None = None
    volume_percent: Decimal | None = None
    # Số server TỰ tính từ volume_l/capacity_l*100. Phát cả hai là cơ chế duy nhất
    # phát hiện được lỗi thang 0-1 vs 0-100: không CHECK constraint nào bắt được vì
    # 0.59 hợp lệ ở cả hai thang, chỉ có sự KHÔNG KHỚP giữa hai số tính độc lập mới
    # phát hiện được. Dashboard vẽ CON SỐ NÀY.
    fill_percent: Decimal | None = None

    pressure_mpa: Decimal | None = None
    temperature_c: Decimal | None = None
    battery_v: Decimal | None = None
    signal_percent: Decimal | None = None
    level_mmwc: Decimal | None = None
    diff_pressure_kpa: Decimal | None = None
    vacuum_pa: Decimal | None = None
    sampled_at: datetime | None = None


class TerminalUpdateIn(BaseModel):
    """Field do người vận hành sở hữu. Ingest KHÔNG ghi đè các cột này.

    Ít nhất một field phải được gửi. ``name`` trống sau khi strip bị từ chối —
    không cho phép "xoá tên" thành chuỗi trắng rồi lần ingest sau không điền lại
    (ingest chỉ coalesce vào chỗ NULL).
    """

    name: str | None = Field(None, min_length=1, max_length=128)
    capacity_l: Decimal | None = Field(None, gt=0)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = v.strip()
        if not s:
            raise ValueError("name không được để trống")
        return s

    @model_validator(mode="after")
    def _at_least_one(self) -> TerminalUpdateIn:
        if self.name is None and self.capacity_l is None:
            raise ValueError("cần ít nhất một field: name hoặc capacity_l")
        return self


class TerminalDetailOut(TerminalOut):
    id: UUID
    modem_number: str | None = None
    sim_iccid: str | None = None
    hardware_version: str | None = None
    software_version: str | None = None
    device_model: str | None = None
    device_type_name: str | None = None
    created_at: datetime
    updated_at: datetime


class Page[T](BaseModel):
    items: list[T]
    page: int
    limit: int
    total: int
    has_next: bool


class AlertOut(BaseModel):
    psn: str
    code: str
    severity: Literal["critical", "warning", "info"]
    message: str
    value: Decimal | None = None
    threshold: Decimal | None = None


class SummaryOut(BaseModel):
    """Bốn stat tile của dashboard, tính phía server.

    Tính ở đây chứ không trong JS để `alert` là kết quả của rule alert thật (xem
    domain/alerts.py) thay vì placeholder "coi offline là cảnh báo" của prototype.
    """

    total: int
    online: int
    offline: int
    alert: int
    critical: int = 0
    total_volume_l: Decimal | None = None
    generated_at: datetime


class CheckOut(BaseModel):
    ok: bool
    detail: str | None = None


class HealthOut(BaseModel):
    # 200 cho cả ok và degraded, 503 CHỈ cho error — nhờ vậy monitor phân biệt được
    # "API sống nhưng ingestion tắc" với "database mất". Gộp cả hai thành 503 là
    # phá tín hiệu đó.
    status: Literal["ok", "degraded", "error"]
    version: str
    time: datetime
    database: CheckOut
    migration: CheckOut
    ingest: CheckOut
    # Số thiết bị online KHÔNG ảnh hưởng status: sức khoẻ thiết bị không phải sức
    # khoẻ platform. Cả hai thiết bị thật đang offline hàng tháng.
    terminals_total: int = 0
    terminals_online: int = 0
    terminals_offline: int = 0
    last_ingest_at: datetime | None = None
    last_ingest_age_seconds: float | None = None
    scheduler_enabled: bool = False
    ingest_paused_reason: str | None = None


class IngestRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trigger: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    fetched: int
    inserted: int
    # Con số này SẼ rất lớn và đó là hoạt động đúng: endpoint vendor trả theo NGÀY,
    # nên mỗi vòng poll 10 phút refetch lại cả ngày. Xem README.
    duplicates: int
    terminals_created: int
    error_count: int
    error_summary: str | None = None


class IngestRunDetailOut(IngestRunOut):
    """Chỉ dùng ở /api/admin/* — mapping_report có thể chứa tên field vendor."""

    params: dict = Field(default_factory=dict)
    mapping_report: dict = Field(default_factory=dict)


class ActionOut(BaseModel):
    ok: bool
    message: str


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    username: str
