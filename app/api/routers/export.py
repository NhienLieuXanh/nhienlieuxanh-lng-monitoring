"""Xuất báo cáo CSV: telemetry thô, nhật ký nạp, và bảng tổng hợp bồn.

Ba quyết định thực dụng, mỗi cái sửa một lỗi cụ thể hay gặp khi gửi CSV cho người
dùng Việt Nam mở bằng Excel:

1. **UTF-8 có BOM** (``utf-8-sig``). Không có BOM, Excel trên Windows đọc file
   theo codepage hệ thống và mọi tên bồn tiếng Việt thành ký tự rác. Đây là lỗi
   mà người nhận báo cáo thấy đầu tiên và không ai sửa được ở đầu họ.
2. **Thời gian ``YYYY-MM-DD HH:MM:SS`` theo APP_TZ**, không phải ISO có offset.
   Excel nhận ra dạng này là ngày-giờ và cho lọc/vẽ ngay; dạng
   ``2026-08-19T07:30:00+00:00`` thì nó coi là chuỗi. Đồng thời số đã quy về giờ
   Việt Nam nên không ai phải cộng 7 tiếng trong đầu.
3. **``delimiter`` cho chọn**. Windows locale tiếng Việt dùng ``;`` làm dấu phân
   tách danh sách; mặc định ``,`` đúng cho phần lớn máy nhưng để sẵn đường thoát
   thay vì bắt người ta sửa file.

Cột xuất ra lấy từ ``tel_repo.EXPORT_COLUMNS`` — query chỉ SELECT đúng các cột đó
nên ``raw_payload`` (JSONB key tiếng Trung) không thể lọt vào file gửi ra ngoài,
dù ai sửa code phía trên thế nào.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.api.deps import HistoryQueryDep, SessionDep, SettingsDep, UserDep
from app.config import Settings
from app.domain import forecast as fc
from app.domain.alerts import fill_percent
from app.domain.status import derive_status
from app.repositories import telemetry as tel_repo
from app.repositories import terminals as term_repo

log = logging.getLogger(__name__)
router = APIRouter(prefix="/export", tags=["export"])
UTC = ZoneInfo("UTC")

DelimiterQ = Annotated[str, Query(pattern=r"^(comma|semicolon|tab)$")]
_DELIMS = {"comma": ",", "semicolon": ";", "tab": "\t"}

TS_FMT = "%Y-%m-%d %H:%M:%S"


def _csv(
    rows: list[list[Any]], header: list[str], *, filename: str, delimiter: str
) -> Response:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, delimiter=delimiter, lineterminator="\r\n")
    w.writerow(header)
    w.writerows(rows)
    # encode utf-8-sig: BOM ở đầu file là thứ duy nhất khiến Excel trên Windows
    # đọc đúng tiếng Việt mà người nhận không phải làm gì.
    return Response(
        content=buf.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _ts(dt: datetime | None, settings: Settings) -> str:
    if dt is None:
        return ""
    return dt.astimezone(settings.tzinfo).strftime(TS_FMT)


def _num(v: Any) -> str:
    """Số ra chuỗi, giữ nguyên độ chính xác Decimal, ô trống cho None.

    Ô trống chứ KHÔNG phải 0: trên báo cáo mức LNG, một ô 0 nghĩa là "đo được và
    bằng 0" còn ô trống nghĩa là "vendor không gửi field này". Gộp hai thứ đó là
    bịa dữ liệu.
    """
    if v is None:
        return ""
    if isinstance(v, Decimal):
        return format(v.normalize(), "f")
    return str(v)


def _m3(v: float | None) -> str:
    """Lít -> m³. Báo cáo cho người đọc dùng m³ (khối), giống dashboard."""
    return "" if v is None else f"{v / 1000:.3f}"


def _num2(v: float | None, nd: int) -> str:
    return "" if v is None else f"{v:.{nd}f}"


@router.get("/telemetry.csv", response_class=Response)
def export_telemetry(
    session: SessionDep,
    settings: SettingsDep,
    q: HistoryQueryDep,
    _: UserDep,
    psn: Annotated[str, Query(min_length=1, max_length=32)],
    delimiter: DelimiterQ = "comma",
) -> Response:
    """Telemetry thô trong khoảng [from, to].

    Dùng lại ``HistoryQueryDep`` để có sẵn hai thứ đã đúng ở đó: datetime naive
    được localize theo ``APP_TZ`` (không phải UTC), và trần 90 ngày mỗi lần xuất.
    ``page``/``limit`` của dependency đó bị bỏ qua ở đây một cách có ý thức —
    báo cáo phải trọn khoảng, phân trang một file CSV là vô nghĩa.
    """
    if term_repo.get_by_psn(session, psn) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "PSN không tồn tại")
    assert q.from_ is not None and q.to is not None
    rows = tel_repo.export_rows(session, psn, q.from_, q.to)
    out = [[_ts(r[0], settings), *[_num(v) for v in r[1:]]] for r in rows]
    header = ["thoi_diem", *tel_repo.EXPORT_COLUMNS[1:]]
    fname = f"telemetry_{psn}_{q.from_:%Y%m%d}_{q.to:%Y%m%d}.csv"
    return _csv(out, header, filename=fname, delimiter=_DELIMS[delimiter])


@router.get("/refills.csv", response_class=Response)
def export_refills(
    session: SessionDep,
    settings: SettingsDep,
    _: UserDep,
    psn: Annotated[str | None, Query(max_length=32)] = None,
    window_days: Annotated[int, Query(ge=1, le=365)] = 90,
    delimiter: DelimiterQ = "comma",
) -> Response:
    """Nhật ký nạp. Không có ``psn`` thì xuất mọi bồn trong một file."""
    now = datetime.now(tz=UTC)
    start = now - timedelta(days=window_days)
    if psn:
        term = term_repo.get_by_psn(session, psn)
        if term is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "PSN không tồn tại")
        terms = [term]
    else:
        terms = term_repo.list_all(session)

    out: list[list[Any]] = []
    for t in terms:
        cap = None if t.capacity_l is None else float(t.capacity_l)
        rows = tel_repo.series(session, t.psn, start, now)
        samples = [fc.Sample(at=at, volume_l=v, pressure_mpa=p) for at, v, p in rows]
        for e in fc.detect_refills(samples, capacity_l=cap):
            out.append(
                [
                    _ts(e.at, settings),
                    t.psn,
                    t.name or "",
                    f"{e.before_l / 1000:.3f}",
                    f"{e.after_l / 1000:.3f}",
                    f"{e.amount_l / 1000:.3f}",
                ]
            )
    out.sort(key=lambda r: str(r[0]), reverse=True)
    header = ["thoi_diem", "psn", "ten_bon", "truoc_m3", "sau_m3", "luong_nap_m3"]
    fname = f"nhat_ky_nap_{now:%Y%m%d}.csv"
    return _csv(out, header, filename=fname, delimiter=_DELIMS[delimiter])


@router.get("/tanks.csv", response_class=Response)
def export_tanks(
    session: SessionDep,
    settings: SettingsDep,
    _: UserDep,
    window_days: Annotated[int | None, Query(ge=1, le=365)] = None,
    delimiter: DelimiterQ = "comma",
) -> Response:
    """Bảng tổng hợp: trạng thái hiện tại + dự báo cho mọi bồn.

    Đây là tờ báo cáo một trang cho người không mở dashboard: mức hiện tại, mức
    dùng/ngày ĐO ĐƯỢC kèm độ tin cậy, ngày tới cạn, boil-off, hold time và lượng
    đề xuất đặt. Cột ``do_tin_cay`` đi liền cột ``muc_dung_ngay_m3`` là cố ý —
    một con số dự báo không có độ tin cậy bên cạnh sẽ bị đọc như số đo.
    """
    now = datetime.now(tz=UTC)
    win = window_days or settings.forecast_window_days
    stale = timedelta(minutes=settings.online_stale_minutes)
    terms = term_repo.list_all(session)
    latest = tel_repo.latest_many(session, [t.psn for t in terms])

    out: list[list[Any]] = []
    for t in terms:
        lt = latest.get(t.psn)
        cap = None if t.capacity_l is None else float(t.capacity_l)
        vol = None if (lt is None or lt.volume_l is None) else float(lt.volume_l)
        pres = (
            None if (lt is None or lt.pressure_mpa is None) else float(lt.pressure_mpa)
        )
        rows = tel_repo.series(session, t.psn, now - timedelta(days=win), now)
        samples = [fc.Sample(at=at, volume_l=v, pressure_mpa=p) for at, v, p in rows]
        f = fc.build_forecast(
            samples,
            psn=t.psn,
            volume_l=vol,
            capacity_l=cap,
            pressure_mpa=pres,
            now=now,
            tz=settings.tzinfo,
            reserve_percent=settings.forecast_reserve_percent,
            lead_time_days=settings.forecast_lead_time_days,
            service_level=settings.forecast_service_level,
            relief_mpa=settings.lng_relief_pressure_mpa,
            max_fill_percent=settings.lng_max_fill_percent,
        )
        fp = fill_percent(lt.volume_l if lt else None, t.capacity_l)
        out.append(
            [
                t.psn,
                t.name or "",
                derive_status(t.last_seen_at, now, stale).value,
                _ts(t.last_seen_at, settings),
                _m3(cap),
                _m3(vol),
                "" if fp is None else f"{fp:.2f}",
                _m3(f.consumption.daily_use_l),
                f.consumption.confidence,
                f"{f.consumption.coverage * 100:.0f}",
                _num2(f.idle.boil_off_percent_per_day, 3),
                f.idle.method,
                _num2(f.runout.days_to_reserve, 1),
                _num2(f.runout.days_to_empty, 1),
                _ts(f.runout.empty_at, settings),
                _num2(f.hold.days, 1),
                _m3(f.suggestion.order_l),
                _ts(f.suggestion.order_at, settings),
                f.suggestion.urgency,
            ]
        )

    header = [
        "psn",
        "ten_bon",
        "trang_thai",
        "lan_cuoi_nhan_du_lieu",
        "dung_tich_m3",
        "muc_hien_tai_m3",
        "phan_tram_day",
        "muc_dung_ngay_m3",
        "do_tin_cay",
        "do_phu_du_lieu_pct",
        "bay_hoi_pct_ngay",
        "nguon_bay_hoi",
        "ngay_toi_du_tru",
        "ngay_toi_can",
        "du_kien_can_luc",
        "hold_time_ngay",
        "de_xuat_dat_m3",
        "de_xuat_dat_luc",
        "muc_do_gap",
    ]
    fname = f"tong_hop_bon_{now.astimezone(settings.tzinfo):%Y%m%d_%H%M}.csv"
    return _csv(out, header, filename=fname, delimiter=_DELIMS[delimiter])
