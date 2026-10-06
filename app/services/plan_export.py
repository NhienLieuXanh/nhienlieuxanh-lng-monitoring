"""Báo cáo Excel "Lịch nạp LNG": một file, ba sheet, in vừa A4.

Mẫu là bảng người vận hành gửi đối tác (ảnh 30/09/2026, sửa lại 06/10/2026): chỉ
các ngày xe tới, lượng tính bằng m³ (không phải tấn), cột khách hàng, ngày, ghi
chú — và ngày nghỉ rơi giữa hai lần giao thì ghi chú ở dòng giao TRƯỚC nó.

1. **Lịch nạp** — sheet chính: lần nạp THẬT đã qua (số đo) + lần nạp KẾ HOẠCH.
2. **Nhật ký nạp thật** — mỗi lần xe đã tới, kèm thể tích trước/sau.
3. **Tiêu thụ theo ngày** — đầu ngày, cuối ngày, nạp, tiêu thụ; kèm biểu đồ.

Ai tính gì. Lịch KẾ HOẠCH do trang tính (cùng bộ tính với trang Kế hoạch, cùng ô
tích và số đo tay người dùng đang thấy) và gửi lên; một bản tính thứ hai ở server
là cách chắc chắn nhất để file lệch với màn hình. Phần THẬT — lần nạp, thể tích
từng ngày, ngày nghỉ — server tự đọc từ dữ liệu, không tin trang gửi lên.

Mọi ô tổng là SỐ, không phải công thức. openpyxl không lưu sẵn kết quả công thức,
và Excel mở file tải về ở Protected View — chế độ không tính lại — nên ô SUM hiện
TRỐNG: dòng "Tổng cộng" trống trơn trên máy người dùng (06/10/2026).

Module này thuần: nhận dữ liệu đã chuẩn bị, trả bytes. Không đọc DB.
"""

from __future__ import annotations

import io
import struct
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell.cell import Cell
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.drawing.image import Image
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

WEEKDAY_VI = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]

#: Logo chữ của công ty — cùng file trang web dùng ở màn đăng nhập.
LOGO_PATH = Path(__file__).resolve().parents[1] / "static" / "logo-nlx.png"

# Bảng màu: xanh thương hiệu cho tiêu đề; xanh lá nhạt = đã xảy ra; vàng = tổng
# (giữ đúng thói quen của mẫu gốc); xám rất nhạt = sọc dòng; cam = ngày nghỉ.
_BRAND = "1F4E79"
_GREEN = "375623"
_ORANGE = "C55A11"
_GREY = "595959"
_HEAD_FILL = PatternFill("solid", fgColor=_BRAND)
_DONE_FILL = PatternFill("solid", fgColor="E2F0D9")
_ZEBRA_FILL = PatternFill("solid", fgColor="F5F8FC")
_TOTAL_FILL = PatternFill("solid", fgColor="FFF2CC")
_MUTED_FILL = PatternFill("solid", fgColor="F2F2F2")
_THIN = Side(style="thin", color="BFBFBF")
_EDGE = Side(style="medium", color=_BRAND)
_BOX = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_M3 = "#,##0.00"
_DATE = "dd/mm/yyyy"
_DT = "dd/mm/yyyy hh:mm"
#: Hàng tiêu đề bảng ở mọi sheet — cố định để người đọc (và test) không phải dò.
HEADER_ROW = 7


# --------------------------------------------------------------------------- #
# Dữ liệu vào
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PlannedRow:
    """Một lần nạp KẾ HOẠCH do trang tính."""

    day: date
    m3: float
    forced: bool = False


@dataclass(frozen=True)
class ActualRefill:
    """Một lần xe đã tới, suy từ số đo. ``at`` là giờ địa phương."""

    at: datetime
    before_m3: float
    after_m3: float

    @property
    def m3(self) -> float:
        return self.after_m3 - self.before_m3


