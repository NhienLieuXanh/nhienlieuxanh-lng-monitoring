"""Model SQLAlchemy 2.0.

Ba sai lệch có chủ ý so với spec gốc, đều được giải thích tại chỗ:

1. ``telemetry`` PK là ``(psn, sampled_at)``, không phải ``id bigserial``.
2. FK từ ``telemetry`` sang ``terminals`` là **composite** ``(terminal_id, psn)``.
3. Thêm ``terminals.capacity_l`` và bảng ``ingest_runs``.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Numeric thay vì Float: giá trị vendor là số thập phân ngắn (0.071 MPa, 3.6 V) và
# ta so sánh chúng với ngưỡng cảnh báo. Float làm 3.6 thành 3.5999999999999996 nên
# một ngưỡng "battery_v < 3.6" hành xử khác nhau tuỳ hướng gió. Numeric giữ đúng
# giá trị vendor gửi, và Decimal ở tầng Python khớp 1:1 với nó.
_MEASURE = Numeric(18, 6)


class Terminal(Base):
    """Một thiết bị đo + bồn nó gắn vào.

    Giai đoạn 1 gộp *thiết bị* (psn, modem, SIM, firmware) và *bồn* (capacity_l,
    tên, site) vào một bảng vì chúng đang 1:1. Khi nào thay thiết bị trên một bồn
    — việc bảo trì thường xuyên — thì cần tách bảng ``tanks`` với
    ``terminals.tank_id``, nếu không lịch sử của bồn vỡ thành hai PSN. Ghi lại đây
    để lúc đó không ai phải khảo cổ lý do.
    """

    __tablename__ = "terminals"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        # gen_random_uuid() là core từ Postgres 13 — KHÔNG cần CREATE EXTENSION
        # pgcrypto. Sinh phía server nên INSERT thô bằng psql cũng có id.
        server_default=text("gen_random_uuid()"),
    )
    psn: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)

    # Tên do người vận hành đặt. sync_terminals() KHÔNG BAO GIỜ ghi đè field này
    # bằng giá trị vendor — xem repositories/terminals.py.
    name: Mapped[str | None] = mapped_column(String(128))

    modem_number: Mapped[str | None] = mapped_column(String(64))
    sim_iccid: Mapped[str | None] = mapped_column(String(32))
    hardware_version: Mapped[str | None] = mapped_column(String(64))
    software_version: Mapped[str | None] = mapped_column(String(64))
    device_model: Mapped[str | None] = mapped_column(String(64))
    device_type_name: Mapped[str | None] = mapped_column(String(64))

    medium_name: Mapped[str | None] = mapped_column(String(64))
    tank_type_name: Mapped[str | None] = mapped_column(String(64))

    # THÊM so với spec. Vendor gửi cylinderVolume kèm MỌI lần đọc, nhưng đó là cấu
    # hình tài sản vật lý, không phải telemetry — không có lý gì lưu lại 48 lần
    # mỗi ngày. Ở đây nó cũng sửa tay được khi vendor sai.
    capacity_l: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))

    # Toạ độ bồn. Hai nguồn, và thứ tự ưu tiên là điều quan trọng nhất ở đây:
    #
    # 1. **GPS của module**, tự lấy qua `psn/search` mỗi vòng ingest. Nhưng nó là
    #    dữ liệu THỈNH THOẢNG CÓ: cùng một thiết bị có ngày trả toạ độ thật, có
    #    ngày trả `0.000000 / 0.000000` — 0,0 là Null Island giữa vịnh Guinea, tức
    #    tín hiệu MẤT ĐỊNH VỊ, không phải một vị trí. Đã xác minh bằng cách gọi
    #    thẳng vendor: PSN 2604200016 ngày 2026-07-23 trả 10.971047/106.750161,
    #    còn ngày 2026-06-02 trả 0,0 cho cả 17 dòng.
    # 2. **Người vận hành ghim tay** qua update_operator(), khi cần vị trí chính
    #    xác hơn (đúng nhà kho) hoặc khi module không định vị được.
    #
    # Ingest chỉ COALESCE vào chỗ NULL nên nguồn 2 luôn thắng nguồn 1 — xem
    # repositories/terminals.py:upsert(). Không có luật đó thì một ngày mất định vị
    # sẽ xoá mất toạ độ đang đúng.
    #
    # Bồn LNG là tài sản cố định đặt tại kho khách hàng: toạ độ thuộc *cấu hình tài
    # sản*, cùng loại với capacity_l, không phải telemetry.
    #
    # Numeric(9,6): 3 chữ số phần nguyên đủ cho kinh độ ±180, 6 chữ số thập phân
    # ~0,11 m — dư sức cho một bồn đứng yên.
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))

    # `status` là CACHE, không phải sự thật. API suy lại từ last_seen_at lúc đọc
    # (xem domain/status.py) vì cột lưu có lỗi staleness không tránh được: thiết bị
    # ngừng báo thì không ingest nào chạm row đó, nên nó đứng mãi ở giá trị cũ.
    # Giữ cột để query SQL ad-hoc và alert phía DB vẫn dùng được.
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'offline'")
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        # varchar + CHECK thay vì PG ENUM: thêm giá trị vào enum bên trong một
        # migration transactional là nỗi đau đã biết (ALTER TYPE ... ADD VALUE bị
        # hạn chế), còn CHECK chỉ là một dòng drop + create.
        CheckConstraint("status IN ('online','offline')", name="status_valid"),
        CheckConstraint(
            "capacity_l IS NULL OR capacity_l > 0", name="capacity_positive"
        ),
        CheckConstraint(
            "latitude IS NULL OR (latitude >= -90 AND latitude <= 90)",
            name="latitude_range",
        ),
        CheckConstraint(
            "longitude IS NULL OR (longitude >= -180 AND longitude <= 180)",
            name="longitude_range",
        ),
        # Một nửa toạ độ là trạng thái vô nghĩa: không vẽ được điểm nào, mà cũng
        # không phải "chưa khai". Bắt cặp ở tầng DB nên không đường ghi nào tạo ra
        # được nó — cùng tinh thần với composite FK của telemetry: để Postgres
        # chứng minh, đừng dựa vào kỷ luật ở tầng app.
        CheckConstraint("(latitude IS NULL) = (longitude IS NULL)", name="latlon_paired"),
        # 0,0 là Null Island giữa vịnh Guinea — chính là giá trị vendor gửi khi
        # module KHÔNG có định vị. Chặn ở đây để một lần gõ nhầm, hay một lần import
        # GPS vendor sau này, không đặt bồn LNG ra giữa Đại Tây Dương.
        CheckConstraint(
            "latitude IS NULL OR latitude <> 0 OR longitude <> 0",
            name="latlon_not_null_island",
        ),
        # Target cho composite FK của telemetry. Redundant với PK về mặt logic
        # nhưng Postgres đòi một UNIQUE khớp đúng cặp cột được tham chiếu.
        UniqueConstraint("id", "psn"),
    )


class AppSetting(Base):
    """Cấu hình do NGƯỜI VẬN HÀNH đặt trong app, ghi đè giá trị mặc định từ .env.

    Vì sao cần bảng này: trước đó danh sách email nhận cảnh báo, ngưỡng gửi lại,
    áp van an toàn... đều là biến môi trường. Đổi một địa chỉ email phải sửa env
    trên Vercel rồi redeploy — với thứ thay đổi thường xuyên đó là thiết kế sai.
    Cấu hình vận hành phải bấm được trong app; chỉ những thứ KHÔNG bao giờ đổi
    mới thuộc về env.

    **Một dòng duy nhất** (``id`` CHECK = 1). Không phải bảng nhiều dòng theo user
    hay theo site: giai đoạn này chỉ có một cấu hình cho cả hệ thống, và một CHECK
    một dòng thì không bao giờ có chuyện "hai bản cấu hình, không biết cái nào
    đang có hiệu lực".

    ``data`` là JSONB thay vì cột rời từng setting: tập setting sẽ còn mọc thêm, và
    mỗi lần thêm một ô trong trang Cài đặt mà phải viết một migration là ma sát vô
    ích. Đánh đổi: không có constraint ở tầng DB — bù lại bằng validate Pydantic ở
    tầng API, và API là ĐƯỜNG GHI DUY NHẤT vào bảng này.
    """

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    #: Ai sửa lần cuối. Cấu hình này quyết định cảnh báo đi đâu, nên phải truy
    #: được người đổi — cùng lý do như bảng notifications.
    updated_by: Mapped[str | None] = mapped_column(String(128))

    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)


class Notification(Base):
    """Nhật ký thông báo đã gửi. Hai vai trò, cả hai đều bắt buộc.

    1. **Chống spam.** Ingest chạy mỗi 10 phút và cảnh báo được suy lại mỗi vòng,
       nên không có nơi lưu "đã gửi lúc nào" thì một bồn cạn sẽ tạo ra 144 email
       mỗi ngày. Người nhận sẽ lọc hết vào thùng rác và cảnh báo mất tác dụng —
       tệ hơn là không có cảnh báo, vì lúc đó ai cũng tưởng mình đang được báo.
       Trên serverless (Vercel) trạng thái không thể nằm trong bộ nhớ process:
       mỗi lần gọi là một process mới. Nên nó phải ở DB.
    2. **Kiểm toán.** Trả lời được "đã báo cho ai, lúc nào, nội dung gì" — thứ
       nhà đầu tư và bộ phận vận hành sẽ hỏi ngay sau một sự cố.

    CỐ Ý KHÔNG có FK sang ``terminals``: dòng log phải ghi được ngay cả khi PSN
    chưa được provision, và một lần insert log tuyệt đối không được thất bại vì
    ràng buộc tham chiếu đúng lúc đang có sự cố.
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=False), primary_key=True
    )
    psn: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Mã cảnh báo: RUNOUT / HOLD_TIME / BOIL_OFF_HIGH / LOW_VOLUME / OFFLINE...
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    channel: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'email'")
    )
    #: Chỉ hai giá trị: sent = đã bàn giao cho SMTP, failed = SMTP từ chối hoặc
    #: lỗi mạng. CỐ Ý không có 'skipped': cảnh báo bị cửa chặn gửi lại xảy ra mỗi
    #: vòng ingest (10 phút), ghi lại sẽ thành 144 dòng/ngày/cảnh báo mà không
    #: thêm thông tin nào — số đó chỉ cần đếm trong stats của vòng ingest.
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("status IN ('sent','failed')", name="notify_status_valid"),
        CheckConstraint(
            "severity IN ('critical','warning','info')", name="notify_severity_valid"
        ),
        # Query nóng duy nhất: "lần gửi gần nhất cho (psn, code) là khi nào" —
        # một backward index-scan trên đúng index này.
        Index("ix_notifications_psn_code_sent_at", "psn", "code", "sent_at"),
    )


