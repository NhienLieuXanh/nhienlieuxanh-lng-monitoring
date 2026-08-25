"""Áp mapping.py lên payload raw -> NormalizedTelemetry / NormalizedTerminal.

Tách khỏi adapter.py có chủ đích: file này thuần hàm, không I/O, nên test được
bằng fixture JSON mà không cần mock HTTP. Toàn bộ kiến thức về "tên field vendn
là gì" nằm ở mapping.py; file này chỉ biết cách áp dụng nó.
"""

from __future__ import annotations

import logging
from typing import Any
from zoneinfo import ZoneInfo

from app.adapters.xingke import mapping as M
from app.domain.contracts import (
    MappingReport,
    NormalizedTelemetry,
    NormalizedTerminal,
    PercentSource,
)

log = logging.getLogger(__name__)


def normalize_reading(
    row: dict[str, Any],
    *,
    psn: str,
    source: str,
    vendor_tz: ZoneInfo,
    report: MappingReport,
    store_raw: bool = True,
) -> NormalizedTelemetry | None:
    """Một dòng raw -> một lần đọc chuẩn hoá, hoặc None nếu dòng không dùng được.

    Trả None thay vì raise: một dòng lỗi trong 100 dòng không được làm chết cả lần
    fetch. Số dòng bị loại được đếm vào ``report.rejected_rows`` nên nó vẫn hiện ra
    chứ không im lặng biến mất.
    """
    index = M.build_index(row)
    M.record_unmapped(index, report)

    raw_ts = M.find_timestamp(index)
    try:
        sampled_at = M.parse_vendor_ts(raw_ts, vendor_tz)
    except M.TimestampParseError as exc:
        report.rejected_rows += 1
        report.errors.append(("sampled_at", str(exc)))
        log.warning("xingke: loại dòng psn=%s vì timestamp không parse được: %s",
                    psn, exc)
        return None

    values: dict[str, Any] = {}
    for spec in M.TELEMETRY_FIELDS:
        values[spec.target] = M.extract_number(index, spec, report)
    for spec in M.TEXT_FIELDS:
        values[spec.target] = M.extract_text(index, spec, report)

    capacity_l = M.extract_number(index, M.CAPACITY_FIELD, report)
    latitude, longitude = M.extract_gps(index, report)

    # Nhãn nguồn của volume_percent. Vendor CÓ gửi volumePercentage trên endpoint
    # này, nhưng endpoint khác có thể không — và trộn giá trị vendor gửi với giá
    # trị ta tự tính vào cùng một cột mà không nhãn thì sáu tháng sau không ai
    # phản nghiệm được một cuộc điều tra sai lệch nào.
    percent_source = (
        PercentSource.VENDOR if values.get("volume_percent") is not None else None
    )

    return NormalizedTelemetry(
        source=source,
        psn=psn,
        sampled_at=sampled_at,
        vendor_ts_raw=str(raw_ts) if raw_ts is not None else None,
        volume_percent_source=percent_source,
        capacity_l=capacity_l,
        latitude=latitude,
        longitude=longitude,
        raw_payload=dict(row) if store_raw else {},
        **values,
    )


def normalize_terminal(
    row: dict[str, Any], *, psn: str, report: MappingReport
) -> NormalizedTerminal:
    """Metadata thiết bị. Dùng được cho CẢ psn/search và device/list.

    Hai endpoint viết cùng một field khác nhau (`hardwareVersion` vs
    `hardwarVersion`); norm_key() làm cả hai resolve như nhau nên hàm này không
    cần biết nó đang đọc endpoint nào.
    """
    index = M.build_index(row)
    values = {
        spec.target: M.extract_text(index, spec, report) for spec in M.TERMINAL_FIELDS
    }
    return NormalizedTerminal(
        psn=psn,
        modem_number=values.get("modem_number"),
        sim_iccid=values.get("sim_iccid"),
        hardware_version=values.get("hardware_version"),
        software_version=values.get("software_version"),
        capacity_l=M.extract_number(index, M.CAPACITY_FIELD, report),
        medium_name=M.extract_text(index, M.TEXT_FIELDS[0], report),
        tank_type_name=M.extract_text(index, M.TEXT_FIELDS[1], report),
        raw_payload=dict(row),
    )


def merge_terminals(*parts: NormalizedTerminal | None) -> NormalizedTerminal | None:
    """Hợp metadata từ nhiều endpoint, ưu tiên giá trị non-None xuất hiện trước.

    Cần thiết vì không endpoint nào cho đủ: `moduleNumber`/`cardNumber` chỉ có ở
    psn/search, còn `deviceMode`/`deviceTypeName` chỉ có ở device/list.
    """
    live = [p for p in parts if p is not None]
    if not live:
        return None
    base = live[0]
    merged = base.model_dump()
    for other in live[1:]:
        for k, v in other.model_dump().items():
            if k == "raw_payload":
                continue
            if merged.get(k) is None and v is not None:
                merged[k] = v
        merged["raw_payload"] = {**other.raw_payload, **merged.get("raw_payload", {})}
    return NormalizedTerminal(**merged)