@dataclass(frozen=True)
class DayRow:
    day: date
    open_m3: float | None
    close_m3: float | None
    refill_m3: float
    rest: bool = False
    note: str = ""

    @property
    def use_m3(self) -> float | None:
        if self.open_m3 is None or self.close_m3 is None:
            return None
        return self.open_m3 + self.refill_m3 - self.close_m3


@dataclass(frozen=True)
class ScheduleRow:
    day: date
    m3: float
    actual: bool
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReportMeta:
    tank_name: str
    psn: str
    customer: str | None
    from_day: date
    to_day: date
    generated_at: datetime
    basis: str | None = None


def vn_day(d: date) -> str:
    return f"{d:%d/%m} ({WEEKDAY_VI[d.weekday()]})"


# --------------------------------------------------------------------------- #
# Ghép lịch: thật + kế hoạch + ghi chú ngày nghỉ
# --------------------------------------------------------------------------- #


def build_schedule(
    planned: list[PlannedRow],
    actual: list[ActualRefill],
    rest_days: list[date],
    *,
    from_day: date,
    to_day: date,
    today: date,
) -> list[ScheduleRow]:
    """Các dòng của sheet Lịch nạp, theo ngày tăng dần.

    * Ngày đã qua chỉ có lần nạp THẬT. Một lần nạp kế hoạch rơi vào ngày đã có
      xe thật thì bỏ — xe đã tới, lịch mô phỏng cho ngày đó không còn nghĩa — và
      lần nạp kế hoạch trước hôm nay cũng bỏ: quá khứ là số đo, không phải dự kiến.
    * Ngày nghỉ (người dùng đánh dấu) ghi chú ở dòng giao TRƯỚC nó (người dùng
      chọn 06/10/2026): đọc dòng giao là biết chuyến sau phải qua một ngày nghỉ.
      Không có dòng nào trước nó thì ghi ở dòng đầu tiên, không được làm rơi mất.
    """
    rows: dict[date, ScheduleRow] = {}
    for a in sorted(actual, key=lambda x: x.at):
        d = a.at.date()
        if not from_day <= d <= to_day:
            continue
        note = f"Đã nạp thực tế lúc {a.at:%H:%M}"
        if d in rows:   # hai xe cùng ngày: gộp, giữ cả hai giờ
            prev = rows[d]
            rows[d] = ScheduleRow(d, prev.m3 + a.m3, True, [*prev.notes, note])
        else:
            rows[d] = ScheduleRow(d, a.m3, True, [note])
    for p in planned:
        if not from_day <= p.day <= to_day or p.day < today or p.day in rows or p.m3 <= 0:
            continue
        rows[p.day] = ScheduleRow(p.day, p.m3, False, ["Nạp chỉ định"] if p.forced else [])

    ordered = [rows[d] for d in sorted(rows)]
    for r in sorted(d for d in rest_days if from_day <= d <= to_day):
        before = [s for s in ordered if s.day <= r]
        target = before[-1] if before else (ordered[0] if ordered else None)
        if target is not None:
            target.notes.append(f"Nghỉ {vn_day(r)}")
    return ordered


# --------------------------------------------------------------------------- #
# Khung trình bày dùng chung
# --------------------------------------------------------------------------- #


class _PngImage(Image):
    """Ảnh PNG cho openpyxl mà KHÔNG cần Pillow.

    ``openpyxl.drawing.image.Image`` mở ảnh bằng Pillow chỉ để đọc kích thước —
    thêm một thư viện xử lý ảnh vào bản deploy cho đúng một việc đó là không
    đáng. PNG ghi kích thước ngay trong khối IHDR (byte 16–24).
    """

    def __init__(self, data: bytes, *, height_px: int) -> None:
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError("logo phải là PNG")
        w, h = struct.unpack(">II", data[16:24])
        self.ref = data
        self.format = "png"
        self.height = height_px
        self.width = round(w * height_px / h)

    def _data(self) -> bytes:
        return self.ref


