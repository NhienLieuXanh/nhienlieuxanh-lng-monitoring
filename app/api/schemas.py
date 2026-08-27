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

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)


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

    # Toạ độ bồn cho trang Bản đồ. Người vận hành nhập, không phải GPS vendor —
    # module trả 0,0 khi mất định vị, xem db/models.py. NULL cả hai nghĩa là chưa
    # khai; DB cấm trạng thái nửa vời nên JS không phải xử lý ca đó.
    latitude: Decimal | None = None
    longitude: Decimal | None = None

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

    # Toạ độ dùng ngữ nghĩa KHÁC name/capacity_l: ở đây ``null`` tường minh có
    # nghĩa là XOÁ toạ độ, không phải "không gửi". Cần vậy vì ghim sai vị trí thì
    # phải bỏ ghim được, mà `null` = "bỏ qua" thì không có đường nào bỏ. Phân biệt
    # "gửi null" với "không gửi" bằng ``model_fields_set``.
    latitude: Decimal | None = Field(None, ge=Decimal("-90"), le=Decimal("90"))
    longitude: Decimal | None = Field(None, ge=Decimal("-180"), le=Decimal("180"))

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = v.strip()
        if not s:
            raise ValueError("name không được để trống")
        return s

    @property
    def location_sent(self) -> bool:
        """True nếu client CÓ gửi toạ độ (kể cả gửi null để xoá)."""
        return bool(self.model_fields_set & {"latitude", "longitude"})

    @model_validator(mode="after")
    def _check(self) -> TerminalUpdateIn:
        sent = self.model_fields_set
        has_lat, has_lon = "latitude" in sent, "longitude" in sent
        if has_lat != has_lon:
            # Gửi một nửa thì không có cách nào đoán đúng: DB cấm trạng thái nửa
            # vời, nên từ chối ở đây thay vì để 500 từ CHECK constraint.
            raise ValueError("toạ độ phải gửi cả latitude và longitude")
        if has_lat:
            if (self.latitude is None) != (self.longitude is None):
                raise ValueError(
                    "latitude và longitude phải cùng có giá trị, hoặc cùng null để xoá"
                )
            if self.latitude == 0 and self.longitude == 0:
                # 0,0 là giá trị module gửi khi MẤT định vị, không phải một vị trí.
                # Nhận nó thì bản đồ đặt bồn ở giữa vịnh Guinea.
                raise ValueError("0,0 không phải vị trí — đó là giá trị khi mất định vị")

        if self.name is None and self.capacity_l is None and not self.location_sent:
            raise ValueError("cần ít nhất một field: name, capacity_l, hoặc toạ độ")
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


# --------------------------------------------------------------------------- #
# Dự báo
# --------------------------------------------------------------------------- #

# Số dẫn xuất (không phải giá trị vendor) nên dùng float, và làm tròn NGAY Ở
# TẦNG SERIALIZE. Nếu để nguyên, JSON sẽ đầy `7103.999999999999` — vô hại về mặt
# toán nhưng làm người đọc mất tin vào cả trang. Làm tròn ở đây thay vì ở từng
# router để không có endpoint nào lỡ quên.
Num = Annotated[float, PlainSerializer(lambda v: round(v, 4), return_type=float)]
OptNum = Annotated[
    float | None,
    PlainSerializer(
        lambda v: None if v is None else round(v, 4), return_type=float | None
    ),
]


class ConsumptionOut(BaseModel):
    """Mức dùng/ngày SUY TỪ LỊCH SỬ, thay cho con số gõ tay.

    Phát kèm ``confidence``/``coverage``/``samples`` là cố ý: một con số dự báo
    không có độ tin cậy đi kèm sẽ được đọc như số đo, và ở đây nó thường không
    phải — thiết bị mất upload liên tục.
    """

    model_config = ConfigDict(from_attributes=True)

    daily_use_l: OptNum = None
    daily_use_sd_l: OptNum = None
    samples: int = 0
    window_days: Num = 0.0
    active_days: Num = 0.0
    coverage: Num = 0.0
    drawdown_l: Num = 0.0
    refills: int = 0
    refill_l: Num = 0.0
    full_days: int = 0
    confidence: Literal["high", "medium", "low", "none"] = "none"


