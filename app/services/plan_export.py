"""File Excel "Lịch nạp": CHỈ những ngày có nạp, theo mẫu người dùng gửi đối tác.

Bảng của trang Kế hoạch có một dòng cho MỖI ngày; thứ người dùng gửi đi thì chỉ có
các ngày xe tới — STT, đơn vị cung cấp, khách hàng, địa điểm, lượng, ngày, ghi chú,
và dòng tổng cộng. File này dựng đúng bảng đó.

Các dòng do TRANG gửi lên, không tính lại ở đây: lịch nạp được tính ở trình duyệt
(cùng những ô tích, số đo tay và lần đo mới nhất mà người dùng đang nhìn), và một
bản tính thứ hai ở server là cách chắc chắn nhất để file xuất lệch với màn hình.
Server chỉ thêm thông tin của bồn và quy đổi tấn.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from openpyxl import Workbook
from openpyxl.cell.cell import Cell
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

#: "20 tấn là khoảng 44 m³" (người dùng, 30/09/2026). Bồn lưu hệ số riêng thì dùng
#: hệ số đó — khối lượng riêng LNG đổi theo nguồn hàng.
DEFAULT_M3_PER_TONNE = Decimal("2.2")

HEADER = [
    "STT", "Đơn vị cung cấp", "Khách hàng", "Địa điểm",
    "Lượng (tấn)", "Lượng (m³)", "Ngày nạp", "Ghi chú",
]
_WIDTHS = [6, 34, 22, 22, 12, 12, 13, 40]
_Q2 = Decimal("0.01")


@dataclass(frozen=True)
class ExportRow:
    day: date
    m3: Decimal
    note: str | None = None


def tonnes(m3: Decimal, m3_per_tonne: Decimal) -> Decimal:
    return (m3 / m3_per_tonne).quantize(_Q2, rounding=ROUND_HALF_UP)


def _text(cell: Cell, value: str | None) -> None:
    """Ghi CHUỖI, không bao giờ là công thức.

    openpyxl coi mọi chuỗi mở đầu bằng "=" là công thức. Tên khách hàng hay ghi chú
    là chữ người gõ vào, và một ghi chú "=HYPERLINK(...)" không được phép chạy trên
    máy của người nhận file.
    """
    cell.value = value or ""
    cell.data_type = "s"


def build_xlsx(
    rows: list[ExportRow],
    *,
    supplier: str | None,
    customer: str | None,
    site: str | None,
    m3_per_tonne: Decimal,
) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Lịch nạp"

    thin = Side(style="thin", color="999999")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    bold = Font(bold=True)

    ws.append(HEADER)
    for c in ws[1]:
        c.font = bold
        c.border = box
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    ordered = sorted(rows, key=lambda r: r.day)
    for i, r in enumerate(ordered, start=1):
        n = i + 1
        ws.cell(n, 1, i)
        _text(ws.cell(n, 2), supplier)
        _text(ws.cell(n, 3), customer)
        _text(ws.cell(n, 4), site)
        ws.cell(n, 5, float(tonnes(r.m3, m3_per_tonne))).number_format = "0.00"
        ws.cell(n, 6, float(r.m3.quantize(_Q2, rounding=ROUND_HALF_UP))).number_format = "0.00"
        ws.cell(n, 7, r.day).number_format = "dd/mm/yyyy"
        _text(ws.cell(n, 8), r.note)
        for col in range(1, 9):
            ws.cell(n, col).border = box
        ws.cell(n, 1).alignment = Alignment(horizontal="center")
        ws.cell(n, 7).alignment = Alignment(horizontal="center")

    # Dòng tổng dùng SUM chứ không ghi số: người nhận hay sửa tay một dòng trong
    # Excel, và tổng phải đi theo.
    t = len(ordered) + 2
    ws.cell(t, 1, "Tổng cộng")
    ws.merge_cells(start_row=t, start_column=1, end_row=t, end_column=4)
    last = t - 1
    for col, letter in ((5, "E"), (6, "F")):
        ws.cell(t, col, f"=SUM({letter}2:{letter}{last})" if ordered else 0)
        ws.cell(t, col).number_format = "0.00"
    yellow = PatternFill("solid", fgColor="FFFF00")
    for col in range(1, 9):
        c = ws.cell(t, col)
        c.font = bold
        c.fill = yellow
        c.border = box
    ws.cell(t, 1).alignment = Alignment(horizontal="center")

    for i, w in enumerate(_WIDTHS, start=1):
        ws.column_dimensions[chr(64 + i)].width = w
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
