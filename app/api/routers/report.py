"""Báo cáo giám sát bồn LNG: một trang HTML in được thẳng ra A4 / PDF.

Vì sao là HTML in được chứ không phải PDF sinh từ server:

1. **Tiếng Việt có dấu.** Mọi thư viện PDF thuần Python đều cần nhúng kèm một
   font Unicode (~700 KB) mới hiển thị được dấu; thiếu font là mất dấu toàn bộ
   báo cáo, và đó là loại lỗi chỉ lộ ra ở bản in cuối cùng. Trình duyệt đã có
   sẵn font đúng.
2. **Không thêm dependency.** Hàm chạy trên serverless có trần thời gian; mỗi
   thư viện thêm vào là thêm thời gian khởi động lạnh cho MỌI request, không chỉ
   request xuất báo cáo.
3. **In → Lưu thành PDF là chức năng có sẵn** của mọi trình duyệt, kèm xem trước.

Ba quyết định về nội dung, mỗi cái sửa một cách đọc sai cụ thể:

- **Kỳ báo cáo neo vào lần đo cuối, không phải hôm nay.** Thiết bị có thể im
  hàng tuần; mặc định "30 ngày tính từ hôm nay" cho ra một tờ báo cáo trống mà
  người đọc lại hiểu là "không có gì xảy ra".
- **Tuổi số liệu đứng cạnh mọi con số dự báo.** "Còn 11,7 ngày tới cạn" dựng
  trên số đo của tháng trước là con số sai theo cách khó phát hiện nhất.
- **Ô trống nghĩa là không đo được, không phải bằng 0.** Cùng nguyên tắc với
  ``export.py``: gộp hai thứ đó là bịa dữ liệu, và trên tờ giấy có chữ ký thì
  bịa dữ liệu là chuyện nghiêm trọng.

Cảnh báo trong báo cáo lấy từ ``notifier.collect_notices`` — đúng hàm mà email
cảnh báo dùng. Nhờ vậy báo cáo in ra không thể nói khác email đã gửi.
"""

from __future__ import annotations

import base64
import html
import logging
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import HTMLResponse

from app.api.deps import SessionDep, SettingsDep, UserDep
from app.domain import analytics as an
from app.domain import forecast as fc
from app.domain.alerts import fill_percent
from app.domain.status import derive_status
from app.repositories import telemetry as tel_repo
from app.repositories import terminals as term_repo
from app.repositories import vendor_alarms as alarm_repo
from app.services import notifier
from app.services.appconfig import ConfigLike, load_config

log = logging.getLogger(__name__)
router = APIRouter(prefix="/export", tags=["export"])
UTC = ZoneInfo("UTC")

#: Thư mục static, để nhúng logo vào báo cáo dưới dạng data URI.
_STATIC = Path(__file__).resolve().parents[2] / "static"

#: Tên file logo chữ (wordmark) theo thứ tự ưu tiên. Nhiều đuôi vì file do người
#: dùng đặt vào; thiếu cả bốn thì báo cáo tự lùi về chữ, KHÔNG hiện ảnh vỡ — một
#: khung ảnh lỗi trên tờ báo cáo có chữ ký còn tệ hơn không có logo.
_LOGO_NAMES = ("logo-nlx.png", "logo-nlx.svg", "logo-nlx.webp", "logo-nlx.jpg")
_MIME = {
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
}

#: Trần kích thước logo nhúng. Vượt ngưỡng thì bỏ qua: nhúng một ảnh 5 MB vào
#: mỗi lần xuất báo cáo là cách tự tạo ra lỗi hết thời gian hàm serverless.
MAX_LOGO_BYTES = 512 * 1024

DEFAULT_WINDOW_DAYS = 30


@lru_cache(maxsize=1)
def _logo_data_uri() -> str | None:
    """Đọc logo một lần cho cả tiến trình, trả về data URI hoặc None.

    Nhúng thẳng vào HTML thay vì trỏ ``/ui/logo-nlx.png``: người ta sẽ lưu tờ báo
    cáo này thành file và gửi đi. Một file HTML trỏ tới máy chủ nội bộ sẽ mất logo
    ngay khi ra khỏi mạng công ty.
    """
    for name in _LOGO_NAMES:
        p = _STATIC / name
        try:
            if not p.is_file():
                continue
            raw = p.read_bytes()
        except OSError as exc:  # quyền file, đĩa lỗi — không được làm sập báo cáo
            log.warning("report: không đọc được logo %s: %s", name, exc)
            continue
        if len(raw) > MAX_LOGO_BYTES:
            log.warning("report: logo %s quá lớn (%d byte), bỏ qua", name, len(raw))
            continue
        mime = _MIME.get(p.suffix.lower(), "application/octet-stream")
        return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
    return None


def _e(v: Any) -> str:
    """Escape mọi giá trị đi vào HTML. Tên bồn do người dùng đặt, không tin được."""
    return html.escape("" if v is None else str(v), quote=True)


def _dt(v: datetime | None, cfg: ConfigLike, *, with_time: bool = True) -> str:
    if v is None:
        return "—"
    local = v.astimezone(cfg.tzinfo)
    return local.strftime("%d/%m/%Y %H:%M" if with_time else "%d/%m/%Y")