def _logo(height_px: int = 46) -> _PngImage | None:
    try:
        return _PngImage(LOGO_PATH.read_bytes(), height_px=height_px)
    except (OSError, ValueError):
        return None   # thiếu logo không được làm hỏng báo cáo


def _text(cell: Cell, value: str | None) -> None:
    """Ghi CHUỖI, không bao giờ là công thức.

    openpyxl coi mọi chuỗi mở đầu bằng "=" là công thức. Tên khách hàng hay ghi chú
    là chữ người gõ vào, và một ghi chú "=HYPERLINK(...)" không được phép chạy trên
    máy của người nhận file.
    """
    cell.value = value or ""
    cell.data_type = "s"


def _band(ws: Worksheet, title: str, meta: ReportMeta, ncols: int, extra: str | None) -> None:
    """Đầu trang: logo trái, tên báo cáo + kỳ phải, đường kẻ thương hiệu, thông tin bồn.

    Hàng 1–2: logo | tên báo cáo / kỳ. Hàng 3: đường kẻ. Hàng 4: bồn · khách hàng.
    Hàng 5: dòng giải thích (nếu có). Hàng 6: khoảng trống. Hàng 7: tiêu đề bảng.
    """
    last = get_column_letter(ncols)
    right = get_column_letter(max(3, ncols - 2))
    ws.row_dimensions[1].height = 24
    ws.row_dimensions[2].height = 18
    ws.row_dimensions[3].height = 6
    logo = _logo()
    if logo is not None:
        ws.add_image(logo, "A1")

    ws.merge_cells(f"{right}1:{last}1")
    c = ws[f"{right}1"]
    c.value = title
    c.font = Font(bold=True, size=16, color=_BRAND)
    c.alignment = Alignment(horizontal="right", vertical="center")
    ws.merge_cells(f"{right}2:{last}2")
    c = ws[f"{right}2"]
    _text(c, f"Kỳ báo cáo: {meta.from_day:%d/%m/%Y} – {meta.to_day:%d/%m/%Y}")
    c.font = Font(size=10, color=_GREY)
    c.alignment = Alignment(horizontal="right", vertical="center")
    for col in range(1, ncols + 1):
        ws.cell(3, col).border = Border(bottom=_EDGE)

    ws.merge_cells(f"A4:{last}4")
    who = f"Bồn: {meta.tank_name}"
    if meta.customer:
        who += f"     ·     Khách hàng: {meta.customer}"
    _text(ws["A4"], who)
    ws["A4"].font = Font(bold=True, size=11, color="262626")
    ws["A4"].alignment = Alignment(vertical="center")
    ws.row_dimensions[4].height = 20
    if extra:
        ws.merge_cells(f"A5:{last}5")
        _text(ws["A5"], extra)
        ws["A5"].font = Font(italic=True, size=9, color=_GREY)
        ws["A5"].alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[5].height = 26


def _header(ws: Worksheet, labels: list[str], widths: list[float]) -> None:
    for i, (lbl, w) in enumerate(zip(labels, widths, strict=True), start=1):
        c = ws.cell(HEADER_ROW, i, lbl)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = _HEAD_FILL
        c.border = Border(left=_THIN, right=_THIN, top=_EDGE, bottom=_EDGE)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.row_dimensions[HEADER_ROW].height = 30
    ws.freeze_panes = ws.cell(HEADER_ROW + 1, 1)
    ws.print_title_rows = f"{HEADER_ROW}:{HEADER_ROW}"


def _print_setup(ws: Worksheet, meta: ReportMeta, landscape: bool = False) -> None:
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.orientation = "landscape" if landscape else "portrait"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins.left = ws.page_margins.right = 0.5
    ws.page_margins.top = 0.6
    ws.print_options.horizontalCentered = True
    ws.sheet_view.showGridLines = False
    ws.oddFooter.left.text = f"Xuất lúc {meta.generated_at:%d/%m/%Y %H:%M}"
    ws.oddFooter.left.size = 8
    ws.oddFooter.center.text = "Trang &P / &N"
    ws.oddFooter.center.size = 8