class IdleTrendOut(BaseModel):
    """Boil-off và tốc độ tăng áp, đo trong các cửa sổ bồn nghỉ.

    ``method`` phân biệt "đo được" với "lấy theo tham chiếu 0.05 %/ngày". Trộn
    hai thứ này lại là cách nhanh nhất để một hằng số bị hiểu thành một phép đo.
    """

    model_config = ConfigDict(from_attributes=True)

    boil_off_l_per_day: OptNum = None
    boil_off_percent_per_day: OptNum = None
    pressure_rise_mpa_per_day: OptNum = None
    idle_windows: int = 0
    idle_hours: Num = 0.0
    method: Literal["measured", "reference", "insufficient"] = "insufficient"


class RunoutOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    daily_loss_l: OptNum = None
    days_to_reserve: OptNum = None
    days_to_empty: OptNum = None
    reserve_at: datetime | None = None
    empty_at: datetime | None = None


class HoldTimeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    days: OptNum = None
    current_mpa: OptNum = None
    relief_mpa: Num = 0.8
    rise_mpa_per_day: OptNum = None
    headroom_mpa: OptNum = None
    method: Literal["measured", "reference", "insufficient"] = "insufficient"


class SuggestionOut(BaseModel):
    """Đề xuất đặt hàng kèm ``reasons`` — mỗi con số truy được về đầu vào."""

    model_config = ConfigDict(from_attributes=True)

    order_l: OptNum = None
    order_at: datetime | None = None
    deliver_at: datetime | None = None
    target_l: Num = 0.0
    reorder_point_l: Num = 0.0
    safety_stock_l: Num = 0.0
    lead_time_days: Num = 1.0
    service_level: int = 95
    urgency: Literal["now", "soon", "ok", "unknown"] = "unknown"
    reasons: list[str] = Field(default_factory=list)


class RefillOut(BaseModel):
    """Một lần nạp phát hiện từ telemetry — nhật ký nạp không cần nhập tay."""

    model_config = ConfigDict(from_attributes=True)

    at: datetime
    before_l: Num
    after_l: Num
    amount_l: Num


class ForecastOut(BaseModel):
    psn: str
    name: str | None = None
    status: Literal["online", "offline"] = "offline"
    sampled_at: datetime | None = None
    volume_l: OptNum = None
    capacity_l: OptNum = None
    fill_percent: OptNum = None
    reserve_l: Num = 0.0
    consumption: ConsumptionOut
    idle: IdleTrendOut
    runout: RunoutOut
    hold: HoldTimeOut
    suggestion: SuggestionOut
    refills: list[RefillOut] = Field(default_factory=list)
    # Tuổi của lần đọc mà mọi con số 'hiện tại' dựa vào, và cờ nói rằng nó đã
    # quá cũ để chiếu về tương lai. Phát ra ngoài để UI nói thẳng điều đó thay
    # vì trình bày một dự báo từ số liệu chết như thể nó còn đúng.
    reading_age_days: OptNum = None
    stale: bool = False
    # Cảnh báo suy từ dự báo (RUNOUT / HOLD_TIME / BOIL_OFF_HIGH). Cùng shape với
    # AlertOut để dashboard gộp được vào một danh sách mà không cần map.
    alerts: list[AlertOut] = Field(default_factory=list)
    generated_at: datetime


class DeliveryStopOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    psn: str
    name: str | None = None
    order_l: Num
    days_to_reserve: OptNum = None
    urgency: Literal["now", "soon", "ok", "unknown"] = "unknown"


class DeliveryTripOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    stops: list[DeliveryStopOut]
    total_l: Num
    truck_capacity_l: Num


class DeliveryPlanOut(BaseModel):
    truck_capacity_l: Num
    horizon_days: Num
    trips: list[DeliveryTripOut] = Field(default_factory=list)
    total_l: Num = 0.0
    stops: int = 0
    generated_at: datetime