def _n(v: float | None, nd: int = 2) -> str:
    """Số ra chuỗi kiểu Việt Nam: dấu phẩy thập phân, không nhóm hàng nghìn.

    Không nhóm hàng nghìn là có chủ ý: mọi thể tích trong báo cáo tính bằng m³ nên
    đều dưới 1000, và một dấu chấm phân nhóm cạnh một dấu phẩy thập phân trên cùng
    một trang là cách chắc nhất để ai đó đọc 10,425 m³ thành 10425 m³.
    """
    if v is None:
        return "—"
    return f"{v:.{nd}f}".replace(".", ",")


def _m3(litres: float | None, nd: int = 3) -> str:
    return "—" if litres is None else _n(litres / 1000.0, nd)


def _pct(v: float | None, nd: int = 1) -> str:
    return "—" if v is None else _n(v, nd) + "%"


def _cell(v: str, cls: str = "") -> str:
    return f'<td class="{cls}">{v}</td>' if cls else f"<td>{v}</td>"


def _row(cells: list[str]) -> str:
    return "<tr>" + "".join(cells) + "</tr>"


def _table(headers: list[tuple[str, str]], rows: list[str], *, empty: str) -> str:
    """Bảng có thead lặp lại mỗi trang in. Bảng rỗng ra một câu, không ra bảng rỗng."""
    if not rows:
        return f'<p class="empty">{_e(empty)}</p>'
    # scope="col" là bắt buộc, không phải trang trí: không có nó thì trình đọc màn
    # hình đọc từng ô số mà không nói ô đó thuộc cột nào, và một bảng 11 cột trở
    # thành một dãy số vô nghĩa.
    head = "".join(
        f'<th scope="col" class="{c}">{_e(t)}</th>'
        if c
        else f'<th scope="col">{_e(t)}</th>'
        for t, c in headers
    )
    return (
        '<div class="tw"><table><thead><tr>'
        + head
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


# ---------------------------------------------------------------- CSS bản in
#: Bảng màu CỐ ĐỊNH sáng, KHÔNG theo theme của người dùng. Báo cáo này đi ra giấy
#: và ra PDF gửi cho người khác: in một trang nền tối là tốn mực và sai chỗ, còn
#: một file PDF đổi màu theo cài đặt máy của người xuất là không kiểm tra được.
_CSS = """
:root{--ink:#0f172a;--soft:#475569;--faint:#64748b;--line:#d7dee8;--line2:#eef2f7;
--bg:#ffffff;--band:#f5f8fc;--blue:#1257a8;--green:#00994d;
--ok:#0f7a3d;--warn:#9a5b00;--crit:#b3261e;--okbg:#e8f6ed;--warnbg:#fdf3e0;--critbg:#fdecea}
*{box-sizing:border-box}
body{margin:0;background:#eef2f7;color:var(--ink);
font:13px/1.55 "Segoe UI",system-ui,-apple-system,"Helvetica Neue",Arial,sans-serif;
font-variant-numeric:tabular-nums;-webkit-print-color-adjust:exact;print-color-adjust:exact}
/* Bề rộng trên màn hình đặt bằng ĐÚNG vùng in của A4 (210mm trừ hai lề 11mm):
   trang này là bản xem trước của tờ giấy, nên nếu màn hình rộng hơn giấy thì người
   ta duyệt xong một bố cục khác với bố cục sẽ in ra. */
.sheet{max-width:210mm;margin:14px auto;background:var(--bg);padding:16mm 11mm;
box-shadow:0 2px 18px rgba(15,23,42,.13)}
.bar{max-width:210mm;margin:14px auto 0;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.bar button{font:600 13px/1 inherit;padding:9px 15px;border-radius:7px;border:1px solid var(--line);
background:#fff;color:var(--ink);cursor:pointer}
.bar button.pri{background:var(--blue);border-color:var(--blue);color:#fff}
.bar .tip{color:var(--faint);font-size:12px}

.lh{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;
border-bottom:2.5px solid var(--blue);padding-bottom:11px}
.lh .mark img{max-height:44px;max-width:74mm;width:auto;height:auto;display:block}
.lh .mark .txt{font-weight:800;font-size:19px;letter-spacing:-.01em}
.lh .mark .txt .g{color:var(--blue)}.lh .mark .txt .n{color:var(--green)}
.lh .mark .tag{color:var(--faint);font-size:11px;margin-top:3px}
.lh .id{text-align:right;font-size:11.5px;color:var(--soft);white-space:nowrap}
.lh .id b{color:var(--ink);font-size:12.5px}

h1{font-size:19px;margin:15px 0 2px;letter-spacing:-.02em;text-transform:uppercase}
.sub{color:var(--soft);font-size:12.5px;margin:0 0 4px}
.anchor{color:var(--warn);background:var(--warnbg);border-left:3px solid var(--warn);
padding:7px 9px;font-size:11.5px;margin:9px 0 0;border-radius:3px}

.sec{margin-top:17px}
.sec>h2{font-size:13px;margin:0 0 7px;padding-bottom:4px;border-bottom:1px solid var(--line);
text-transform:uppercase;letter-spacing:.03em;color:var(--blue)}
.sec>h2 .num{display:inline-block;min-width:19px;color:var(--faint)}

.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;break-inside:avoid}
.kpi{border:1px solid var(--line);border-radius:5px;padding:8px 10px;background:var(--band)}
.kpi .k{color:var(--faint);font-size:10.5px;text-transform:uppercase;letter-spacing:.04em}
.kpi .v{font-size:17px;font-weight:700;margin-top:1px;letter-spacing:-.02em}
.kpi .u{font-size:11px;font-weight:600;color:var(--soft);margin-left:2px}
.kpi.warn{background:var(--warnbg);border-color:#e6cd9c}
.kpi.crit{background:var(--critbg);border-color:#f0c2bd}

.tw{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:10.5px}
thead{display:table-header-group}
th,td{border:1px solid var(--line);padding:4.5px 6px;text-align:left;vertical-align:top}
th{background:var(--band);font-weight:700;font-size:10.5px;text-transform:uppercase;
letter-spacing:.02em;color:var(--soft)}
tbody tr{break-inside:avoid}
tbody tr:nth-child(even) td{background:#fafcfe}
/* nowrap CHỈ cho ô dữ liệu. Để nowrap trên <th> thì tiêu đề dài ép bảng rộng hơn
   trang giấy, và khi in ra A4 các cột cuối bị cắt mất — trên màn hình chỉ là một
   thanh cuộn, trên giấy là mất dữ liệu. */
td.n,th.n{text-align:right}
td.c,th.c{text-align:center}
td.n,td.c{white-space:nowrap}
td.psn{font-family:ui-monospace,"Cascadia Mono",Consolas,monospace;white-space:nowrap}
.pill{display:inline-block;padding:1px 6px;border-radius:9px;font-size:10px;font-weight:700;
border:1px solid transparent}
.pill.ok{background:var(--okbg);color:var(--ok);border-color:#b3ddc3}
.pill.warn{background:var(--warnbg);color:var(--warn);border-color:#e6cd9c}
.pill.crit{background:var(--critbg);color:var(--crit);border-color:#f0c2bd}
.pill.mute{background:#eef2f7;color:var(--faint);border-color:var(--line)}
.empty{color:var(--faint);font-size:12px;font-style:italic;margin:3px 0 0;
padding:8px 10px;background:var(--band);border:1px dashed var(--line);border-radius:4px}
.note{color:var(--soft);font-size:11px;margin:6px 0 0}

.meth{font-size:11px;color:var(--soft);columns:2;column-gap:16px}
.meth p{margin:0 0 5px;break-inside:avoid}
.meth b{color:var(--ink)}

.sign{margin-top:20px;display:grid;grid-template-columns:repeat(3,1fr);gap:12px;
break-inside:avoid;text-align:center;font-size:11.5px}
.sign .box{border-top:1px solid var(--ink);padding-top:5px;margin-top:66px;color:var(--soft)}
.sign .role{font-weight:700;color:var(--ink);text-transform:uppercase;font-size:10.5px;
letter-spacing:.04em}
.sign .who{margin-top:2px}
.foot{margin-top:16px;border-top:1px solid var(--line2);padding-top:7px;
color:var(--faint);font-size:10.5px;display:flex;justify-content:space-between;gap:12px}

@media print{
  @page{size:A4;margin:12mm 11mm}
  body{background:#fff}
  .bar{display:none}
  .sheet{max-width:none;margin:0;padding:0;box-shadow:none}
  /* Trên giấy KHÔNG có thanh cuộn: một bảng rộng hơn khổ giấy bị CẮT MẤT cột,
     không phải cuộn được. Nên bản in dùng cỡ chữ nhỏ hơn để bảng 11 cột vừa bề
     rộng 188mm, và bỏ khung cuộn đi để không có gì bị khung cắt âm thầm. */
  table{font-size:10px}
  th,td{padding:3.5px 4px}
  .tw{overflow:visible}
}
@media (max-width:760px){
  .kpis{grid-template-columns:repeat(2,1fr)}
  .sign{grid-template-columns:1fr}
  .sign .box{margin-top:46px}
  .meth{columns:1}
  .sheet{padding:12px}
}
"""


def _brand_block() -> str:
    """Logo chữ nếu có file, ngược lại lùi về chữ đúng màu thương hiệu."""
    uri = _logo_data_uri()
    if uri:
        return f'<img src="{uri}" alt="GAS Nhiên Liệu Xanh" />'
    return '<div class="txt"><span class="g">GAS</span> <span class="n">Nhiên Liệu Xanh</span></div>'


def _sev_pill(sev: str) -> str:
    cls = {"critical": "crit", "warning": "warn", "info": "mute"}.get(sev, "mute")
    label = {
        "critical": "NGHIÊM TRỌNG",
        "warning": "CẢNH BÁO",
        "info": "THÔNG TIN",
    }.get(sev, sev)
    return f'<span class="pill {cls}">{_e(label)}</span>'


def _grade_pill(grade: str) -> str:
    """Hạng dữ liệu: "cao" là TỐT.

    Cố ý tách khỏi ``_risk_pill``: cùng chữ "cao" nhưng hạng dữ liệu cao là tin tốt
    còn rủi ro cao là tin xấu. Dùng chung một bảng tra sẽ tô một trong hai sai màu,
    và trên báo cáo thì tô sai màu là nói sai kết luận.
    """
    cls = {"cao": "ok", "trung bình": "warn", "thấp": "crit", "không dùng được": "crit"}
    return f'<span class="pill {cls.get(grade, "mute")}">{_e(grade)}</span>'


def _risk_pill(risk: str) -> str:
    cls = {"thấp": "ok", "trung bình": "warn", "cao": "crit"}.get(risk, "mute")
    return f'<span class="pill {cls}">{_e(risk)}</span>'


def _boiloff_cell(percent_per_day: float | None, method: str) -> str:
    """Bay hơi, kèm nhãn khi con số là THAM CHIẾU chứ không phải đo được.

    Không có khoảng bồn nghỉ thì domain trả về một giá trị tham chiếu của ngành.
    In con số đó trơ trọi cạnh các số đo thật là biến một hằng số tra bảng thành
    một phép đo — chính xác điều mà nhãn ``method`` tồn tại để ngăn.
    """
    if percent_per_day is None:
        return "—"
    txt = _n(percent_per_day, 3)
    if method == "measured":
        return txt
    label = "tham chiếu" if method == "reference" else "chưa đủ dữ liệu"
    return f'{txt} <span class="pill mute">{_e(label)}</span>'


#: Nhãn mức gấp. Dùng ĐÚNG bốn giá trị của ``forecast.Urgency`` và đúng lời của
#: dashboard (``URG_VI`` trong index.html): báo cáo in ra và màn hình phải gọi cùng
#: một trạng thái bằng cùng một từ, nếu không hai bên sẽ bị đọc như hai kết luận.
_URG_LABEL = {
    "now": "Cần đặt ngay",
    "soon": "Sắp phải đặt",
    "ok": "Chưa cần đặt",
    "unknown": "Chưa đủ dữ liệu",
}
_URG_CLS = {"now": "crit", "soon": "warn", "ok": "ok", "unknown": "mute"}


def _urg_pill(urg: str) -> str:
    cls = _URG_CLS.get(urg, "mute")
    return f'<span class="pill {cls}">{_e(_URG_LABEL.get(urg, urg))}</span>'


@router.get("/report.html", response_class=HTMLResponse)
def export_report(
    session: SessionDep,
    settings: SettingsDep,
    user: UserDep,
    psn: Annotated[str | None, Query(max_length=32)] = None,
    window_days: Annotated[int, Query(ge=1, le=365)] = DEFAULT_WINDOW_DAYS,
) -> HTMLResponse:
    """Báo cáo giám sát bồn LNG, in được ra A4 hoặc lưu thành PDF.

    Không có ``psn`` thì báo cáo toàn bộ bồn. Kỳ báo cáo kết ở **lần đo cuối** của
    tập bồn được chọn, không phải ở thời điểm bấm nút — xem docstring module.
    """
    cfg = load_config(session, settings)
    now = datetime.now(tz=UTC)

    if psn:
        term = term_repo.get_by_psn(session, psn)
        if term is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "PSN không tồn tại")
        terms = [term]
    else:
        terms = term_repo.list_all(session)

    latest = tel_repo.latest_many(session, [t.psn for t in terms])

    # Neo kỳ báo cáo vào lần đo cuối. Chỉ khi chưa bồn nào từng gửi số liệu mới lùi
    # về "hôm nay" — lúc đó tờ báo cáo trống là sự thật, không phải lỗi mặc định.
    seen = [t.last_seen_at for t in terms if t.last_seen_at is not None]
    period_end = max(seen) if seen else now
    anchored = bool(seen) and (now - period_end) > timedelta(days=1)
    period_start = period_end - timedelta(days=window_days)

    stale_after = timedelta(minutes=cfg.online_stale_minutes)

    rows_state: list[str] = []
    rows_fc: list[str] = []
    rows_health: list[str] = []
    refills: list[tuple[datetime, str]] = []
    tot_cap = tot_vol = 0.0
    n_cap = n_vol = n_offline = n_urgent = n_stale = 0

    for t in terms:
        lt = latest.get(t.psn)
        cap = None if t.capacity_l is None else float(t.capacity_l)
        vol = None if (lt is None or lt.volume_l is None) else float(lt.volume_l)
        pres = (
            None if (lt is None or lt.pressure_mpa is None) else float(lt.pressure_mpa)
        )
        st = derive_status(t.last_seen_at, now, stale_after).value
        if st != "online":
            n_offline += 1
        if cap is not None:
            tot_cap += cap
            n_cap += 1
        if vol is not None:
            tot_vol += vol
            n_vol += 1

        rows = tel_repo.series(
            session, t.psn, period_start, period_end, bucket_minutes=30
        )
        samples = [fc.Sample(at=at, volume_l=v, pressure_mpa=p) for at, v, p in rows]
        f = fc.build_forecast(
            samples,
            psn=t.psn,
            volume_l=vol,
            capacity_l=cap,
            pressure_mpa=pres,
            now=now,
            tz=cfg.tzinfo,
            reserve_percent=cfg.forecast_reserve_percent,
            lead_time_days=cfg.forecast_lead_time_days,
            service_level=cfg.forecast_service_level,
            relief_mpa=cfg.lng_relief_pressure_mpa,
            max_fill_percent=cfg.lng_max_fill_percent,
            reading_at=lt.sampled_at if lt else None,
            max_reading_age_days=cfg.forecast_max_reading_age_hours / 24.0,
        )
        if f.stale:
            n_stale += 1
        if f.suggestion.urgency == "now":
            n_urgent += 1

        fp = fill_percent(lt.volume_l if lt else None, t.capacity_l)
        fp_f = None if fp is None else float(fp)
        if fp_f is None:
            fill_cls = "mute"
        elif fp_f < 15:
            fill_cls = "crit"
        elif fp_f < 30:
            fill_cls = "warn"
        else:
            fill_cls = "ok"

        # --- Mục 2: hiện trạng
        temp = None if (lt is None or lt.temperature_c is None) else float(lt.temperature_c)
        rows_state.append(
            _row([
                _cell(_e(t.psn), "psn"),
                _cell(_e(t.name or "—")),
                _cell(
                    f'<span class="pill {"ok" if st == "online" else "crit"}">'
                    f'{"Trực tuyến" if st == "online" else "Mất liên lạc"}</span>',
                    "c",
                ),
                _cell(_dt(t.last_seen_at, cfg), "c"),
                _cell(_m3(cap), "n"),
                _cell(_m3(vol), "n"),
                _cell(
                    "—"
                    if fp_f is None
                    else f'<span class="pill {fill_cls}">{_pct(fp_f, 2)}</span>',
                    "c",
                ),
                _cell(_n(pres, 3), "n"),
                _cell(_n(temp, 1), "n"),
            ])
        )

        # --- Mục 3: tiêu thụ & dự báo. Tuổi số liệu đứng TRƯỚC các cột dự báo.
        rows_fc.append(
            _row([
                _cell(_e(t.psn), "psn"),
                _cell(_m3(f.consumption.daily_use_l), "n"),
                _cell(_e(f.consumption.confidence), "c"),
                _cell(
                    _boiloff_cell(f.idle.boil_off_percent_per_day, f.idle.method), "n"
                ),
                _cell(
                    _n(f.reading_age_days, 1)
                    + (' <span class="pill crit">CŨ</span>' if f.stale else ""),
                    "c",
                ),
                _cell(_n(f.runout.days_to_reserve, 1), "n"),
                _cell(_n(f.runout.days_to_empty, 1), "n"),
                _cell(_n(f.hold.days, 1), "n"),
                _cell(_m3(f.suggestion.order_l), "n"),
                _cell(_dt(f.suggestion.order_at, cfg, with_time=False), "c"),
                _cell(_urg_pill(f.suggestion.urgency), "c"),
            ])
        )

        # --- Mục 4: chất lượng dữ liệu & sức khoẻ thiết bị
        hs = [
            an.HealthSample(at=at, battery_v=b, signal_percent=s)
            for at, b, s in tel_repo.health_series(
                session, t.psn, period_start, period_end
            )
        ]
        # Hai mốc thời gian KHÁC nhau, có chủ ý:
        # - Chất lượng dữ liệu đo trong cửa sổ [period_start, period_end], nên mốc
        #   là period_end — độ phủ phải tính trên đúng kỳ báo cáo.
        # - Sức khoẻ thiết bị đo theo HIỆN TẠI. Nếu cũng lấy period_end thì "đã im
        #   bao nhiêu ngày" luôn ra 0, và một thiết bị chết 80 ngày sẽ được báo cáo
        #   là bình thường — đúng loại sai sót tệ nhất trên tờ giấy có chữ ký.
        q = an.assess_quality(samples, now=period_end, window_days=float(window_days))
        h = an.assess_device_health(
            hs,
            psn=t.psn,
            now=now,
            warn_v=float(cfg.alert_low_battery_v),
            floor_percent=float(cfg.alert_low_signal_percent),
            quality_grade=q.grade,
        )
        rows_health.append(
            _row([
                _cell(_e(t.psn), "psn"),
                _cell(_grade_pill(q.grade), "c"),
                # expected_samples = 0 nghĩa là CHƯA SUY ĐƯỢC nhịp đo (dưới hai lần
                # đo thì không có khoảng nào để suy), không phải "kỳ vọng bằng 0".
                # In thẳng số 0 ra giấy thành "1 / 0" — một tỉ lệ vô nghĩa mà người
                # đọc buộc phải tự đoán nghĩa.
                _cell(
                    f"{q.samples} / "
                    + (str(q.expected_samples) if q.expected_samples else "—"),
                    "n",
                ),
                _cell(_pct(q.coverage * 100, 0), "n"),
                _cell(_n(q.longest_gap_hours, 1), "n"),
                _cell(_n(h.battery.current_v, 2), "n"),
                _cell(_n(h.battery.volts_per_day, 4), "n"),
                _cell(_n(h.signal.current_percent, 0), "n"),
                _cell(_risk_pill(h.risk), "c"),
                _cell(_e(h.likely_cause or "—")),
            ])
        )

        # --- Mục 5: nhật ký nạp. Giữ cặp (thời điểm, hàng) để sắp theo THỜI GIAN
        # thật, không sắp theo chuỗi HTML — chuỗi HTML mở đầu bằng thẻ nên sắp nó
        # cho ra thứ tự vô nghĩa.
        for e in fc.detect_refills(samples, capacity_l=cap):
            refills.append((
                e.at,
                _row([
                    _cell(_dt(e.at, cfg), "c"),
                    _cell(_e(t.psn), "psn"),
                    _cell(_e(t.name or "—")),
                    _cell(_m3(e.before_l), "n"),
                    _cell(_m3(e.after_l), "n"),
                    _cell("<b>" + _m3(e.amount_l) + "</b>", "n"),
                ]),
            ))

    refills.sort(key=lambda r: r[0], reverse=True)
    rows_refill = [h for _, h in refills]

    # --- Mục 6: cảnh báo, dùng đúng hàm mà email cảnh báo dùng.
    try:
        notices = notifier.collect_notices(session, cfg, now)
    except Exception as exc:  # báo cáo vẫn phải in ra được nếu phần này lỗi
        log.warning("report: không gom được cảnh báo: %s", exc)
        notices = []
    keep = {t.psn for t in terms}
    notices = [n for n in notices if n.psn in keep]
    notices.sort(key=lambda n: (notifier.severity_rank(n.severity), n.psn))
    rows_alert = [
        _row([
            _cell(_sev_pill(n.severity), "c"),
            _cell(_e(n.psn), "psn"),
            _cell(_e(n.name or "—")),
            _cell(_e(n.code), "c"),
            _cell(_e(n.message)),
        ])
        for n in notices
    ]
    n_crit = sum(1 for n in notices if n.severity == "critical")

    avg_fill = (tot_vol / tot_cap * 100.0) if (n_cap and tot_cap > 0) else None
    code = (
        f"BC-LNG-{period_end.astimezone(cfg.tzinfo):%Y%m%d}"
        f"-{now.astimezone(cfg.tzinfo):%H%M}"
    )
    scope = f"Bồn {psn}" if psn else f"Toàn bộ {len(terms)} bồn"

    kpis = [
        ("Số bồn theo dõi", str(len(terms)), "", ""),
        ("Mất liên lạc", str(n_offline), f"/ {len(terms)}", "crit" if n_offline else ""),
        ("Tổng dung tích", _m3(tot_cap if n_cap else None, 2), "m³", ""),
        ("Tổng tồn hiện tại", _m3(tot_vol if n_vol else None, 2), "m³", ""),
        (
            "Tỷ lệ đầy bình quân",
            _pct(avg_fill, 1),
            "",
            "crit" if (avg_fill is not None and avg_fill < 15) else "",
        ),
        ("Cảnh báo nghiêm trọng", str(n_crit), "", "crit" if n_crit else ""),
        ("Bồn cần đặt gấp", str(n_urgent), "", "warn" if n_urgent else ""),
        ("Bồn có số liệu cũ", str(n_stale), f"/ {len(terms)}", "warn" if n_stale else ""),
    ]
    kpi_html = "".join(
        f'<div class="kpi {cls}"><div class="k">{_e(k)}</div>'
        f'<div class="v">{v}{f"<span class=u>{_e(u)}</span>" if u else ""}</div></div>'
        for k, v, u, cls in kpis
    )

    anchor_note = ""
    if anchored:
        age = (now - period_end).total_seconds() / 86400.0
        anchor_note = (
            '<p class="anchor"><b>Kỳ báo cáo kết ở lần đo cuối của hệ thống '
            f"({_dt(period_end, cfg)}), không phải thời điểm xuất báo cáo.</b> "
            f"Thiết bị đã không gửi số liệu {_n(age, 1)} ngày. "
            "Lấy kỳ kết ở hôm nay sẽ cho ra một báo cáo trống.</p>"
        )

    tbl_state = _table(
        [
            ("PSN", ""),
            ("Tên bồn", ""),
            ("Trạng thái", "c"),
            ("Lần đo cuối", "c"),
            ("Dung tích (m³)", "n"),
            ("Thể tích (m³)", "n"),
            ("Tỷ lệ đầy", "c"),
            ("Áp suất (MPa)", "n"),
            ("Nhiệt độ (°C)", "n"),
        ],
        rows_state,
        empty="Chưa có bồn nào trong hệ thống.",
    )
    tbl_fc = _table(
        [
            ("PSN", ""),
            ("Tiêu thụ/ngày (m³)", "n"),
            ("Độ tin cậy", "c"),
            # Không có cột "Độ phủ" ở đây: nó trùng đúng nghĩa với cột Độ phủ ở
            # Mục 4, và 12 cột không vừa khổ A4 dọc nên cột cuối bị cắt khi in.
            # Bỏ cột trùng là cách đúng, thu nhỏ chữ tới mức không đọc được thì không.
            ("Bay hơi (%/ngày)", "n"),
            ("Tuổi số liệu (ngày)", "c"),
            ("Tới dự trữ (ngày)", "n"),
            ("Tới cạn (ngày)", "n"),
            ("Giữ áp (ngày)", "n"),
            ("Đề xuất đặt (m³)", "n"),
            ("Đặt trước ngày", "c"),
            ("Mức gấp", "c"),
        ],
        rows_fc,
        empty="Không có bồn nào để dự báo.",
    )
    tbl_health = _table(
        [
            ("PSN", ""),
            ("Hạng dữ liệu", "c"),
            ("Lần đo / kỳ vọng", "n"),
            ("Độ phủ", "n"),
            ("Trống dài nhất (giờ)", "n"),
            ("Pin (V)", "n"),
            ("Suy pin (V/ngày)", "n"),
            ("Sóng (%)", "n"),
            ("Rủi ro thiết bị", "c"),
            ("Nguyên nhân", ""),
        ],
        rows_health,
        empty="Không có dữ liệu để đánh giá.",
    )
    tbl_refill = _table(
        [
            ("Thời điểm", "c"),
            ("PSN", ""),
            ("Tên bồn", ""),
            ("Trước nạp (m³)", "n"),
            ("Sau nạp (m³)", "n"),
            ("Lượng nạp (m³)", "n"),
        ],
        rows_refill,
        empty="Không ghi nhận lần nạp nào trong kỳ báo cáo.",
    )
    tbl_alert = _table(
        [("Mức", "c"), ("PSN", ""), ("Tên bồn", ""), ("Mã", "c"), ("Nội dung", "")],
        rows_alert,
        empty="Không có cảnh báo nào đang mở.",
    )

    # Báo động do NGUỒN phát. Mục này tồn tại vì soát ngày 2026-09-04: báo cáo
    # trình ký nói "Không có cảnh báo nào đang mở" trong khi nhà máy đang báo động
    # 5 thiết bị (PS1, PS2, SV2, SV3, SV4) với 289 dòng thô. Đây là tài liệu đem
    # đi ký, nên một khoảng trống ở đây là một lời nói sai có chữ ký bên dưới.
    #
    # Dùng lại ĐÚNG ``summarize`` mà dashboard dùng, nên báo cáo không thể nói
    # khác màn hình. Lỗi thì ra bảng rỗng kèm lý do, KHÔNG làm chết cả báo cáo.
    va_note = ""
    rows_va: list[str] = []
    va_raw = 0
    try:
        # ``va_ep`` chứ không phải ``e``: scope hàm là phẳng nên trùng tên với
        # vòng lặp RefillEvent phía trên làm mypy suy sai kiểu cho cả hai.
        va_eps, va_raw = alarm_repo.summarize(
            session, start=period_start, end=period_end
        )
        for va_ep in va_eps:
            rows_va.append(
                "<tr>"
                f"<td>{_e(va_ep.device_id)}</td>"
                f"<td>{_e(va_ep.message)}</td>"
                f'<td class="n">{va_ep.count}</td>'
                f'<td class="c">{_dt(va_ep.first_raised_at, cfg)}</td>'
                f'<td class="c">{_dt(va_ep.last_raised_at, cfg)}</td>'
                "</tr>"
            )
    except Exception as exc:  # pragma: no cover - đường phòng vệ
        log.warning("report: không đọc được báo động nguồn: %s", exc)
        va_note = "Không đọc được báo động của nguồn trong kỳ này."
    tbl_va = _table(
        [
            ("Thiết bị", ""),
            ("Việc", ""),
            ("Số lần", "n"),
            ("Lần đầu", "c"),
            ("Lần cuối", "c"),
        ],
        rows_va,
        empty=va_note or "Nguồn không phát báo động nào trong kỳ báo cáo.",
    )
    va_sum = (
        f"{va_raw} dòng thô gộp thành {len(rows_va)} việc."
        if rows_va
        else ""
    )

    body = f"""
<div class="bar">
  <button type="button" class="pri" onclick="window.print()">In / Lưu thành PDF</button>
  <button type="button" onclick="window.close()">Đóng</button>
  <span class="tip">Trong hộp thoại in, chọn máy in là <b>Save as PDF</b> để có tệp gửi đi.</span>
</div>
<div class="sheet">
  <div class="lh">
    <div class="mark">{_brand_block()}
      <div class="tag">Hệ thống giám sát bồn LNG nội bộ</div></div>
    <div class="id">
      <div><b>{_e(code)}</b></div>
      <div>Xuất lúc {_dt(now, cfg)}</div>
      <div>Người lập: {_e(user)}</div>
    </div>
  </div>

  <h1>Báo cáo giám sát bồn LNG</h1>
  <p class="sub">
    Phạm vi: <b>{_e(scope)}</b> &nbsp;·&nbsp;
    Kỳ báo cáo: <b>{_dt(period_start, cfg, with_time=False)} – {_dt(period_end, cfg, with_time=False)}</b>
    ({window_days} ngày) &nbsp;·&nbsp; Múi giờ {_e(cfg.app_tz)}
  </p>
  {anchor_note}

  <div class="sec">
    <h2><span class="num">1.</span>Tóm tắt điều hành</h2>
    <div class="kpis">{kpi_html}</div>
  </div>

  <div class="sec">
    <h2><span class="num">2.</span>Hiện trạng từng bồn</h2>
    {tbl_state}
  </div>

  <div class="sec">
    <h2><span class="num">3.</span>Tiêu thụ và dự báo</h2>
    {tbl_fc}
    <p class="note">Cột <b>Tuổi số liệu</b> đứng trước các cột dự báo có chủ ý: mọi con số
    bên phải nó đều suy từ lần đo cuối, nên tuổi số liệu quyết định chúng còn dùng được
    hay không. Nhãn <b>CŨ</b> nghĩa là quá ngưỡng
    {_n(float(cfg.forecast_max_reading_age_hours), 0)} giờ và dự báo chỉ mang tính tham khảo.</p>
  </div>

  <div class="sec">
    <h2><span class="num">4.</span>Chất lượng dữ liệu và sức khoẻ thiết bị</h2>
    {tbl_health}
  </div>

  <div class="sec">
    <h2><span class="num">5.</span>Nhật ký nạp trong kỳ</h2>
    {tbl_refill}
  </div>

  <div class="sec">
    <h2><span class="num">6.</span>Cảnh báo đang mở</h2>
    {tbl_alert}
    <p class="note">Danh sách này do cùng một hàm sinh ra với email cảnh báo, nên báo cáo
    in ra không thể nói khác thư đã gửi.</p>
  </div>

  <div class="sec">
    <h2><span class="num">7.</span>Báo động của nhà máy</h2>
    {tbl_va}
    <p class="note">Đây là báo động do <b>chính thiết bị tại nhà máy</b> phát, khác mục 6
    (cảnh báo hệ thống tự suy từ mức chứa, dự báo cạn, pin và sóng). Nguồn phát lại cùng
    một dòng mỗi lần quét trong khi điều kiện còn đúng, nên các dòng giống nhau đã được
    gộp thành một việc kèm số lần và khoảng thời gian. {_e(va_sum)} Nguồn gắn cùng một
    mức nguy hiểm cho mọi dòng nên hệ thống <b>không</b> tự xếp mức nặng nhẹ ở đây.</p>
  </div>

  <div class="sec">
    <h2><span class="num">8.</span>Ghi chú phương pháp</h2>
    <div class="meth">
      <p><b>Dấu phẩy là dấu thập phân.</b> Không nhóm hàng nghìn; mọi thể tích tính bằng m³.</p>
      <p><b>Ô ghi “—”</b> nghĩa là thiết bị không gửi giá trị đó, KHÔNG phải bằng 0.</p>
      <p><b>Tiêu thụ/ngày</b> đo từ chuỗi mức thực tế, đã loại các lần nạp; ước lượng bền
      với số đo nhiễu nên một điểm rác không kéo lệch kết quả.</p>
      <p><b>Độ phủ</b> là số lần đo thực tế chia số lần đo kỳ vọng của <i>cả kỳ báo cáo</i>,
      không chia cho khoảng có dữ liệu — nếu chia cách sau, một thiết bị chết 25 trên 30
      ngày vẫn báo độ phủ 100%.</p>
      <p><b>Bay hơi tự nhiên</b> lấy từ các khoảng bồn không xuất; đây là tổn thất vật lý
      của LNG, không phải sai số đo.</p>
      <p><b>Giữ áp</b> là số ngày tới khi áp suất chạm van an toàn
      {_n(float(cfg.lng_relief_pressure_mpa), 2)} MPa nếu bồn để yên.</p>
      <p><b>Đề xuất đặt</b> tính theo mức dự trữ
      {_n(float(cfg.forecast_reserve_percent), 0)}%, thời gian giao
      {_n(float(cfg.forecast_lead_time_days), 1)} ngày và mức đầy tối đa
      {_n(float(cfg.lng_max_fill_percent), 0)}%.</p>
      <p><b>Ngưỡng mất liên lạc</b>: không có số liệu quá
      {_n(float(cfg.online_stale_minutes), 0)} phút.</p>
    </div>
  </div>

  <div class="sign">
    <div><div class="box"><div class="role">Người lập</div>
      <div class="who">{_e(user)}</div></div></div>
    <div><div class="box"><div class="role">Người kiểm tra</div>
      <div class="who">&nbsp;</div></div></div>
    <div><div class="box"><div class="role">Người phê duyệt</div>
      <div class="who">&nbsp;</div></div></div>
  </div>

  <div class="foot">
    <span>{_e(code)} · Hệ thống giám sát bồn LNG nội bộ · Tài liệu nội bộ</span>
    <span>Xuất lúc {_dt(now, cfg)} ({_e(cfg.app_tz)})</span>
  </div>
</div>
"""

    doc = (
        '<!doctype html><html lang="vi"><head><meta charset="utf-8" />'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />'
        f"<title>{_e(code)} — Báo cáo giám sát bồn LNG</title>"
        f"<style>{_CSS}</style></head><body>{body}</body></html>"
    )
    # noindex: tài liệu nội bộ, không muốn bất kỳ trình thu thập nào lưu lại.
    return HTMLResponse(content=doc, headers={"X-Robots-Tag": "noindex, nofollow"})
