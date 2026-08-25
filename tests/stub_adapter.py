"""FakeAdapter — implement TelemetryPort mà không gọi mạng.

Đây là lý do tồn tại của Protocol trong domain/contracts.py: ingestion, scheduler,
API và dashboard build + test được ĐẦY ĐỦ mà không cần credential vendor, không cần
mạng, và không đập vào một API console quản trị đang bị audit. Adapter thật drop-in
sau đó với zero thay đổi ở các tầng đó.

Hai chế độ:
  * ``from_fixture``  — phát lại response thật đã capture. Dùng cho test mapping.
  * ``synthetic``     — sinh chuỗi thời gian đều đặn. Dùng cho seed-demo và test API.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from app.domain.contracts import (
    FetchResult,
    MappingReport,
    NormalizedTelemetry,
    NormalizedTerminal,
    PercentSource,
)

log = logging.getLogger(__name__)

UTC = ZoneInfo("UTC")
SHANGHAI = ZoneInfo("Asia/Shanghai")
SOURCE = "fake"

# Hai PSN thật, và dung tích thật của chúng.
DEMO_PSNS: tuple[str, ...] = ("2604200016", "2605090007")

# Toạ độ duy nhất từng thấy trong dữ liệu vendor thật (DISCOVERY.md, và xác minh
# lại bằng cách gọi thẳng vendor cho ngày 2026-07-23).
DEMO_GPS: tuple[Decimal, Decimal] = (Decimal("10.971047"), Decimal("106.750161"))
DEMO_CAPACITY_L = Decimal("10425")

# Ngày cuối có dữ liệu thật của mỗi thiết bị. Mặc định seed-demo dừng ở đây để tái
# hiện production: cả hai thiết bị offline hàng tháng, nên đường code "offline" là
# đường mặc định chứ không phải ngoại lệ.
DEMO_LAST_SEEN: dict[str, datetime] = {
    "2604200016": datetime(2026, 7, 23, 16, 3, 29, tzinfo=SHANGHAI),
    "2605090007": datetime(2026, 6, 2, 22, 17, 3, tzinfo=SHANGHAI),
}

# Cadence đo được từ dữ liệu thật (12 dòng của 2605090007): đúng 30 phút.
SAMPLE_INTERVAL = timedelta(minutes=30)

_SYNTH_FIELDS = (
    "volume_l",
    "volume_percent",
    "pressure_mpa",
    "battery_v",
    "signal_percent",
    "level_mmwc",
    "diff_pressure_kpa",
    "vacuum_pa",
)


class FakeAuthError(RuntimeError):
    """Giả lập session hết hạn mà không import module vendor.

    Test scheduler cần một exception "auth chết" để kiểm việc pause job. Dùng lại
    exception của vendor ở đây sẽ khiến test phụ thuộc vào adapters/xingke, phá
    chính cái biên mà FakeAdapter dùng để chứng minh là kín.
    """


class FakeTransientError(RuntimeError):
    """Giả lập lỗi mạng tạm thời."""


class FakeAdapter:
    """TelemetryPort tổng hợp, có công tắc điều khiển hành vi lỗi."""

    source = SOURCE

    def __init__(
        self,
        *,
        psns: tuple[str, ...] = DEMO_PSNS,
        capacity_l: Decimal = DEMO_CAPACITY_L,
        fresh: bool = False,
        days: int = 3,
        fixture: Path | None = None,
        raise_auth: bool = False,
        raise_transient: bool = False,
        return_empty: bool = False,
        now: datetime | None = None,
    ) -> None:
        self._psns = psns
        self._capacity = capacity_l
        self._fresh = fresh
        self._days = days
        self._fixture = fixture
        self._raise_auth = raise_auth
        self._raise_transient = raise_transient
        self._return_empty = return_empty
        self._now = now or datetime.now(tz=UTC)

    def close(self) -> None:
        return None

    @property
    def vendor_tz(self) -> ZoneInfo:
        # Khớp adapter thật: IngestionService hỏi TZ vendor để chọn "hôm nay".
        # Không có property này thì fake ingest theo UTC và lệch một ngày so với
        # dữ liệu tổng hợp (neo Asia/Shanghai).
        return SHANGHAI

    def _anchor(self, psn: str) -> datetime:
        """Thời điểm của lần đọc cuối cùng cho PSN này.

        ``fresh=True`` neo vào hiện tại để ít nhất một terminal tính ra ONLINE —
        không có nó thì nhánh online không bao giờ được chạy trong test hay demo,
        vì dữ liệu thật đều đã cũ hàng tháng.
        """
        if self._fresh:
            return self._now
        return DEMO_LAST_SEEN.get(psn, self._now).astimezone(UTC)

    def fetch_telemetry(self, psn: str, day: date) -> FetchResult:
        if self._raise_auth:
            raise FakeAuthError("fake: auth bị từ chối (công tắc test)")
        if self._raise_transient:
            raise FakeTransientError("fake: lỗi mạng tạm thời (công tắc test)")

        result = FetchResult()
        if self._return_empty:
            return result

        if self._fixture is not None:
            return self._from_fixture(psn, day)

        anchor = self._anchor(psn)
        readings = [
            r
            for r in self._synthesize(psn, anchor)
            # Adapter thật trả về đúng một ngày lịch giờ vendor; fake phải tôn trọng
            # hợp đồng đó, nếu không test ingestion sẽ pass với một adapter mà
            # adapter thật không giống.
            if r.sampled_at.astimezone(SHANGHAI).date() == day
        ]
        result.readings = readings
        result.total = len(readings)
        result.pages_fetched = 1 if readings else 0
        result.report.n_rows = len(readings)
        for r in readings:
            for f in _SYNTH_FIELDS:
                if getattr(r, f) is not None:
                    result.report.present[f] = result.report.present.get(f, 0) + 1
        return result

    def _synthesize(self, psn: str, anchor: datetime) -> list[NormalizedTelemetry]:
        """Chuỗi giảm dần đều, cách nhau 30 phút, kết thúc tại ``anchor``."""
        n = self._days * 48
        # Bồn thật gần cạn (61 L và 30 L trên 10425 L). Giữ nguyên bậc độ lớn đó để
        # dashboard và ngưỡng alert được thử trên số liệu giống thực tế, không phải
        # trên một bồn đầy tưởng tượng.
        end_volume = Decimal("61") if psn == DEMO_PSNS[0] else Decimal("30")
        base_pressure = Decimal("0.071") if psn == DEMO_PSNS[0] else Decimal("0.132")
        base_batt = Decimal("3.60") if psn == DEMO_PSNS[0] else Decimal("3.64")
        base_signal = Decimal("20") if psn == DEMO_PSNS[0] else Decimal("15")

        out: list[NormalizedTelemetry] = []
        for i in range(n):
            idx = n - 1 - i  # 0 = mới nhất
            ts = anchor - SAMPLE_INTERVAL * idx
            volume = end_volume + Decimal(idx) * Decimal("0.25")
            pressure = base_pressure + (Decimal(idx % 7) * Decimal("0.001"))
            # volume_percent LUÔN thang 0-100, tính đúng như vendor tính:
            # currentVolume / cylinderVolume * 100.
            pct = (volume / self._capacity) * Decimal(100)
            out.append(
                NormalizedTelemetry(
                    source=SOURCE,
                    psn=psn,
                    sampled_at=ts.astimezone(UTC),
                    vendor_ts_raw=ts.astimezone(SHANGHAI).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    volume_l=volume,
                    volume_percent=pct.quantize(Decimal("0.01")),
                    volume_percent_source=PercentSource.VENDOR,
                    pressure_mpa=pressure,
                    battery_v=base_batt,
                    signal_percent=base_signal,
                    level_mmwc=Decimal("42") + Decimal(idx % 5),
                    diff_pressure_kpa=Decimal("0.41"),
                    vacuum_pa=Decimal("0"),
                    # Vendor gửi null cho temperature trên CẢ HAI thiết bị thật.
                    # Để null ở đây là cố ý: nhánh xử lý nullable phải được TẬP
                    # LUYỆN, không phải được giả định.
                    temperature_c=None,
                    medium_name="LNG",
                    tank_type_name="立式",
                    capacity_l=self._capacity,
                    raw_payload={"_fake": True, "psn": psn},
                )
            )
        # GPS chỉ trên bản đọc MỚI NHẤT, các dòng còn lại None. Đây là hình dạng
        # thật của dữ liệu vendor, không phải cho tiện: PSN 2604200016 ngày
        # 2026-07-23 có toạ độ, còn ngày 2026-06-02 thì cả 17 dòng đều 0,0 (adapter
        # thật quy 0,0 về None). Nhờ vậy fake TẬP LUYỆN đúng nhánh "lấy cặp gần nhất
        # còn dùng được" trong ingest_psn_day, thay vì nhánh "mọi dòng đều có".
        if out:
            out[-1] = out[-1].model_copy(
                update={"latitude": DEMO_GPS[0], "longitude": DEMO_GPS[1]}
            )
        return out

    def _from_fixture(self, psn: str, day: date) -> FetchResult:
        """Phát lại một response vendor thật đã capture, qua đúng normalizer thật.

        Import mapping/normalizer của vendor ở đây là có ý: chế độ này TỒN TẠI để
        test mapping vendor, nên nó được phép biết về vendor. Chế độ synthetic ở
        trên thì không.
        """
        from app.adapters.xingke.envelope import extract_page
        from app.adapters.xingke.normalizer import normalize_reading

        assert self._fixture is not None
        payload = json.loads(Path(self._fixture).read_text(encoding="utf-8"))
        rows, total = extract_page(payload.get("data", payload))
        result = FetchResult(total=total, pages_fetched=1)
        report: MappingReport = result.report
        for row in rows:
            report.n_rows += 1
            reading = normalize_reading(
                row,
                psn=str(row.get("psn") or psn),
                source=SOURCE,
                vendor_tz=SHANGHAI,
                report=report,
            )
            if reading is not None:
                result.readings.append(reading)
        return result

    def fetch_devices(self, psns: list[str]) -> list[NormalizedTerminal]:
        if self._raise_auth:
            raise FakeAuthError("fake: auth bị từ chối (công tắc test)")
        return [
            NormalizedTerminal(
                psn=psn,
                modem_number=f"8600000000000{i:02d}",
                sim_iccid=f"898600000000000000{i:02d}",
                hardware_version="fake-hw-1.0",
                software_version="fake-sw-1.0",
                capacity_l=self._capacity,
                medium_name="LNG",
                tank_type_name="立式",
                raw_payload={"_fake": True},
            )
            for i, psn in enumerate(psns)
        ]

    def demo_days(self) -> list[tuple[str, date]]:
        """Các cặp (psn, ngày) mà seed-demo nên ingest để phủ hết dữ liệu sinh ra."""
        pairs: list[tuple[str, date]] = []
        for psn in self._psns:
            anchor = self._anchor(psn).astimezone(SHANGHAI)
            first = (anchor - SAMPLE_INTERVAL * (self._days * 48 - 1)).date()
            d = first
            while d <= anchor.date():
                pairs.append((psn, d))
                d += timedelta(days=1)
        return pairs


def midnight(d: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, time.min, tzinfo=tz)
