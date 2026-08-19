"""Mapping khai bao raw Xingke -> schema chuan cua cong ty.

DAY LA FILE DUY NHAT PHAI SUA khi vendor doi ten field hoac doi don vi. Ingestion,
repository, API va dashboard khong biet gi ve noi dung file nay.

Moi mapping duoi day da xac minh tren response THAT ngay 2026-08-18
(PSN 2604200016, queryTime=2026-07-23) - xem DISCOVERY.md muc 4. Fixture:
``tests/fixtures/xingke/psn_search_real.json``.

Hai kiem chung noi bo cheo xac nhan don vi bang DU LIEU thay vi bang suy doan::

    pressure     = 71     kPa   <-> pressureMpa = 0.071 MPa    (x1000)  OK
    diffPressure = 0.41   kPa   <-> height      = 42    mmWC
                                   0.41 kPa x 101.972 mmWC/kPa = 41.8 ~ 42  OK

Hai cap doc lap cung khop => don vi vendor trung hop dong cua ta 1:1, KHONG can
convert. Do la ly do moi ``convert`` duoi day deu la None: khong phai vi chua lam,
ma vi da chung minh la khong can.
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

from app.domain.contracts import MEASURE_FIELDS, MappingReport

log = logging.getLogger(__name__)

UTC = ZoneInfo("UTC")

_NORM_RE = re.compile(r"[^a-z0-9]")


def norm_key(k: str) -> str:
    """``hardwarVersion`` -> ``hardwarversion``, ``pressure_mpa`` -> ``pressurempa``.

    Khong phai de phong ly thuyet: hai endpoint cua vendor viet CUNG MOT field
    khac nhau - ``psn/search`` gui ``hardwareVersion`` (dung chinh ta) con
    ``device/list`` gui ``hardwarVersion`` (thieu chu e). Chuan hoa key lam ca hai
    cung resolve ma khong can alias rieng cho tung endpoint.
    """
    return _NORM_RE.sub("", k.lower())


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """Mot field dich va cach lay no ra khoi dict raw.

    ``aliases`` theo thu tu UU TIEN: key da xac minh tren du lieu that dung DAU,
    cac phuong an kha di dung sau lam bao hiem cho endpoint khac / thay doi tuong
    lai. Hit dau tien co mat trong payload se thang.
    """

    target: str
    aliases: tuple[str, ...]
    convert: Callable[[Decimal], Decimal] | None = None
    lo: Decimal | None = None
    hi: Decimal | None = None
    unit: str = ""

    def norm_aliases(self) -> tuple[str, ...]:
        return tuple(norm_key(a) for a in self.aliases)


_D = Decimal

# Da xac minh 1:1, khong convert. lo/hi la bay sai-don-vi: neu vendor doi sang gui
# kPa vao pressureMpa thi 71 vuot hi=5 va ta thay WARNING ngay o lan fetch dau,
# thay vi phat hien sau sau thang du lieu sai.
TELEMETRY_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("pressure_mpa", ("pressureMpa", "pressure_mpa"),
              lo=_D("-1"), hi=_D("5"), unit="MPa"),
    FieldSpec("volume_l", ("currentVolume", "volume", "volumeL"),
              lo=_D("0"), hi=_D("1000000"), unit="L"),
    # Thang 0-100. Vendor tu tinh currentVolume/cylinderVolume*100, nen 0.59
    # nghia la 0.59% DAY - khong phai 59%.
    FieldSpec("volume_percent", ("volumePercentage", "volume_percent", "percent"),
              lo=_D("0"), hi=_D("100"), unit="%"),
    # height la MUC LONG tinh bang mmWC, KHONG phai chieu cao bon. Xac nhan bang
    # cheo voi diffPressure (xem docstring dau file).
    FieldSpec("level_mmwc", ("height", "level", "liquidLevel"),
              lo=_D("0"), hi=_D("100000"), unit="mmWC"),
    FieldSpec("diff_pressure_kpa", ("diffPressure", "diff_pressure"),
              lo=_D("-100"), hi=_D("1000"), unit="kPa"),
    FieldSpec("battery_v", ("currentVoltage", "voltage", "batteryVoltage"),
              lo=_D("0"), hi=_D("30"), unit="V"),
    FieldSpec("signal_percent", ("signalStrengthPercentage", "signalPercent", "signal"),
              lo=_D("0"), hi=_D("100"), unit="%"),
    # temperatureOne, KHONG phai `temperature`. `temperature` la int va bang 0 tren
    # ca hai thiet bi trong khi temperatureOne la null - dau hieu dien hinh cua
    # default value, khong phai phep do. Map `temperature` vao temperature_c se tao
    # ra mot cot toan so 0 trong nhu du lieu that.
    FieldSpec("temperature_c", ("temperatureOne", "temperature_c"),
              lo=_D("-273"), hi=_D("200"), unit="C"),
    FieldSpec("vacuum_pa", ("vacuumTransducerDegreeOne", "vacuum", "vacuumPa"),
              lo=_D("-200000"), hi=_D("200000"), unit="Pa"),
)

TEXT_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("medium_name", ("mediumName",)),
    FieldSpec("tank_type_name", ("tankTypeName",)),
)

# Cau hinh tai san: vendor gui kem moi lan doc nhung no thuoc bang terminals,
# khong phai telemetry.
CAPACITY_FIELD = FieldSpec(
    "capacity_l", ("cylinderVolume", "capacity", "capacityL"),
    lo=_D("0"), hi=_D("10000000"), unit="L",
)

# hardwarVersion (thieu e) la chinh ta cua device/list; hardwareVersion la cua
# psn/search. norm_key lam ca hai resolve nhu nhau, nhung liet ke ca hai de nguoi
# doc file nay thay duoc su that do.
TERMINAL_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("modem_number", ("moduleNumber",)),
    FieldSpec("sim_iccid", ("cardNumber",)),
    FieldSpec("hardware_version", ("hardwareVersion", "hardwarVersion")),
    FieldSpec("software_version", ("softwareVersion",)),
    FieldSpec("device_model", ("deviceMode",)),
    FieldSpec("device_type_name", ("deviceTypeName",)),
)

TIMESTAMP_ALIASES: tuple[str, ...] = (
    "time",           # DA XAC MINH cho psn/search
    "sampleTime",
    "collectTime",
    "createTime",
)

TIMESTAMP_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d %H:%M:%S",       # DA XAC MINH: 2026-07-23 16:03:29
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)

# Field khong phai du lieu, hoac co y bo ngoai giai doan 1. Liet ke tuong minh de
# chung KHONG xuat hien trong unmapped_keys - neu khong thi report canh bao ve
# nhung field ta da can nhac va chu dong loai, va tieng on do se khien nguoi ta
# ngung doc report.
IGNORED_KEYS: frozenset[str] = frozenset(
    norm_key(k)
    for k in (
        "index",                    # so dong phia client, khong phai du lieu
        "pressure",                 # trung pressureMpa, don vi kPa
        "temperature",              # int default 0, xem note o temperature_c
        "medium",                   # code cua mediumName
        "tankType",                 # code cua tankTypeName
        "color", "diameter", "tubeLength", "sendFrequency",
        "electricityPercentage", "currentChargingCurrent",
        # GPS: giai doan 1 KHONG dung ban do. Van nam trong raw_payload nen khong
        # mat gi neu giai doan 2 can.
        "gpsLatitude", "gpsLongitude", "gpsAddress",
        # Sensor phu, null tren ca hai thiet bi. pressureTwpMpa la typo THAT trong
        # payload vendor (Twp thay vi Two) - giu y nguyen.
        "temperatureTwo", "temperatureThree",
        "pressureOne", "pressureOneMpa", "pressureTwo", "pressureTwoMpa",
        "pressureTwpMpa", "pressureThree", "pressureThreeMpa",
        # device/list
        "id", "createId", "createName", "deviceType", "phone",
        "bindStatus", "bindStatusName", "sensorStatus", "sensorStatusName",
        "isSupportFillingStatus", "isSupportFillingStatusName",
    )
)


class TimestampParseError(ValueError):
    """Khong parse duoc timestamp cua vendor.

    Dong do bi loai chu KHONG luu voi thoi gian doan. Mot dong telemetry khong co
    instant dang tin thi vo dung, va te hon la no chiem mot khoa dedup sai.
    """


def parse_vendor_ts(raw: Any, tz: ZoneInfo) -> datetime:
    """Parse naive string cua vendor thanh datetime tz-aware o UTC.

    Vendor gui "2026-07-23 16:03:29" KHONG co offset, render o UTC+8 (da xac minh
    thuc nghiem - DISCOVERY.md muc 5). ``tz`` den tu setting ``XINGKE_VENDOR_TZ``
    chu khong hard-code, vi neu ket luan do sai thi phai sua duoc bang .env.

    Sai timezone o day KHONG phai loi hien thi - no lam hong khoa dedup
    ``(psn, sampled_at)``. Sua parsing ve sau thi moi dong co khoa khac, ON CONFLICT
    khong match, va toan bo lich su bi nhan doi am tham. Vi vay: khong backfill
    truoc khi verify TZ.
    """
    if raw is None or raw == "":
        raise TimestampParseError("timestamp rong")

    # Epoch ms/s - chua thay o endpoint nay nhung re de ho tro.
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        secs = float(raw) / 1000.0 if float(raw) > 1e11 else float(raw)
        return datetime.fromtimestamp(secs, tz=UTC)

    s = str(raw).strip()
    for fmt in TIMESTAMP_FORMATS:
        try:
            naive = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if naive.tzinfo is not None:
            return naive.astimezone(UTC)
        return naive.replace(tzinfo=tz).astimezone(UTC)

    try:
        parsed = datetime.fromisoformat(s)
    except ValueError as exc:
        raise TimestampParseError(f"khong parse duoc timestamp {s!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(UTC)


def build_index(row: dict[str, Any]) -> dict[str, Any]:
    """Index key da chuan hoa -> value. Xay mot lan cho moi dong."""
    return {norm_key(k): v for k, v in row.items()}


def _to_decimal(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        # Qua str: Decimal(0.071) la 0.07099999... con Decimal("0.071") thi dung.
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
    """Lay mot field so, ghi nhan alias nao da dung va bat gia tri ngoai khoang."""
    for alias_norm, alias_orig in zip(spec.norm_aliases(), spec.aliases, strict=True):
        if alias_norm not in index:
            continue
        report.resolved_from.setdefault(spec.target, alias_orig)
        val = _to_decimal(index[alias_norm])
        if val is None:
            # Key CO nhung null - do la cau tra loi hop le (vendor khong do cai
            # nay), nen dung o day thay vi roi xuong alias sau va vo tinh lay mot
            # phep do khac.
            return None

        if spec.convert is not None:
            val = spec.convert(val)

        if (spec.lo is not None and val < spec.lo) or (
            spec.hi is not None and val > spec.hi
        ):
            report.errors.append((
                spec.target,
                f"{val} ngoai khoang [{spec.lo}, {spec.hi}] {spec.unit} "
                f"(alias={alias_orig}) - nghi vendor doi don vi",
            ))
            log.warning(
                "xingke: %s=%s ngoai khoang hop ly [%s,%s] %s (alias=%s). "
                "Nghi vendor doi don vi; gia tri bi loai, raw_payload van giu.",
                spec.target, val, spec.lo, spec.hi, spec.unit, alias_orig,
            )
            return None

        report.present[spec.target] = report.present.get(spec.target, 0) + 1
        return val
    return None


def extract_text(
    index: dict[str, Any], spec: FieldSpec, report: MappingReport
) -> str | None:
    for alias_norm, alias_orig in zip(spec.norm_aliases(), spec.aliases, strict=True):
        if alias_norm not in index:
            continue
        report.resolved_from.setdefault(spec.target, alias_orig)
        v = index[alias_norm]
        if v is None:
            return None
        s = str(v).strip()
        if s in ("", "--"):
            return None
        report.present[spec.target] = report.present.get(spec.target, 0) + 1
        return s
    return None


def find_timestamp(index: dict[str, Any]) -> Any:
    for alias in TIMESTAMP_ALIASES:
        n = norm_key(alias)
        if n in index and index[n] not in (None, ""):
            return index[n]
    return None


_ALL_KNOWN_NORM: frozenset[str] = (
    frozenset(
        a
        for spec in (*TELEMETRY_FIELDS, *TEXT_FIELDS, *TERMINAL_FIELDS, CAPACITY_FIELD)
        for a in spec.norm_aliases()
    )
    | frozenset(norm_key(a) for a in TIMESTAMP_ALIASES)
    | frozenset({norm_key("psn")})
)


def record_unmapped(index: dict[str, Any], report: MappingReport) -> None:
    """Ghi nhan key vendor ma ta chua map va cung chua chu dong bo.

    Vao ``report`` (persist vao ingest_runs) chu khong chi log: khoang trong
    mapping phai noi len qua endpoint admin, khong doi ai di doc log file.
    """
    for k in index:
        if k not in _ALL_KNOWN_NORM and k not in IGNORED_KEYS:
            report.unmapped_keys.add(k)


def assert_mapping_sane() -> None:
    """Bat loi cau hinh luc import, khong phai luc ingest.

    Neu ai do them mot cot do vao MEASURE_FIELDS ma quen FieldSpec thi cot do se
    im lang null mai mai. Day la cho chuyen do thanh loi on ao.
    """
    mapped = [s.target for s in TELEMETRY_FIELDS]
    missing = set(MEASURE_FIELDS) - set(mapped)
    if missing:
        raise RuntimeError(
            f"MEASURE_FIELDS thieu FieldSpec: {sorted(missing)}. "
            "Them vao TELEMETRY_FIELDS trong app/adapters/xingke/mapping.py."
        )
    dupes = {t for t in mapped if mapped.count(t) > 1}
    if dupes:
        raise RuntimeError(f"FieldSpec trung target: {sorted(dupes)}")


assert_mapping_sane()
