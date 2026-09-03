"""XingkeAdapter — implement TelemetryPort. Biên duy nhất biết tên vendor.

Hai cái bẫy của vendor được thi hành ở đây, không ở tầng nào khác
(DISCOVERY.md mục 7):

  Bẫy A: ``device/list?psn=…`` bị BỎ QUA IM LẶNG. Truyền ``psn`` không báo lỗi, chỉ
         trả về thiết bị của tất cả mọi người. Phải dùng ``searchParam``.

  Bẫy B: ``device/list`` không filter trả về 3543 thiết bị của MỌI khách hàng —
         account có org scope nhưng endpoint bỏ qua nó. Đây là lỗi phân quyền phía
         vendor. Vì vậy allowlist PSN được thi hành ở RANH GIỚI ADAPTER: dòng nào
         không thuộc allowlist bị drop và đếm vào report, không bao giờ "ingest hết
         rồi filter sau".
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from app.adapters.xingke.auth import build_auth
from app.adapters.xingke.client import XingkeClient
from app.adapters.xingke.config import XingkeSettings, get_xingke_settings
from app.adapters.xingke.envelope import extract_page
from app.adapters.xingke.normalizer import (
    merge_terminals,
    normalize_reading,
    normalize_terminal,
)
from app.domain.contracts import (
    MEASURE_FIELDS,
    FetchResult,
    MappingReport,
    NormalizedTerminal,
)

log = logging.getLogger(__name__)

SOURCE = "xingke"

PSN_SEARCH_PATH = "infrastructure/server/backstage/device/psn/search"
DEVICE_LIST_PATH = "infrastructure/server/backstage/device/list"


class XingkeAdapter:
    """Adapter thật. Sync — khớp với TelemetryPort và tầng DB sync."""

    source = SOURCE
    measure_fields = MEASURE_FIELDS

    def __init__(
        self,
        settings: XingkeSettings | None = None,
        client: XingkeClient | None = None,
        *,
        store_raw: bool = True,
    ) -> None:
        self._settings = settings or get_xingke_settings()
        self._client = client or XingkeClient(
            self._settings, build_auth(self._settings)
        )
        self._store_raw = store_raw
        self._allowed = self._settings.allowed_psn_set
        if not self._allowed:
            # Không im lặng cho qua: chạy không allowlist trên một endpoint rò dữ
            # liệu khách khác là cách nhanh nhất để ghi dữ liệu của người khác vào
            # DB của mình.
            log.warning(
                "xingke: XINGKE_ALLOWED_PSNS RỖNG. Endpoint backstage của vendor bỏ "
                "qua org scope; không có allowlist thì mọi PSN vendor trả về sẽ được "
                "nhận. Set XINGKE_ALLOWED_PSNS trong .env."
            )

    def close(self) -> None:
        self._client.close()

    def begin_cycle(self) -> None:
        return None

    @property
    def vendor_tz(self) -> ZoneInfo:
        return self._settings.tzinfo

    def _permitted(self, psn: str, report: MappingReport) -> bool:
        if not self._allowed:
            return True
        if psn in self._allowed:
            return True
        report.dropped_foreign_psn += 1
        return False

    def fetch_telemetry(self, psn: str, day: date) -> FetchResult:
        """Các lần đọc của một PSN trong MỘT ngày lịch giờ vendor.

        Endpoint chỉ nhận ``queryTime`` là một ngày ``YYYY-MM-DD`` — không có range.
        Đó là lý do backfill phải walk từng ngày, và là lý do mỗi vòng poll 10 phút
        refetch lại cả ngày (nên ``duplicates`` lớn là hoạt động ĐÚNG).
        """
        result = FetchResult()
        report = result.report

        if not self._permitted(psn, report):
            log.error("xingke: PSN %s không thuộc allowlist, không fetch", psn)
            return result

        page = 1
        while page <= self._settings.max_pages:
            data = self._client.get(
                PSN_SEARCH_PATH,
                params={
                    "currentPage": page,  # 1-based, đã xác minh
                    "pageSize": self._settings.page_size,
                    "psn": psn,
                    "queryTime": day.isoformat(),
                },
            )
            rows, total = extract_page(data)
            result.pages_fetched += 1
            if result.total is None:
                result.total = total

            if not rows:
                break

            for row in rows:
                # ĐẾM TRƯỚC allowlist: source_rows là "nguồn gửi bao nhiêu", còn
                # n_rows là "ta giữ bao nhiêu". Chênh lệch chính là
                # dropped_foreign_psn — cổng này từng rò 3543 thiết bị của khách
                # khác, nên khoảng cách đó là một con số cần thấy, không phải ẩn.
                report.source_rows += 1
                row_psn = str(row.get("psn") or psn).strip()
                if not self._permitted(row_psn, report):
                    continue
                report.n_rows += 1
                reading = normalize_reading(
                    row,
                    psn=row_psn,
                    source=SOURCE,
                    vendor_tz=self._settings.tzinfo,
                    report=report,
                    store_raw=self._store_raw,
                )
                if reading is not None:
                    result.readings.append(reading)

            seen = len(result.readings) + report.rejected_rows
            # Dừng khi đã lấy đủ theo `total`, hoặc trang chưa đầy (trang cuối).
            if total is not None and seen >= total:
                break
            if len(rows) < self._settings.page_size:
                break
            page += 1
        else:
            log.warning(
                "xingke: đạt max_pages=%s cho psn=%s day=%s — nghi parse sai `total`",
                self._settings.max_pages,
                psn,
                day,
            )

        if report.dropped_foreign_psn:
            log.warning(
                "xingke: drop %s dòng có PSN ngoài allowlist (endpoint vendor bỏ qua "
                "org scope)",
                report.dropped_foreign_psn,
            )
        return result

    def fetch_devices(self, psns: list[str]) -> list[NormalizedTerminal]:
        """Metadata cho ĐÚNG những PSN được yêu cầu.

        Ký hiệu nhận list tường minh thay vì "liệt kê tất cả" là có chủ đích: không
        có cách nào diễn đạt "lấy hết" bằng API này, vì "lấy hết" nghĩa là 3543
        thiết bị của các khách hàng khác.
        """
        out: list[NormalizedTerminal] = []
        report = MappingReport()
        for psn in psns:
            if not self._permitted(psn, report):
                continue
            merged = merge_terminals(self._device_row(psn, report))
            if merged is not None:
                out.append(merged)
        return out

    def _device_row(
        self, psn: str, report: MappingReport
    ) -> NormalizedTerminal | None:
        data = self._client.get(
            DEVICE_LIST_PATH,
            params={
                "currentPage": 1,
                "pageSize": self._settings.page_size,
                # searchParam, KHÔNG phải psn — xem Bẫy A ở docstring đầu file.
                "searchParam": psn,
            },
        )
        rows, _ = extract_page(data)
        for row in rows:
            if str(row.get("psn") or "").strip() == psn:
                return normalize_terminal(row, psn=psn, report=report)
        log.info("xingke: device/list không có PSN %s", psn)
        return None

    def probe(self) -> dict[str, Any]:
        """Kiểm tra kết nối + auth, trả về summary an toàn để log.

        Dùng ``probe_date`` (một ngày CÓ dữ liệu) chứ không phải hôm nay: cả hai
        thiết bị đã offline hàng tháng nên hôm nay trả rỗng, và một kết quả rỗng
        rất dễ bị chẩn đoán sai thành "auth lỗi".
        """
        self._client.ensure_authenticated()
        day = date.fromisoformat(self._settings.probe_date)
        res = self.fetch_telemetry(self._settings.probe_psn, day)
        return {
            "psn": self._settings.probe_psn,
            "day": day.isoformat(),
            "readings": len(res.readings),
            "total": res.total,
            "pages": res.pages_fetched,
            "coverage": res.report.coverage(),
            "always_null": res.report.always_null(),
            "unmapped_keys": sorted(res.report.unmapped_keys),
            "errors": res.report.errors,
        }