class Telemetry(Base):
    """Một lần đọc đã chuẩn hoá. Bất biến — không bao giờ UPDATE trong luồng thường."""

    __tablename__ = "telemetry"

    # `id` giữ lại làm tie-breaker đơn điệu cho keyset pagination sau này, nhưng
    # KHÔNG unique và KHÔNG phải PK. Identity() là idiom hiện đại thay bigserial.
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), nullable=False)

    terminal_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    psn: Mapped[str] = mapped_column(String(32), nullable=False)

    # LUÔN lưu UTC. Vendor gửi naive string render ở UTC+8; adapter gắn timezone
    # rồi convert, và NormalizedTelemetry từ chối datetime naive để việc đó không
    # thể bị bỏ sót.
    sampled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # String timestamp gốc của vendor, y nguyên. ~20 byte/dòng, và nó biến việc sửa
    # timezone về sau thành một câu UPDATE re-derive thuần SQL thay vì phải fetch
    # lại vendor — quan trọng vì retention lịch sử của vendor chưa rõ và thiết bị
    # đã offline hàng tháng.
    vendor_ts_raw: Mapped[str | None] = mapped_column(String(64))

    level_mmwc: Mapped[Decimal | None] = mapped_column(_MEASURE)
    diff_pressure_kpa: Mapped[Decimal | None] = mapped_column(_MEASURE)
    pressure_mpa: Mapped[Decimal | None] = mapped_column(_MEASURE)
    volume_l: Mapped[Decimal | None] = mapped_column(_MEASURE)

    # Thang 0-100. Xác minh trên dữ liệu thật: vendor gửi volumePercentage=0.59 với
    # currentVolume=61 và cylinderVolume=10425, và 61/10425*100 = 0.5851. Nghĩa là
    # 0.59% ĐẦY. Không CHECK constraint nào bắt được lỗi hiểu sai thang này vì 0.59
    # hợp lệ ở cả hai — nên API phát kèm fill_percent tính độc lập làm đối chứng.
    volume_percent: Mapped[Decimal | None] = mapped_column(_MEASURE)
    volume_percent_source: Mapped[str | None] = mapped_column(String(16))

    temperature_c: Mapped[Decimal | None] = mapped_column(_MEASURE)
    vacuum_pa: Mapped[Decimal | None] = mapped_column(_MEASURE)
    signal_percent: Mapped[Decimal | None] = mapped_column(_MEASURE)
    battery_v: Mapped[Decimal | None] = mapped_column(_MEASURE)

    medium_name: Mapped[str | None] = mapped_column(String(64))
    tank_type_name: Mapped[str | None] = mapped_column(String(64))

    # Payload vendor nguyên bản. Đây là thứ duy nhất cho phép sửa một field map sai
    # mà không phải gọi lại vendor. CŨNG là vector rò tên vendor nguy hiểm nhất:
    # loại khỏi mọi response model, enforce bằng tests/test_isolation.py.
    raw_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'xingke'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # PK là (psn, sampled_at), không phải id. Thoả hợp đồng uniqueness của spec
        # bằng MỘT index thay vì hai, và TimescaleDB-ready ngay: create_hypertable
        # đòi cột phân vùng phải có trong PK và mọi unique index. Với PK theo spec
        # (id) thì chuyển sang Timescale là DROP PK + rebuild bảng dưới lock.
        PrimaryKeyConstraint("psn", "sampled_at", name="pk_telemetry"),
        # FK COMPOSITE. Spec denormalize psn cạnh terminal_id, nên về nguyên tắc
        # hai cột có thể lệch nhau. Thay vì trigger hay kỷ luật tầng app, để
        # Postgres chứng minh: giờ không thể insert một dòng telemetry mà psn không
        # thuộc terminal_id của nó. ON UPDATE CASCADE để sửa một PSN gõ sai ở bảng
        # cha lan xuống. ON DELETE RESTRICT vì xoá terminal mà còn telemetry gần
        # như luôn là nhầm.
        ForeignKeyConstraint(
            ["terminal_id", "psn"],
            ["terminals.id", "terminals.psn"],
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
        # Postgres KHÔNG tự index cột referencing của FK. Không có index này thì
        # mỗi UPDATE/DELETE trên terminals phải seq scan cả telemetry.
        Index("ix_telemetry_terminal_id_sampled_at", "terminal_id", "sampled_at"),
        # Không thêm index (psn, sampled_at DESC): Postgres scan btree ngược gần
        # như cùng chi phí, nên PK ASC đã phục vụ cả latest-per-psn lẫn range scan.
        # Index DESC chỉ có ích khi trộn hướng sort trên nhiều cột trong cùng một
        # ORDER BY, việc không endpoint nào làm — thêm nó chỉ tăng gấp đôi chi phí ghi.
    )


