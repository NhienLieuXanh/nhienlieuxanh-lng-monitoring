"""File Excel "Lịch nạp": chỉ các ngày nạp, theo mẫu người dùng gửi đối tác (30/09/2026).

Mẫu: STT | Công ty TNHH Nhiên Liệu Xanh | Yokohama | Cảng Cái Mép | 20 | 28/09/2026 |
ghi chú, rồi dòng "Tổng cộng". Người dùng: "20 tấn là khoảng 44 m³" và lượng mỗi
chuyến KHÔNG cố định — nên file ghi cả tấn (quy đổi) lẫn m³ (số của trang).
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import pytest
from openpyxl import load_workbook
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.routers import plan as plan_router
from app.api.schemas import PlanExportIn, PlanSettingsIn
from app.db.models import Terminal
from app.repositories import plan_readings as pr_repo
from app.services import plan_export as px

PSN = "YKH-TANK-01"


def _sheet(content: bytes):
    return load_workbook(io.BytesIO(content)).active


def _rows() -> list[px.ExportRow]:
    return [
        px.ExportRow(day=date(2026, 10, 3), m3=Decimal("41.36")),
        px.ExportRow(day=date(2026, 9, 28), m3=Decimal("35.31"), note="Đã nạp thực tế lúc 14:29"),
    ]


def test_mot_dong_moi_lan_nap_xep_theo_ngay_va_co_dong_tong() -> None:
    ws = _sheet(px.build_xlsx(
        _rows(), supplier="Công ty TNHH Nhiên Liệu Xanh", customer="Yokohama",
        site="Cảng Cái Mép", m3_per_tonne=Decimal("2.2"),
    ))
    assert [c.value for c in ws[1]] == px.HEADER
    r2 = [c.value for c in ws[2]]
    assert r2[:4] == [1, "Công ty TNHH Nhiên Liệu Xanh", "Yokohama", "Cảng Cái Mép"]
    assert r2[4] == pytest.approx(16.05)            # 35.31 / 2.2
    assert r2[5] == pytest.approx(35.31)
    assert r2[6].date() == date(2026, 9, 28)        # sắp theo ngày, lần nạp thật trước
    assert r2[7] == "Đã nạp thực tế lúc 14:29"
    assert ws.cell(2, 7).number_format == "dd/mm/yyyy"
    assert ws.cell(3, 1).value == 2 and ws.cell(3, 7).value.date() == date(2026, 10, 3)
    assert ws.cell(3, 5).value == pytest.approx(18.8)  # 41.36 / 2.2
    assert ws.cell(4, 1).value == "Tổng cộng"
    assert ws.cell(4, 5).value == "=SUM(E2:E3)" and ws.cell(4, 6).value == "=SUM(F2:F3)"
    assert ws.max_row == 4


def test_44_m3_la_20_tan() -> None:
    assert px.tonnes(Decimal("44"), px.DEFAULT_M3_PER_TONNE) == Decimal("20.00")


def test_chu_nguoi_go_khong_bao_gio_thanh_cong_thuc() -> None:
    ws = _sheet(px.build_xlsx(
        [px.ExportRow(day=date(2026, 10, 3), m3=Decimal("1"), note="=HYPERLINK(\"x\")")],
        supplier="=1+1", customer=None, site=None, m3_per_tonne=Decimal("2.2"),
    ))
    assert ws.cell(2, 2).value == "=1+1" and ws.cell(2, 2).data_type == "s"
    assert ws.cell(2, 8).data_type == "s"
    assert ws.cell(2, 3).value in ("", None)


def test_khong_co_lan_nap_nao_van_ra_file_hop_le() -> None:
    ws = _sheet(px.build_xlsx([], supplier=None, customer=None, site=None,
                              m3_per_tonne=Decimal("2.2")))
    assert ws.cell(2, 1).value == "Tổng cộng" and ws.cell(2, 5).value == 0


def test_dong_ngoai_khung_hoac_khung_nguoc_bi_tu_choi() -> None:
    with pytest.raises(ValidationError):
        PlanExportIn(from_day=date(2026, 10, 1), to_day=date(2026, 10, 31),
                     rows=[{"day": date(2026, 11, 1), "m3": 40}])
    with pytest.raises(ValidationError):
        PlanExportIn(from_day=date(2026, 10, 31), to_day=date(2026, 10, 1), rows=[])
    with pytest.raises(ValidationError):   # lượng 0 không phải một lần nạp
        PlanExportIn(from_day=date(2026, 10, 1), to_day=date(2026, 10, 31),
                     rows=[{"day": date(2026, 10, 3), "m3": 0}])


def test_route_dung_he_so_mac_dinh_va_ten_file_an_toan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plan_router, "_require_terminal", lambda s, p: None)
    monkeypatch.setattr(plan_router.pr_repo, "get_settings_for", lambda s, p: None)
    body = PlanExportIn(from_day=date(2026, 9, 27), to_day=date(2026, 12, 25),
                        rows=[{"day": date(2026, 10, 3), "m3": "44"}])
    resp = plan_router.export_refills_xlsx('A/B"C', body, None, None)  # type: ignore[arg-type]
    assert resp.media_type.startswith("application/vnd.openxmlformats")
    assert resp.headers["content-disposition"] == (
        'attachment; filename="lich_nap_A_B_C_20260927_20261225.xlsx"'
    )
    ws = _sheet(resp.body)
    assert ws.cell(2, 5).value == pytest.approx(20.0)


def test_chuoi_rong_la_xoa() -> None:
    b = PlanSettingsIn(customer_name="   ")
    assert b.customer_name is None and "customer_name" in b.model_fields_set


@pytest.mark.db
def test_luu_theo_bon_va_route_dung_so_da_luu(session: Session) -> None:
    session.add(Terminal(psn=PSN, name=PSN))
    session.flush()
    pr_repo.save_settings(session, PSN, {
        "supplier_name": "Công ty TNHH Nhiên Liệu Xanh", "customer_name": "Yokohama",
        "site_name": "Cảng Cái Mép", "m3_per_tonne": Decimal("2.5"),
    }, by="t")
    # Lưu một ô khác không được xoá các ô vừa lưu (ngữ nghĩa trộn).
    pr_repo.save_settings(session, PSN, {"reserve_l": Decimal("11000")}, by="t")
    st = pr_repo.get_settings_for(session, PSN)
    assert st is not None and st.customer_name == "Yokohama" and st.m3_per_tonne == Decimal("2.5")

    body = PlanExportIn(from_day=date(2026, 10, 1), to_day=date(2026, 10, 31),
                        rows=[{"day": date(2026, 10, 3), "m3": "50"}])
    ws = _sheet(plan_router.export_refills_xlsx(PSN, body, session, "t").body)  # type: ignore[arg-type]
    assert [ws.cell(2, c).value for c in (2, 3, 4)] == [
        "Công ty TNHH Nhiên Liệu Xanh", "Yokohama", "Cảng Cái Mép",
    ]
    assert ws.cell(2, 5).value == pytest.approx(20.0)   # 50 / 2.5


@pytest.mark.db
def test_he_so_quy_doi_phai_duong(session: Session) -> None:
    session.add(Terminal(psn=PSN, name=PSN))
    session.flush()
    with pytest.raises(IntegrityError):
        pr_repo.save_settings(session, PSN, {"m3_per_tonne": Decimal("0")}, by="t")
        session.flush()
