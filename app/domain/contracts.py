"""Hợp đồng giữa adapter vendor và phần còn lại của platform.

Đây là cột sống. Luật một chiều, thi hành bởi ``tests/test_isolation.py``:

    app/adapters/**  ──imports──>  app/domain
    app/services, app/api, app/repositories  ──imports──>  app/domain
    app/domain  ──KHÔNG BAO GIỜ imports──>  app/adapters/**

Adapter cụ thể được khởi tạo đúng một lần (``app/main.py`` hoặc ``app/cli.py``)
rồi inject. Nhờ đó adapter có thể hoán đổi, và từ vựng vendor không rò lên trên.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_validator

# Cột đo — dùng lại ở mapping, repository và test coverage. Một nguồn sự thật.
MEASURE_FIELDS: tuple[str, ...] = (
    "level_mmwc",
    "diff_pressure_kpa",
    "pressure_mpa",
    "volume_l",
    "volume_percent",
    "temperature_c",
    "vacuum_pa",
    "signal_percent",
    "battery_v",
)


class TerminalStatus(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"


class PercentSource(StrEnum):
    """Nguồn của ``volume_percent``.

    Không bao giờ trộn giá trị vendor gửi và giá trị ta tự tính trong một cột mà
    không có nhãn nguồn — sáu tháng sau không ai nhớ dòng nào là loại nào, và
    mọi cuộc điều tra sai lệch trở nên không thể phản nghiệm.
    """

    VENDOR = "vendor"
    DERIVED = "derived"


class NormalizedTelemetry(BaseModel):
    """Một lần đọc đã chuẩn hoá. Tên field là của CÔNG TY, không phải của vendor."""

    model_config = ConfigDict(frozen=True)

    source: str
    psn: str
    sampled_at: datetime

    # String timestamp gốc của vendor, giữ y nguyên. Tốn ~20 byte/dòng và làm
    # việc sửa timezone về sau thành một câu UPDATE re-derive thuần SQL, KHÔNG
    # cần fetch lại vendor. Vì retention lịch sử của vendor chưa rõ và thiết bị
    # đã offline, string này có thể là thứ duy nhất chắn giữa ta và dữ liệu
    # không thể phục hồi.
    vendor_ts_raw: str | None = None

    level_mmwc: Decimal | None = None
    diff_pressure_kpa: Decimal | None = None
    pressure_mpa: Decimal | None = None
    volume_l: Decimal | None = None
    # LUÔN LUÔN thang 0-100, không bao giờ là phân số 0-1. Xác minh trên dữ liệu
    # thật: vendor gửi volumePercentage=0.59 với currentVolume=61 và
    # cylinderVolume=10425, và 61/10425*100 = 0.5851. Nghĩa là 0.59% ĐẦY.
    volume_percent: Decimal | None = None
    volume_percent_source: PercentSource | None = None
    temperature_c: Decimal | None = None
    vacuum_pa: Decimal | None = None
    signal_percent: Decimal | None = None
    battery_v: Decimal | None = None

    medium_name: str | None = None
    tank_type_name: str | None = None

    # Dung tích danh nghĩa của bồn. Vendor gửi kèm mỗi lần đọc nhưng nó là CẤU
    # HÌNH TÀI SẢN, không phải telemetry — nên nó đi vào bảng terminals, không
    # vào bảng telemetry.
    capacity_l: Decimal | None = None

    # Toạ độ bồn. Cùng lý do như capacity_l: vendor gửi kèm mỗi lần đọc nhưng nó
    # là cấu hình tài sản, nên đi vào bảng terminals chứ không vào telemetry.
    #
    # LUÔN đi theo cặp, hoặc cùng None. Adapter đã loại cặp 0,0 (giá trị module
    # gửi khi mất định vị) và giá trị ngoài khoảng — xem mapping.extract_gps.
    # Đây là dữ liệu "thỉnh thoảng có": cùng một thiết bị có ngày trả toạ độ thật,
    # có ngày trả 0,0.
    latitude: Decimal | None = None
    longitude: Decimal | None = None

    raw_payload: dict[str, Any]

    @field_validator("sampled_at")
    @classmethod
    def _must_be_aware(cls, v: datetime) -> datetime:
        """Từ chối datetime naive.

        Đây là guardrail quan trọng nhất trong file này. Vendor gửi naive string
        ('2026-07-23 16:03:29') render ở UTC+8. Nếu adapter để một naive datetime
        lọt qua đây thì khoá dedup (psn, sampled_at) được xây từ instant SAI —
        và khi sửa parsing về sau, mọi dòng đã sửa có khoá KHÁC nên ON CONFLICT
        không match và toàn bộ dữ liệu lịch sử bị nhân đôi âm thầm. Sai timezone
        ở đây không phải lỗi hiển thị, nó làm hỏng identity.
        """
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError(
                "sampled_at phải tz-aware. Adapter chịu trách nhiệm gắn timezone "
                "của vendor rồi convert sang UTC (xem mapping.parse_vendor_ts)."
            )
        return v


class NormalizedTerminal(BaseModel):
    """Metadata thiết bị/bồn. Hợp từ NHIỀU endpoint vendor.

    Lưu ý: ``modem_number`` và ``sim_iccid`` chỉ có trên endpoint telemetry, còn
    version firmware chỉ có trên endpoint device list — và hai endpoint đó viết
    ``hardwareVersion`` / ``hardwarVersion`` khác nhau. Adapter hợp nhất chúng;
    tầng này không cần biết.
    """

    model_config = ConfigDict(frozen=True)

    psn: str
    name: str | None = None
    modem_number: str | None = None
    sim_iccid: str | None = None
    hardware_version: str | None = None
    software_version: str | None = None
    capacity_l: Decimal | None = None
    medium_name: str | None = None
    tank_type_name: str | None = None
    raw_payload: dict[str, Any] = {}


@dataclass(slots=True)
class MappingReport:
    """Chất lượng mapping của một lần fetch.

    Trả về object thay vì chỉ log, để (a) test assert được và (b) persist vào
    ``ingest_runs.mapping_report`` — nhờ đó khoảng trống tự nổi lên qua endpoint
    admin mà không ai phải đọc log.
    """

    n_rows: int = 0
    present: dict[str, int] = field(default_factory=dict)
    resolved_from: dict[str, str] = field(default_factory=dict)
    unmapped_keys: set[str] = field(default_factory=set)
    rejected_rows: int = 0
    dropped_foreign_psn: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)

    def coverage(self) -> str:
        mapped = sum(1 for f in MEASURE_FIELDS if self.present.get(f, 0) > 0)
        return f"{mapped}/{len(MEASURE_FIELDS)}"

    def always_null(self) -> list[str]:
        return [f for f in MEASURE_FIELDS if self.present.get(f, 0) == 0]

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_rows": self.n_rows,
            "coverage": self.coverage(),
            "present": self.present,
            "resolved_from": self.resolved_from,
            "unmapped_keys": sorted(self.unmapped_keys),
            "rejected_rows": self.rejected_rows,
            "dropped_foreign_psn": self.dropped_foreign_psn,
            "always_null": self.always_null(),
            "errors": [{"field": f, "error": e} for f, e in self.errors],
        }


@dataclass(slots=True)
class FetchResult:
    readings: list[NormalizedTelemetry] = field(default_factory=list)
    total: int | None = None
    pages_fetched: int = 0
    report: MappingReport = field(default_factory=MappingReport)


@runtime_checkable
class TelemetryPort(Protocol):
    """Cái mà ingestion service biết về thế giới bên ngoài. Chỉ có thế này."""

    source: str

    def fetch_telemetry(self, psn: str, day: date) -> FetchResult:
        """Lấy các lần đọc của ``psn`` trong ngày ``day``.

        ``day`` là ngày lịch theo giờ VENDOR — adapter chịu trách nhiệm quy đổi.
        Trả về 0 dòng KHÔNG phải lỗi: thiết bị offline là trạng thái bình thường
        và phải báo được, không được coi là failure.
        """
        ...

    def fetch_devices(self, psns: list[str]) -> list[NormalizedTerminal]:
        """Lấy metadata cho đúng những PSN được yêu cầu.

        Nhận danh sách PSN tường minh, KHÔNG phải "liệt kê tất cả": endpoint
        device list của vendor bỏ qua org scope và trả về thiết bị của mọi khách
        hàng (đã đo: 3543 bản ghi). Ký hiệu hàm này cố ý làm cho việc lấy hết
        trở nên không diễn đạt được.
        """
        ...

    def close(self) -> None: ...
