"""YokohamaAdapter — TelemetryPort + VendorAlarmPort. Chỉ GET."""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.adapters.yokohama.client import YokohamaClient
from app.adapters.yokohama.config import YokohamaSettings, get_yokohama_settings
from app.adapters.yokohama.errors import YokohamaSchemaError
from app.adapters.yokohama.mapping import MEASURE_TARGETS, TANK_CAPACITY_L
from app.adapters.yokohama.normalizer import (
    SOURCE,
    normalize_alarm,
    normalize_reading,
    normalize_terminal,
)
from app.domain.contracts import (
    FetchResult,
    MappingReport,
    NormalizedAlarm,
    NormalizedTerminal,
)

log = logging.getLogger(__name__)

UTC = ZoneInfo("UTC")

RECORDS_PATH = "/Data/GetRecordData"
ALARMS_PATH = "/Alarm/GetAlarmData"
# Đo được: 12 bản ghi = 16 KB → 1440 bản ghi/ngày ≈ 1,9 MB.
EST_BYTES_PER_DAY = 2_000_000


class YokohamaAdapter:
    source = SOURCE
    measure_fields = MEASURE_TARGETS

    def __init__(
        self,
        settings: YokohamaSettings | None = None,
        client: YokohamaClient | None = None,
        *,
        store_raw: bool = False,
    ) -> None:
        self._settings = settings or get_yokohama_settings()
        self._client = client or YokohamaClient(self._settings)
        self._store_raw = store_raw
        self._allowed = frozenset(self._settings.psn_list)
        # Một stream/cycle: ngày -> readings. Xoá khi close / cycle mới.
        self._day_cache: dict[date, list] = {}
        self._seen: set[tuple[date, datetime]] = set()
        self._cache_loaded_from: date | None = None
        # Report của LẦN STREAM (một lần mỗi cycle), chờ gắn vào FetchResult đầu
        # tiên. Trước đây ``_ensure_cache`` tạo report cục bộ rồi bỏ đi, nên
        # rejected_rows / zero_as_missing / resolved_from / unmapped_keys của nguồn
        # này LUÔN bằng 0 trong ``ingest_runs`` bất kể thực tế — một con số 0 không
        # có nghĩa còn tệ hơn không có con số nào.
        self._stream_report: MappingReport | None = None

    @property
    def vendor_tz(self) -> ZoneInfo:
        return self._settings.tzinfo

    def close(self) -> None:
        self.begin_cycle()
        self._client.close()

    def begin_cycle(self) -> None:
        self._day_cache.clear()
        self._seen.clear()
        self._cache_loaded_from = None
        self._stream_report = None

    def _permitted(self, psn: str, report: MappingReport) -> bool:
        if psn in self._allowed:
            return True
        report.dropped_foreign_psn += 1
        return False

    def fetch_telemetry(self, psn: str, day: date) -> FetchResult:
        result = FetchResult()
        result.report.fields = self.measure_fields
        if not self._permitted(psn, result.report):
            log.error("ykh: PSN %s không thuộc allowlist, không fetch", psn)
            return result
        self._ensure_cache(day)
        # Cache được lấp theo thứ tự stream (newest-first), nên phải sắp lại:
        # ``FetchResult.readings`` là hợp đồng TĂNG DẦN.
        readings = sorted(self._day_cache.get(day, []), key=lambda r: r.sampled_at)
        result.readings = readings
        result.total = len(readings)
        result.pages_fetched = 1 if self._cache_loaded_from is not None else 0
        result.report.n_rows = len(readings)
        # Gắn report của lần stream vào ĐÚNG MỘT FetchResult mỗi cycle: stream là
        # việc của cả cycle (một lần cho mọi ngày), nên cộng nó vào từng ngày sẽ
        # đếm trùng.
        pending = self._stream_report
        if pending is not None:
            self._stream_report = None
            result.report.source_rows = pending.source_rows
            result.report.newest_source_at = pending.newest_source_at
            result.report.source_device = pending.source_device
            result.report.rejected_rows += pending.rejected_rows
            result.report.zero_as_missing += pending.zero_as_missing
            result.report.resolved_from.update(pending.resolved_from)
            result.report.unmapped_keys |= pending.unmapped_keys
            result.report.errors.extend(pending.errors)
        for r in readings:
            for f in self.measure_fields:
                if getattr(r, f, None) is not None:
                    result.report.present[f] = result.report.present.get(f, 0) + 1
        return result

    def _ensure_cache(self, day: date) -> None:
        if day in self._day_cache:
            return
        tz = self.vendor_tz
        now_local = datetime.now(tz=tz)
        span_days = (now_local.date() - day).days + 1
        budget = self._settings.max_stream_bytes
        if span_days > 0 and span_days * EST_BYTES_PER_DAY > budget:
            allowed = max(1, budget // EST_BYTES_PER_DAY)
            raise YokohamaSchemaError(
                f"cửa sổ {span_days} ngày vượt ngân sách stream "
                f"({budget} byte ≈ {allowed} ngày ở {EST_BYTES_PER_DAY} byte/ngày). "
                f"Nguồn bỏ qua bộ lọc ngày nên chỉ lấy được từ bản ghi mới nhất. "
                f"Muốn lùi xa hơn thì đặt YOKOHAMA_MAX_STREAM_BYTES có ý thức.",
                remediation=f"thu hẹp cửa sổ còn ≤ {allowed} ngày, hoặc nâng trần byte",
            )
        # Stream newest-first; dừng khi dateTime < day (đã qua ngày cần).
        # ĐỪNG "dọn dẹp" hai dòng này cho khớp timestamp_order. Định dạng ngày
        # trong REQUEST không chỉ là cách viết — nó CHỌN BỘ DỮ LIỆU cổng trả về.
        # Đo trực tiếp trên cổng sống 2026-09-04, tái lập 3/3 mỗi chiều:
        #
        #   params dd/mm ("04/09") -> dateTime mm/dd, tankNumber=38, 45,58 m³, tot 747k
        #   params mm/dd ("09/04") -> dateTime dd/mm, tankNumber=70, 53,19 m³, tot 1.132k
        #
        # Tham số ``device`` bị BỎ QUA hoàn toàn (device=all|38|70|1|2 cho kết quả
        # y hệt), nên định dạng ngày là thứ DUY NHẤT quyết định ta đọc chuỗi nào.
        # Mọi dữ liệu đã nạp là tankNumber=38, và dd/mm là cách giữ nguyên nó. Đổi
        # sang mm/dd sẽ ghi số của một bồn KHÁC vào cùng một PSN.
        to_s = now_local.strftime("%d/%m/%Y %H:%M")
        from_s = datetime.combine(day, time.min).strftime("%d/%m/%Y %H:%M")
        psn = self._settings.psn
        report = MappingReport(fields=self.measure_fields)
        cutoff = datetime.combine(day, time.min, tzinfo=tz)
        n = 0
        newest: datetime | None = None
        # ``tankNumber`` từng nằm trong IGNORED_KEYS, nên KHÔNG tầng nào thấy được
        # ta đang đọc bồn nào. Đó là lỗ đúng cỡ để một thay đổi định dạng ngày
        # chuyển ta sang bồn khác mà mọi con số vẫn trông "hợp lý".
        tanks: set[int] = set()
        for obj in self._client.iter_record_objects(
            {
                "device": "all",
                "fromDate": from_s,
                "toDate": to_s,
                "timeFilter": "",
            }
        ):
            n += 1
            tn = obj.get("tankNumber")
            if isinstance(tn, (int, float)) and not isinstance(tn, bool):
                tanks.add(int(tn))
            reading = normalize_reading(
                obj,
                psn=psn,
                vendor_tz=tz,
                report=report,
                store_raw=self._store_raw,
                ts_order=self._settings.timestamp_order,
            )
            if reading is None:
                continue
            # Ghi mốc mới nhất TRƯỚC khi xét cutoff: stream là newest-first, nên
            # nếu bản ghi đầu tiên đã cũ hơn cửa sổ thì vòng lặp break ngay và đây
            # là chỗ DUY NHẤT còn thấy được "nguồn báo lần cuối lúc nào". Không có
            # nó, "logger nhà máy đã chết" và "đường ống hỏng" trông giống nhau.
            if newest is None:
                newest = reading.sampled_at
            local_day = reading.sampled_at.astimezone(tz).date()
            if datetime.combine(local_day, time.min, tzinfo=tz) < cutoff:
                break
            key = (local_day, reading.sampled_at)
            if key in self._seen:
                continue
            self._seen.add(key)
            self._day_cache.setdefault(local_day, []).append(reading)
        self._check_day_month_swap(
            newest, cutoff, now_local, tz, self._settings.timestamp_order
        )
        if len(tanks) > 1:
            raise YokohamaSchemaError(
                f"một stream chứa {len(tanks)} tankNumber khác nhau "
                f"({sorted(tanks)}) — không thể ghi chung một PSN",
                remediation="cổng đang trộn nhiều bồn; dừng nạp và đối chiếu nguồn",
            )
        want = self._settings.tank_number
        if want is not None and tanks and want not in tanks:
            raise YokohamaSchemaError(
                f"cổng trả tankNumber={sorted(tanks)}, cấu hình chờ {want}",
                remediation=(
                    "định dạng ngày trong request CHỌN bộ dữ liệu; kiểm lại request, "
                    "hoặc đổi YOKOHAMA_TANK_NUMBER nếu bồn thật đã khác"
                ),
            )
        report.source_device = None if not tanks else str(sorted(tanks)[0])
        report.source_rows = n
        report.newest_source_at = (
            None if newest is None else newest.astimezone(UTC).isoformat()
        )
        self._stream_report = report
        self._cache_loaded_from = day
        log.info(
            "ykh: stream %s object, cache %s ngày, report zero_as_missing=%s",
            n,
            len(self._day_cache),
            report.zero_as_missing,
        )

    @staticmethod
    def _check_day_month_swap(
        newest: datetime | None,
        cutoff: datetime,
        now_local: datetime,
        tz: ZoneInfo,
        order: str,
    ) -> None:
        """Bắt ngày bị đọc đảo tháng, thay vì âm thầm loại sạch dữ liệu.

        Đo trên production 2026-09-03: cổng trả "09/03/2026 16:53" cho ngày 3
        tháng 9. Parser chỉ nhận ``%d/%m/%Y`` nên nó ra 9 THÁNG 3 — 178 ngày trước
        — rơi ra ngoài cửa sổ và bị loại hết. Hai cycle cách nhau 3 phút cho thấy
        GIỜ tiến đúng 3 phút trong khi NGÀY đứng im: dữ liệu sống, chỉ đọc sai.

        Trước guard này, triệu chứng duy nhất là ``no_data`` — không phân biệt
        được với một bồn im lặng thật.

        KHÔNG tự đảo lại: "03/09" là ngày mơ hồ thật sự, và nếu nhà máy đúng là
        có bản ghi cuối từ 9 tháng 3 với giờ trùng khớp, đảo lại sẽ ghi sai lịch
        sử. Bằng chứng đủ để một người quyết định, nên nêu bằng chứng.
        """
        if newest is None:
            return
        nl = newest.astimezone(tz)
        if nl >= cutoff:
            return  # trong cửa sổ, không có gì phải nghi
        if nl.day > 12 or nl.month > 12:
            return  # không mơ hồ: chỉ một cách đọc hợp lệ
        try:
            swapped = nl.replace(day=nl.month, month=nl.day)
        except ValueError:
            return
        # Chỉ báo khi cách đọc kia rơi ĐÚNG vào cửa sổ đang xin. Slack 1 giờ cho
        # lệch đồng hồ giữa cổng và ta.
        if not (cutoff <= swapped <= now_local + timedelta(hours=1)):
            return
        raise YokohamaSchemaError(
            f"ngày mơ hồ: đang đọc theo {order!r} ra {nl.date().isoformat()} "
            f"(ngoài cửa sổ xin từ {cutoff.date().isoformat()}, nên bị loại "
            f"sạch), còn cách đọc đảo ngày/tháng ra {swapped.date().isoformat()} "
            f"(trong cửa sổ). Giờ khớp {nl.strftime('%H:%M')} nên dữ liệu là "
            f"SỐNG, không phải cũ.",
            remediation=(
                "đặt YOKOHAMA_TIMESTAMP_ORDER cho đúng thứ tự cổng đang gửi "
                "(dmy hoặc mdy) rồi chạy lại một cycle"
            ),
        )

    def fetch_devices(self, psns: list[str]) -> list[NormalizedTerminal]:
        out: list[NormalizedTerminal] = []
        for psn in psns:
            if psn not in self._allowed:
                continue
            term = normalize_terminal(psn)
            if term.capacity_l != TANK_CAPACITY_L:
                raise RuntimeError(
                    f"ykh: capacity_l phải là {TANK_CAPACITY_L}, nhận {term.capacity_l}"
                )
            out.append(term)
        return out

    def fetch_alarms(self, day: date) -> list[NormalizedAlarm]:
        # ``FromDate == ToDate`` LUÔN trả 0 — đo trên cổng sống 2026-09-04, cả ba
        # định dạng (ISO, dd/mm, mm/dd), ba ngày liên tiếp:
        #
        #   From == To    ->   0    0    0
        #   From .. To+1  ->  85  194  192
        #
        # ĐÓ là lý do bảng báo động rỗng suốt, không phải định dạng ngày. Biên trên
        # là loại trừ, nên phải hỏi tới ngày kế tiếp. Chồng lấp giữa hai ngày liền
        # nhau vô hại: khoá tự nhiên của ``vendor_alarms`` có ``message_hash``.
        #
        # GIỮ ISO: nó chạy được, và định dạng ở đây cũng là tác dụng lề như bên
        # endpoint bản ghi (params dd/mm trả một payload khác hẳn).
        payload = self._client.get_json(
            ALARMS_PATH,
            params={
                "Keywords": "",
                "FromDate": day.isoformat(),
                "ToDate": (day + timedelta(days=1)).isoformat(),
            },
        )
        # Hợp đồng là MẢNG ở mức ngoài cùng — đo trên capture thật (716 phần tử).
        # ``else []`` trước đây biến một JSON object lạ, ví dụ {"error": ...} do
        # proxy trả về, thành "hôm nay không có báo động nào", im lặng.
        if not isinstance(payload, list):
            raise YokohamaSchemaError(
                f"{ALARMS_PATH} trả {type(payload).__name__}, không phải mảng",
                remediation="cổng trả sai hình dạng; kiểm URL và đường mạng",
            )
        rows = payload
        out: list[NormalizedAlarm] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            alarm = normalize_alarm(
                row,
                site_code=self._settings.site_code,
                vendor_tz=self.vendor_tz,
                # KHÁC ``timestamp_order`` của bản ghi, và đây không phải sơ suất.
                # Cùng một cổng, cùng một lúc, hai endpoint trả hai định dạng: bản
                # ghi (params dd/mm) ra mm/dd, báo động (params ISO) ra dd/mm. Đo
                # trên 1100 báo động: thành phần đầu lên tới 31 (537 lần > 12),
                # thành phần sau chỉ 8..9 — dd/mm, không đọc cách khác được.
                ts_order=self._settings.alarm_timestamp_order,
            )
            if alarm is not None:
                out.append(alarm)
        return out

    def probe(self) -> dict[str, Any]:
        today = datetime.now(tz=self.vendor_tz).date()
        res = self.fetch_telemetry(self._settings.psn, today)
        return {
            "psn": self._settings.psn,
            "day": today.isoformat(),
            "fetched": len(res.readings),
            "coverage": res.report.coverage(),
            "capacity_l": str(TANK_CAPACITY_L),
        }