class IngestRun(Base):
    """Audit log mỗi lần chạy ingest.

    THÊM so với spec, và bắt buộc phải có: ``/api/health`` cần biết "lần ingest
    thành công gần nhất cách đây bao lâu". Suy ra từ ``MAX(telemetry.created_at)``
    là SAI — nó không phân biệt được "ingest chạy tốt, thiết bị chỉ đang offline"
    với "ingest hỏng". Cả hai thiết bị thật hiện offline nhiều tháng, nên health
    suy từ telemetry sẽ báo đỏ vĩnh viễn và không ai còn tin nó nữa.
    """

    __tablename__ = "ingest_runs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    fetched: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    inserted: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    duplicates: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    terminals_created: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    error_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    error_summary: Mapped[str | None] = mapped_column(Text)

    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # MappingReport được persist ở đây để khoảng trống mapping nổi lên qua endpoint
    # admin, thay vì đòi ai đó đi đọc log file.
    mapping_report: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('success','partial','failed')", name="status_valid"
        ),
        CheckConstraint("trigger IN ('scheduler','cli','api')", name="trigger_valid"),
        # Truy vấn nóng duy nhất: "lần thành công gần nhất" cho health check.
        Index("ix_ingest_runs_status_finished_at", "status", "finished_at"),
    )