def _row(ws: Worksheet, row: int, ncols: int, fill: PatternFill | None) -> None:
    for col in range(1, ncols + 1):
        c = ws.cell(row, col)
        c.border = _BOX
        if fill is not None:
            c.fill = fill
        if not c.alignment.horizontal:
            c.alignment = Alignment(vertical="center")
    ws.row_dimensions[row].height = 18


def _total(ws: Worksheet, row: int, ncols: int, label_to: int, label: str,
           values: dict[int, object]) -> None:
    """Dòng tổng: nền vàng như mẫu gốc, chữ đậm, viền đậm. Giá trị là SỐ."""
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=label_to)
    ws.cell(row, 1, label).alignment = Alignment(horizontal="center", vertical="center")
    for col, v in values.items():
        c = ws.cell(row, col, v)
        if isinstance(v, float):
            c.number_format = _M3
            c.alignment = Alignment(vertical="center")
        else:
            c.alignment = Alignment(horizontal="center", vertical="center")
    for col in range(1, ncols + 1):
        c = ws.cell(row, col)
        c.font = Font(bold=True)
        c.fill = _TOTAL_FILL
        c.border = Border(left=_THIN, right=_THIN, top=_EDGE, bottom=_EDGE)
    ws.row_dimensions[row].height = 22


def _empty(ws: Worksheet, ncols: int, msg: str) -> int:
    r = HEADER_ROW + 1
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ncols)
    _text(ws.cell(r, 1), msg)
    ws.cell(r, 1).font = Font(italic=True, color=_GREY)
    ws.cell(r, 1).alignment = Alignment(horizontal="center")
    return r


# --------------------------------------------------------------------------- #
# Ba sheet
# --------------------------------------------------------------------------- #


def _sheet_schedule(ws: Worksheet, rows: list[ScheduleRow], meta: ReportMeta) -> None:
    ws.title = "Lịch nạp"
    labels = ["STT", "Khách hàng", "Ngày nạp", "Thứ", "Lượng (m³)", "Trạng thái", "Ghi chú"]
    n = len(labels)
    _band(ws, "LỊCH NẠP LNG", meta, n, meta.basis)
    _header(ws, labels, [6, 24, 13, 11, 13, 13, 44])

    customer = meta.customer or meta.tank_name
    r = HEADER_ROW
    planned_i = 0
    for i, s in enumerate(rows, start=1):
        r = HEADER_ROW + i
        ws.cell(r, 1, i).alignment = Alignment(horizontal="center", vertical="center")
        _text(ws.cell(r, 2), customer)
        c = ws.cell(r, 3, s.day)
        c.number_format = _DATE
        c.alignment = Alignment(horizontal="center", vertical="center")
        _text(ws.cell(r, 4), WEEKDAY_VI[s.day.weekday()])
        ws.cell(r, 5, round(s.m3, 2)).number_format = _M3
        st = ws.cell(r, 6)
        _text(st, "Đã nạp" if s.actual else "Kế hoạch")
        st.font = Font(bold=True, color=_GREEN if s.actual else _BRAND)
        st.alignment = Alignment(horizontal="center", vertical="center")
        note = ws.cell(r, 7)
        _text(note, "; ".join(s.notes))
        note.alignment = Alignment(wrap_text=True, vertical="center")
        if any(x.startswith("Nghỉ") for x in s.notes):
            note.font = Font(italic=True, color=_ORANGE)
        if s.actual:
            fill: PatternFill | None = _DONE_FILL
        else:
            fill = _ZEBRA_FILL if planned_i % 2 else None
            planned_i += 1
        _row(ws, r, n, fill)
    if not rows:
        r = _empty(ws, n, "Không có lần nạp nào trong kỳ.")

    _total(ws, r + 1, n, 4, "Tổng cộng",
           {5: round(sum(s.m3 for s in rows), 2), 6: f"{len(rows)} lần"})
    _print_setup(ws, meta)