class NotificationOut(BaseModel):
    """Nhật ký thông báo đã gửi. Dùng cho kiểm toán và để chứng minh đã báo."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    psn: str
    code: str
    severity: str
    channel: str
    status: str
    message: str | None = None
    detail: str | None = None
    sent_at: datetime


# --------------------------------------------------------------------------- #
# Cài đặt do người vận hành đặt trong app
# --------------------------------------------------------------------------- #


class SettingsIn(BaseModel):
    """Body của PATCH /api/settings. Mọi field optional — chỉ gửi ô vừa sửa.

    Hai quy ước phải phân biệt được, và đó là lý do dùng ``exclude_unset``:

    * **không gửi field**  -> giữ nguyên giá trị đang có;
    * **gửi field = null** -> XOÁ override, trả field về giá trị .env.

    ``extra="forbid"``: gõ sai tên field bị 422 chứ không im lặng bỏ qua. Một
    trang cấu hình mà nhận rồi bỏ qua là cách tệ nhất — người dùng tưởng đã lưu.
    """

    model_config = ConfigDict(extra="forbid")

    # --- thông báo ---
    notify_enabled: bool | None = None
    alert_email_to: str | None = Field(None, max_length=1000)
    alert_resend_hours: int | None = Field(None, ge=1, le=168)
    smtp_host: str | None = Field(None, max_length=255)
    smtp_port: int | None = Field(None, ge=1, le=65535)
    smtp_user: str | None = Field(None, max_length=255)
    #: Ghi được, KHÔNG BAO GIỜ đọc ra. SettingsOut chỉ phát `smtp_password_set`.
    smtp_password: str | None = Field(None, max_length=255)
    smtp_from: str | None = Field(None, max_length=255)
    smtp_starttls: bool | None = None

    # --- dự báo / kế hoạch ---
    forecast_window_days: int | None = Field(None, ge=1, le=365)
    forecast_reserve_percent: float | None = Field(None, ge=0, le=100)
    forecast_lead_time_days: float | None = Field(None, ge=0, le=30)
    forecast_service_level: int | None = None
    forecast_max_reading_age_hours: float | None = Field(None, gt=0, le=8760)
    lng_relief_pressure_mpa: float | None = Field(None, gt=0, le=10)
    lng_max_fill_percent: float | None = Field(None, gt=0, le=100)
    truck_capacity_l: float | None = Field(None, gt=0)

    # --- ngưỡng cảnh báo thiết bị ---
    online_stale_minutes: int | None = Field(None, ge=1, le=100_000)
    alert_low_volume_percent: float | None = Field(None, ge=0, le=100)
    alert_low_battery_v: float | None = Field(None, ge=0, le=100)
    alert_low_signal_percent: float | None = Field(None, ge=0, le=100)

    @field_validator("alert_email_to")
    @classmethod
    def _emails(cls, v: str | None) -> str | None:
        """Chặn địa chỉ rác NGAY LÚC LƯU.

        Không validate ở đây thì phản hồi duy nhất người dùng nhận được là một lỗi
        SMTP khó hiểu lúc bấm Gửi thử — hoặc tệ hơn, cảnh báo im lặng không đến ai
        suốt nhiều tuần.
        """
        if v is None:
            return None
        parts = [a.strip() for a in v.split(",") if a.strip()]
        for a in parts:
            if a.count("@") != 1 or " " in a or a.startswith("@") or a.endswith("@"):
                raise ValueError(f"địa chỉ email không hợp lệ: {a}")
            if "." not in a.split("@", 1)[1]:
                raise ValueError(f"tên miền email không hợp lệ: {a}")
        return ", ".join(parts)

    @field_validator("forecast_service_level")
    @classmethod
    def _service_level(cls, v: int | None) -> int | None:
        # Bảng z-score chỉ có 5 mức. Nhận một mức lạ rồi âm thầm dùng z=1.645 sẽ
        # làm dự trữ an toàn sai mà không con số nào trên giao diện tố giác được.
        allowed = (50, 80, 90, 95, 99)
        if v is not None and v not in allowed:
            raise ValueError(f"mức phục vụ phải thuộc {allowed}")
        return v


class SettingsOut(BaseModel):
    """Giá trị ĐANG CÓ HIỆU LỰC + mỗi field đến từ đâu.

    ``sources`` (app | env) trả lời câu hỏi đầu tiên của bất kỳ ai mở một trang
    cấu hình có hai nguồn: "vì sao giá trị này lại thế". Không có nó, người dùng
    sửa .env rồi không hiểu vì sao app vẫn giữ số cũ.

    ``smtp_password`` KHÔNG có ở đây, kể cả dạng che dấu — chỉ có cờ đã-lưu-chưa.
    """

    values: dict[str, object]
    sources: dict[str, str]
    smtp_password_set: bool
    smtp_ready: bool
    #: Vì sao chưa gửi được, viết cho người đọc chứ không phải mã lỗi.
    smtp_blocked_reason: str | None = None
    updated_at: datetime | None = None
    updated_by: str | None = None


class ActionOut(BaseModel):
    ok: bool
    message: str


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    username: str


# --------------------------------------------------------------------------- #
# Phân tích
# --------------------------------------------------------------------------- #


class QualityOut(BaseModel):
    """Chuỗi này đáng tin bao nhiêu. Đi KÈM mọi con số phân tích, không tách rời.

    Có nó thì "còn 5.8 ngày" đọc được thành "5.8 ngày, suy từ chuỗi phủ 23%" và người
    vận hành tự biết nên tin bao nhiêu. Không có nó thì hai con số trông giống nhau
    trong khi một cái dựng từ dữ liệu đầy đủ và cái kia từ một phần tư.
    """

    samples: int
    window_days: float
    cadence_minutes: float | None = None
    cadence_jitter_minutes: float | None = None
    expected_samples: int
    coverage: float
    gaps: int
    longest_gap_hours: float | None = None
    flatline_runs: int
    longest_flatline_hours: float | None = None
    grade: Literal["cao", "trung bình", "thấp", "không dùng được"]
    reasons: list[str] = Field(default_factory=list)


class BatteryOut(BaseModel):
    current_v: float | None = None
    volts_per_day: float | None = None
    days_to_warn: float | None = None
    days_to_dead: float | None = None
    warn_v: float
    dead_v: float
    confidence: str


class SignalOut(BaseModel):
    current_percent: float | None = None
    percent_per_day: float | None = None
    below_floor_ratio: float
    floor_percent: float


class DeviceHealthOut(BaseModel):
    """Thiết bị còn báo được bao lâu, và VÌ SAO nó sẽ chết.

    ``likely_cause`` tồn tại vì một điểm số rủi ro không nói được nên mang theo pin
    hay mang theo ăng-ten khi ra hiện trường.
    """

    psn: str
    name: str | None = None
    samples: int
    battery: BatteryOut
    signal: SignalOut
    delivery_ratio: float
    delivery_trend_per_day: float | None = None
    silent_days: float | None = None
    risk: Literal["cao", "trung bình", "thấp", "chưa đủ dữ liệu"]
    likely_cause: str | None = None
    days_to_failure: float | None = None
    reasons: list[str] = Field(default_factory=list)


class AnomalyOut(BaseModel):
    at: datetime
    kind: Literal["sụt bất thường", "tăng bất thường", "cảm biến kẹt"]
    value_l: float | None = None
    expected_l: float | None = None
    deviation_l: float | None = None
    z: float | None = None
    note: str


class AnalyticsOut(BaseModel):
    psn: str
    name: str | None = None
    capacity_l: float | None = None
    window_days: float
    quality: QualityOut
    health: DeviceHealthOut
    anomalies: list[AnomalyOut] = Field(default_factory=list)
    #: Thời điểm chuỗi đổi chế độ tiêu thụ — để dashboard vẽ mốc trên đồ thị.
    regime_changes: list[datetime] = Field(default_factory=list)
    generated_at: datetime


# --------------------------------------------------------------------------- #
# Số đo tay cho trang Kế hoạch
# --------------------------------------------------------------------------- #


class PlanReadingIn(BaseModel):
    """Thể tích ĐO TAY của một ngày, đơn vị lít.

    Lít chứ không m³, dù trang Kế hoạch hiển thị m³: mọi field thể tích khác của
    API này đều là lít (``volume_l``, ``capacity_l``), và một API trộn hai đơn vị
    là đúng loại lỗi sai-1000-lần mà adapter đã phải dựng hàng rào lo/hi để chặn.
    UI quy đổi ở biên, đúng một chỗ.

    Không có giới hạn trên ở đây vì trần thật là dung tích của chính bồn đó, mà
    schema thì không biết bồn nào — router kiểm tiếp bằng ``capacity_l``.
    """

    volume_l: Decimal = Field(..., ge=0)


class PlanReadingOut(BaseModel):
    """Một số đo tay đã lưu."""

    model_config = ConfigDict(from_attributes=True)

    psn: str
    reading_date: date
    volume_l: Decimal
    #: Ai nhập. Số tay ghi đè ước tính nên phải truy được người chịu trách nhiệm.
    entered_by: str | None = None
    updated_at: datetime