class PlanReading(Base):
    """Thể tích ĐO TAY của một bồn vào một ngày cụ thể, dùng cho trang Kế hoạch.

    Vì sao cần bảng này. Kế hoạch nạp là một chuỗi số học: thể tích đầu ngày kế =
    thể tích hôm nay trừ mức tiêu thụ/ngày. Mức tiêu thụ là một con số *bình quân*
    nên chuỗi đó trượt khỏi thực tế ngay ngày thứ hai — hôm nay xưởng chạy ít thì
    còn 48 m³ chứ không phải 46,60 m³ như công thức. Không có đường nhập số thực
    tế thì người vận hành phải sửa "thể tích ban đầu" rồi dịch "ngày bắt đầu", tức
    xoá mất lịch sử và xoá luôn khả năng so ước tính với thực tế.

    Đây là dữ liệu NGƯỜI nhập, KHÔNG phải telemetry, và cố ý đứng ngoài bảng
    ``telemetry``:

    * ``telemetry`` có đúng một đường ghi — ingestion từ vendor. Cho một form web
      ghi vào đó thì mọi con số "đo được" (mức tiêu thụ, nhận diện lần nạp, cảnh
      báo) trở thành pha giữa số máy và số người mà không ai phân biệt được nữa.
    * Phạm vi đã chốt với người dùng: số này chỉ dùng cho trang Kế hoạch. Dashboard
      và dự báo vẫn chỉ đọc số từ thiết bị.

    Khoá chính ``(psn, reading_date)`` là khoá tự nhiên: một bồn một ngày có đúng
    một số đo. Cùng lý do như PK của ``telemetry`` — thoả ràng buộc duy nhất bằng
    một index thay vì hai, và làm cho "nhập lại số của hôm nay" là một UPSERT chứ
    không phải một dòng trùng thứ hai.

    ``reading_date`` là DATE, không phải timestamptz. Kế hoạch làm việc theo *ngày
    lịch Việt Nam* ("thể tích đầu ngày"), nên hạ granularity xuống ngày là cách
    duy nhất không phải chọn múi giờ — và dự án này đã có đủ ba múi giờ để nhầm.
    """

    __tablename__ = "plan_readings"

    psn: Mapped[str] = mapped_column(String(32), nullable=False)
    reading_date: Mapped[date] = mapped_column(Date, nullable=False)

    #: Lít, KHÔNG phải m³. Toàn bộ API và DB nói bằng lít (``volume_l``,
    #: ``capacity_l``); trang Kế hoạch nói bằng m³ và quy đổi ở đúng biên UI. Trộn
    #: hai đơn vị trong cùng một API là đúng loại lỗi sai-1000-lần mà dự án này đã
    #: dựng cả hàng rào lo/hi trong adapter để chặn.
    volume_l: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False)

    #: Ai nhập. Số tay ghi đè số ước tính thì phải truy được người chịu trách
    #: nhiệm — cùng lý do như ``app_settings.updated_by``.
    entered_by: Mapped[str | None] = mapped_column(String(128))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        PrimaryKeyConstraint("psn", "reading_date"),
        # 0 là hợp lệ: bồn cạn là một số đo thật, và chính là lúc người ta cần
        # nhập tay nhất. Chỉ số âm là vô nghĩa.
        CheckConstraint("volume_l >= 0", name="volume_non_negative"),
        # ON UPDATE CASCADE để sửa được một PSN gõ sai; ON DELETE RESTRICT theo
        # đúng luật của telemetry — không đường nào trong app này xoá terminal, và
        # nếu có thì mất im lặng dữ liệu người nhập tay là kết cục tệ nhất.
        ForeignKeyConstraint(
            ["psn"],
            ["terminals.psn"],
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
    )