def _sheet_refills(ws: Worksheet, refills: list[ActualRefill], meta: ReportMeta) -> None:
    ws.title = "Nhật ký nạp thật"
    labels = ["STT", "Thời điểm", "Thứ", "Trước khi nạp (m³)", "Sau khi nạp (m³)", "Lượng nạp (m³)"]
    n = len(labels)
    _band(ws, "NHẬT KÝ NẠP THẬT", meta, n,
          "Hệ thống tự nhận diện lần nạp từ bước tăng của thể tích đo được — không nhập tay.")
    _header(ws, labels, [6, 18, 11, 17, 17, 16])
    r = HEADER_ROW
    for i, a in enumerate(sorted(refills, key=lambda x: x.at), start=1):
        r = HEADER_ROW + i
        ws.cell(r, 1, i).alignment = Alignment(horizontal="center", vertical="center")
        c = ws.cell(r, 2, a.at.replace(tzinfo=None))
        c.number_format = _DT
        c.alignment = Alignment(horizontal="center", vertical="center")
        _text(ws.cell(r, 3), WEEKDAY_VI[a.at.weekday()])
        ws.cell(r, 4, round(a.before_m3, 2)).number_format = _M3
        ws.cell(r, 5, round(a.after_m3, 2)).number_format = _M3
        amt = ws.cell(r, 6, round(a.m3, 2))
        amt.number_format = _M3
        amt.font = Font(bold=True, color=_GREEN)
        _row(ws, r, n, _ZEBRA_FILL if i % 2 == 0 else None)
    if not refills:
        r = _empty(ws, n, "Không có lần nạp nào trong kỳ.")
    _total(ws, r + 1, n, 5, f"Tổng cộng · {len(refills)} lần",
           {6: round(sum(a.m3 for a in refills), 2)})
    _print_setup(ws, meta)


def _chart_style(chart: LineChart | BarChart, title: str, cats: Reference) -> None:
    """Một kiểu cho mọi biểu đồ: MỘT màu, có số trên trục, không chú giải thừa.

    openpyxl 3.1 mặc định ẩn cả hai trục (``delete=True``) và tô mỗi điểm một màu
    (``varyColors``): biểu đồ ra không có một con số nào và trông như cầu vồng
    (đo bằng Excel xuất PDF, 06/10/2026).
    """
    chart.title = title
    chart.title.overlay = False     # tiêu đề nằm TRÊN vùng vẽ, không đè lưới
    chart.height, chart.width = 7.5, 17
    chart.set_categories(cats)
    chart.legend = None
    chart.varyColors = False
    chart.x_axis.delete = False
    chart.y_axis.delete = False
    chart.x_axis.number_format = "dd/mm"
    chart.y_axis.number_format = "0"


