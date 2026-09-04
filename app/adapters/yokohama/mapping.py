"""Mapping raw nguồn đo phút -> schema chuẩn.

Tên field của nguồn ĐẶT NGƯỢC: tankPrecent là thể tích (m³), tankVolume là mức (%).
Đã đối chứng ảnh trang Main 27/08 13:18: Volume 53.58 m³ / Level 89.30 %.
Không tin tên field. lo/hi bắt đọc ngược (89 300 L > 61 000).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from app.domain.contracts import ALL_MEASURE_FIELDS, MappingReport, NormalizedTelemetry

log = logging.getLogger(__name__)

UTC = ZoneInfo("UTC")
_NORM_RE = re.compile(r"[^a-z0-9]")
_D = Decimal

# 60,000 m³ lỏng = 60 000 L. Đo tankPrecent/tankVolume×100 = 59,960…60,051.
TANK_CAPACITY_L = _D("60000")
TANK_CAPACITY_M3 = _D("60")
CAPACITY_RATIO_TOLERANCE = _D("0.005")  # 0,5 %


def norm_key(k: str) -> str:
    return _NORM_RE.sub("", k.lower())


@dataclass(frozen=True, slots=True)
class FieldSpec:
    target: str
    aliases: tuple[str, ...]
    convert: Callable[[Decimal], Decimal] | None = None
    lo: Decimal | None = None
    hi: Decimal | None = None
    unit: str = ""
    zero_is_missing: bool = False

    def norm_aliases(self) -> tuple[str, ...]:
        return tuple(norm_key(a) for a in self.aliases)


def _m3_to_l(v: Decimal) -> Decimal:
    return v * _D("1000")


def _bar_to_mpa(v: Decimal) -> Decimal:
    return v * _D("0.1")


# volume_l hi=61000: dung tích đo được ~60 051 L; đọc ngược (level×1000) = 89 300
# vượt ngay bản ghi đầu.
TELEMETRY_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "volume_l",
        ("tankPrecent",),
        convert=_m3_to_l,
        lo=_D("0"),
        hi=_D("61000"),
        unit="L",
        zero_is_missing=True,
    ),
    FieldSpec(
        "volume_percent",
        ("tankVolume",),
        lo=_D("0"),
        hi=_D("100"),
        unit="%",
        zero_is_missing=True,
    ),
    FieldSpec(
        "pressure_mpa",
        ("pT1_Value",),
        convert=_bar_to_mpa,
        lo=_D("-1"),
        hi=_D("5"),
        unit="MPa",
        zero_is_missing=True,
    ),
    FieldSpec(
        "temperature_c",
        ("tE1_Value",),
        lo=_D("-273"),
        hi=_D("200"),
        unit="C",
    ),
    FieldSpec(
        "gm_totalizer_nm3",
        ("totalizer",),
        lo=_D("0"),
        hi=_D("100000000"),
        unit="Nm3",
        zero_is_missing=True,
    ),
    FieldSpec(
        "gm_flow_rate_nm3h",
        ("flowRate",),
        lo=_D("0"),
        hi=_D("100000"),
        unit="Nm3/h",
    ),
    FieldSpec(
        "gm_pressure_kpa",
        ("pressure",),
        lo=_D("-50"),
        hi=_D("2000"),
        unit="kPa",
    ),
    FieldSpec(
        "gm_temperature_c",
        ("temperature",),
        lo=_D("-273"),
        hi=_D("200"),
        unit="C",
    ),
    FieldSpec(
        "ps1_bar",
        ("pS1_Value",),
        lo=_D("0"),
        hi=_D("50"),
        unit="bar",
        # 0,00 bar là GIÁ TRỊ THẬT, không phải thiếu dữ liệu — và đúng là
        # giá trị đang báo động. Trang Main của cổng hiển thị "0.00 bar"
        # (tô cam), và danh sách báo động có PS1 25 lần + PS2 28 lần
        # trong 7 ngày. Coi nó là thiếu dữ liệu tức là che đúng cái
        # điều kiện nhà máy đang báo. Đo trên cổng sống 2026-09-04.
        zero_is_missing=False,
    ),
    FieldSpec(
        "ps2_bar",
        ("pS2_Value",),
        lo=_D("0"),
        hi=_D("50"),
        unit="bar",
        # 0,00 bar là GIÁ TRỊ THẬT, không phải thiếu dữ liệu — và đúng là
        # giá trị đang báo động. Trang Main của cổng hiển thị "0.00 bar"
        # (tô cam), và danh sách báo động có PS1 25 lần + PS2 28 lần
        # trong 7 ngày. Coi nó là thiếu dữ liệu tức là che đúng cái
        # điều kiện nhà máy đang báo. Đo trên cổng sống 2026-09-04.
        zero_is_missing=False,
    ),
    FieldSpec(
        "gd1_percent",
        ("gD1_Value",),
        lo=_D("0"),
        hi=_D("100"),
        unit="%",
    ),
    FieldSpec(
        "gd2_percent",
        ("gD2_Value",),
        lo=_D("0"),
        hi=_D("100"),
        unit="%",
    ),
    FieldSpec(
        "gd3_percent",
        ("gD3_Value",),
        lo=_D("0"),
        hi=_D("100"),
        unit="%",
    ),
)

MEASURE_TARGETS: tuple[str, ...] = tuple(s.target for s in TELEMETRY_FIELDS)

TIMESTAMP_ALIASES: tuple[str, ...] = ("dateTime", "receivedAt")
# HAI HẰNG SỐ, không phải cấu hình, và chúng KHÁC NHAU. Cổng PARSE ngày gửi lên
# theo mm/dd, và LUÔN XUẤT ra dd/mm. Đo trực tiếp bằng ngày không mơ hồ nên không
# còn chỗ cho suy đoán:
#
#   gửi "08/20/2026" (mm/dd = 20/8)  -> trả "20/08/2026 23:58", refill 67, tot 1.132.100
#   gửi "04/09/2026" (mm/dd = 9/4)   -> trả "09/04/2026 23:58", refill 38, tot   749.328
#   gửi "09/04/2026" (mm/dd = 4/9)   -> trả "04/09/2026 11:54", refill 70, tot 1.132.428
#   gửi "20/08/2026" (dd/mm)         -> cổng KHÔNG parse được -> rơi về bản mới nhất
#
# refill và totalizer tăng đơn điệu theo ngày, nên đây là MỘT bồn, MỘT đồng hồ.
#
# Vì sao điều này từng làm hỏng dữ liệu: gửi ngày kiểu dd/mm thì cổng đọc "04/09"
# thành 9 THÁNG 4 và trả về dữ liệu tháng 4, in ra "09/04/2026"; code đọc chuỗi đó
# theo mm/dd lại ra 4 tháng 9. Kết quả là dữ liệu tháng 4 được cất dưới mốc tháng 9,
# lệch 5 tháng, và KHÔNG tầng nào báo lỗi vì mốc rơi đúng vào cửa sổ đang xin.
# 2040 dòng đã bị ghi sai như vậy trước khi phát hiện.
#
# Chỉ MỘT thứ tự được thử khi đọc, không bao giờ cả hai: ngày mơ hồ như "03/09" thì
# thử cả hai là chọn bừa, và một lần chọn sai ghi vào lịch sử thì không phản nghiệm
# được nữa.
REQUEST_DATE_FMT = "%m/%d/%Y"
RECORD_TS_ORDER = "dmy"
ALARM_TS_ORDER = "dmy"
TIMESTAMP_FORMATS_DMY: tuple[str, ...] = (
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
)
TIMESTAMP_FORMATS_MDY: tuple[str, ...] = (
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
)
TIMESTAMP_ORDERS: dict[str, tuple[str, ...]] = {
    "dmy": TIMESTAMP_FORMATS_DMY,
    "mdy": TIMESTAMP_FORMATS_MDY,
}
# Giữ tên cũ cho code/test đã tham chiếu.
TIMESTAMP_FORMATS: tuple[str, ...] = TIMESTAMP_FORMATS_DMY

IGNORED_KEYS: frozenset[str] = frozenset(
    norm_key(k) for k in ("receivedAt", "tankNumber")
)


class TimestampParseError(ValueError):
    """Không parse được timestamp. Dòng bị loại, không đoán giờ."""


def parse_vendor_ts(raw: Any, tz: ZoneInfo, *, order: str = "dmy") -> datetime:
    if raw is None or raw == "":
        raise TimestampParseError("timestamp rỗng")
    formats = TIMESTAMP_ORDERS.get(order)
    if formats is None:
        raise TimestampParseError(
            f"thứ tự ngày {order!r} không hợp lệ; chọn {sorted(TIMESTAMP_ORDERS)}"
        )
    s = str(raw).strip()
    for fmt in formats:
        try:
            naive = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=tz).astimezone(UTC)
    raise TimestampParseError(f"không parse được timestamp {s!r}")


def build_index(row: dict[str, Any]) -> dict[str, Any]:
    return {norm_key(k): v for k, v in row.items()}


def _to_decimal(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    s = str(v).strip()
    if s in ("", "--", "-", "null", "None", "N/A"):
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def extract_number(
    index: dict[str, Any], spec: FieldSpec, report: MappingReport
) -> Decimal | None:
    for alias_norm, alias_orig in zip(spec.norm_aliases(), spec.aliases, strict=True):
        if alias_norm not in index:
            continue
        report.resolved_from.setdefault(spec.target, alias_orig)
        val = _to_decimal(index[alias_norm])
        if val is None:
            return None
        if spec.zero_is_missing and val == 0:
            report.zero_as_missing += 1
            return None
        if spec.convert is not None:
            val = spec.convert(val)
        if (spec.lo is not None and val < spec.lo) or (
            spec.hi is not None and val > spec.hi
        ):
            report.errors.append((
                spec.target,
                f"{val} ngoài khoảng [{spec.lo}, {spec.hi}] {spec.unit} "
                f"(alias={alias_orig}) — nghi đọc ngược tên field hoặc sai đơn vị",
            ))
            log.warning(
                "ykh: %s=%s ngoài khoảng [%s,%s] %s (alias=%s)",
                spec.target, val, spec.lo, spec.hi, spec.unit, alias_orig,
            )
            return None
        report.present[spec.target] = report.present.get(spec.target, 0) + 1
        return val
    return None


def extract_refill_counter(index: dict[str, Any]) -> int | None:
    raw = index.get(norm_key("tankNumber"))
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def find_timestamp(index: dict[str, Any]) -> Any:
    for alias in TIMESTAMP_ALIASES:
        key = norm_key(alias)
        if key in index and index[key] not in (None, ""):
            return index[key]
    return None


def record_unmapped(index: dict[str, Any], report: MappingReport) -> None:
    known = (
        {a for spec in TELEMETRY_FIELDS for a in spec.norm_aliases()}
        | {norm_key(a) for a in TIMESTAMP_ALIASES}
        | IGNORED_KEYS
        | {norm_key("tankNumber")}
    )
    for k in index:
        if k not in known:
            report.unmapped_keys.add(k)


def capacity_from_ratio(volume_l: Decimal | None, volume_percent: Decimal | None) -> Decimal | None:
    """Đối chứng dung tích từ (m³, %). Lệch > 0,5% so với 60 m³ → None."""
    if volume_l is None or volume_percent is None or volume_percent == 0:
        return None
    cap_l = volume_l / (volume_percent / _D("100"))
    if abs(cap_l - TANK_CAPACITY_L) / TANK_CAPACITY_L > CAPACITY_RATIO_TOLERANCE:
        return None
    return TANK_CAPACITY_L


def assert_mapping_sane(fields: tuple[FieldSpec, ...] = TELEMETRY_FIELDS) -> None:
    names = set(NormalizedTelemetry.model_fields)
    mapped = [s.target for s in fields]
    unknown = [t for t in mapped if t not in names]
    if unknown:
        raise RuntimeError(f"ykh FieldSpec.target không có trên NormalizedTelemetry: {unknown}")
    not_measure = [t for t in mapped if t not in ALL_MEASURE_FIELDS]
    if not_measure:
        raise RuntimeError(f"ykh FieldSpec.target không nằm trong ALL_MEASURE_FIELDS: {not_measure}")
    dupes = {t for t in mapped if mapped.count(t) > 1}
    if dupes:
        raise RuntimeError(f"ykh FieldSpec trùng target: {sorted(dupes)}")
    zeroed = [s for s in fields if s.zero_is_missing]
    if not zeroed:
        raise RuntimeError("ykh: không có FieldSpec zero_is_missing")
    for spec in zeroed:
        if not spec.aliases:
            raise RuntimeError(f"ykh: {spec.target} zero_is_missing nhưng không alias")


assert_mapping_sane()