def _sheet_days(ws: Worksheet, days: list[DayRow], meta: ReportMeta) -> None:
    ws.title = "Tiêu thụ theo ngày"
    labels = ["Ngày", "Thứ", "Đầu ngày (m³)", "Cuối ngày (m³)", "Nạp trong ngày (m³)",
              "Tiêu thụ (m³)", "Ghi chú"]
    n = len(labels)
    _band(ws, "TIÊU THỤ THEO NGÀY", meta, n,
          "Tiêu thụ = đầu ngày + nạp trong ngày − cuối ngày. Đầu ngày là lần đo đầu tiên "
          "của ngày; cuối ngày là đầu ngày hôm sau. Chỉ gồm những ngày đã qua.")
    _header(ws, labels, [13, 11, 14, 14, 16, 14, 26])
    r = HEADER_ROW
    for i, d in enumerate(days, start=1):
        r = HEADER_ROW + i
        c = ws.cell(r, 1, d.day)
        c.number_format = _DATE
        c.alignment = Alignment(horizontal="center", vertical="center")
        _text(ws.cell(r, 2), WEEKDAY_VI[d.day.weekday()])
        for col, v in ((3, d.open_m3), (4, d.close_m3), (5, d.refill_m3 or None), (6, d.use_m3)):
            cell = ws.cell(r, col, None if v is None else round(v, 2))
            cell.number_format = _M3
        if d.refill_m3:
            ws.cell(r, 5).font = Font(bold=True, color=_GREEN)
        note = ws.cell(r, 7)
        _text(note, d.note)
        if d.rest:
            note.font = Font(italic=True, color=_ORANGE)
        if d.refill_m3 > 0:
            fill: PatternFill | None = _DONE_FILL
        elif d.rest or d.open_m3 is None:
            fill = _MUTED_FILL
        else:
            fill = _ZEBRA_FILL if i % 2 == 0 else None
        _row(ws, r, n, fill)
    if not days:
        _empty(ws, n, "Kỳ này chưa có ngày nào đã qua.")
        _print_setup(ws, meta, landscape=True)
        return

    uses = [d.use_m3 for d in days if d.use_m3 is not None]
    _total(ws, r + 1, n, 4, "Tổng cộng", {
        5: round(sum(d.refill_m3 for d in days), 2),
        6: round(sum(uses), 2),
    })
    # Bình quân những ngày CÓ tiêu thụ — cùng cách tính "khi nhà máy chạy" của trang
    # Kế hoạch. Gộp cả ngày nghỉ (tiêu thụ 0) thì số bị kéo thấp và lệch với mức
    # tiêu thụ người vận hành đang lập kế hoạch.
    running = [u for u in uses if u > 0]
    avg = r + 2
    ws.merge_cells(start_row=avg, start_column=1, end_row=avg, end_column=5)
    ws.cell(avg, 1, "Bình quân ngày nhà máy chạy").alignment = Alignment(horizontal="right")
    ws.cell(avg, 1).font = Font(italic=True, color=_GREY)
    if running:
        a = ws.cell(avg, 6, round(sum(running) / len(running), 2))
        a.number_format = _M3
        a.font = Font(bold=True, color=_BRAND)

    # Hai biểu đồ cạnh bảng: thể tích đầu ngày (thấy được nhịp nạp) và tiêu thụ
    # mỗi ngày (thấy được ngày nghỉ, ngày chạy mạnh). Nhìn một lần là hiểu kỳ.
    first = HEADER_ROW + 1
    cats = Reference(ws, min_col=1, min_row=first, max_row=r)
    line = LineChart()
    line.add_data(Reference(ws, min_col=3, min_row=HEADER_ROW, max_row=r), titles_from_data=True)
    _chart_style(line, "Thể tích đầu ngày (m³)", cats)
    s = line.series[0]
    s.smooth = False          # đường gãy: nạp là một bước nhảy, không phải đường cong
    s.marker.symbol = "circle"
    s.marker.size = 5
    s.marker.graphicalProperties.solidFill = _BRAND
    s.marker.graphicalProperties.line.solidFill = _BRAND
    s.graphicalProperties.line.solidFill = _BRAND
    s.graphicalProperties.line.width = 22000
    ws.add_chart(line, f"{get_column_letter(n + 2)}{HEADER_ROW}")
    bar = BarChart()
    bar.add_data(Reference(ws, min_col=6, min_row=HEADER_ROW, max_row=r), titles_from_data=True)
    _chart_style(bar, "Tiêu thụ mỗi ngày (m³)", cats)
    bar.gapWidth = 60
    bar.series[0].graphicalProperties.solidFill = "5B9BD5"
    bar.series[0].graphicalProperties.line.solidFill = "5B9BD5"
    ws.add_chart(bar, f"{get_column_letter(n + 2)}{HEADER_ROW + 17}")
    _print_setup(ws, meta, landscape=True)


def build_report(
    schedule: list[ScheduleRow],
    refills: list[ActualRefill],
    days: list[DayRow],
    meta: ReportMeta,
) -> bytes:
    wb = Workbook()
    _sheet_schedule(wb.active, schedule, meta)
    _sheet_refills(wb.create_sheet(), refills, meta)
    _sheet_days(wb.create_sheet(), days, meta)
    wb.active = 0
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
