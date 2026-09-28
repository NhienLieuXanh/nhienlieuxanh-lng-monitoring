"""Cờ "ngày nghỉ" / "nạp chỉ định" phải sống ở server, và bám theo NGÀY.

Bug đã xảy ra thật, anh Đức báo 28/09/2026: nhập ngày bắt đầu 28/09 mà kế hoạch
VẪN để Chủ Nhật là ngày nạp. Hai cờ này từng là hai ``Set`` trong bộ nhớ trang,
khoá theo CHỈ SỐ DÒNG. Đổi ngày bắt đầu thì cả bảng trượt đi bên dưới còn ô tích
đứng yên: tích ở dòng thứ 7 lúc bảng bắt đầu 01/09 (= 07/09, thứ Hai) trở thành
04/10 — Chủ Nhật — khi bảng bắt đầu 28/09. Và ô tích đi vòng qua luật cấm Chủ
Nhật, nên kế hoạch khai một ngày mà nhà cung cấp không giao.

Phần khoá-theo-ngày đã sửa ở tầng UI. File này khoá phần còn lại: hai cờ đó phải
BỀN — chúng quyết định ngày đặt hàng nên không được sống trong một tab trình
duyệt, và hai người cùng theo một bồn phải thấy cùng một kế hoạch.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.routers import plan as plan_router
from app.api.schemas import PlanDayFlagIn
from app.db.models import Terminal
from app.repositories import plan_readings as pr_repo

PSN = "2604200016"
PSN2 = "2605090007"
DAY = date(2026, 10, 4)  # Chủ Nhật — đúng ngày trong lời phàn nàn

pytestmark = pytest.mark.db


def _terminals(session: Session) -> None:
    session.add(Terminal(psn=PSN, name=PSN))
    session.add(Terminal(psn=PSN2, name=PSN2))
    session.flush()


# ------------------------------------------------------------------ repository


def test_co_luu_roi_doc_lai_duoc(session: Session) -> None:
    _terminals(session)
    pr_repo.set_flags(session, PSN, DAY, rest=False, forced=True, by="son")

    rows = pr_repo.list_flags(session, PSN)
    assert len(rows) == 1
    assert (rows[0].flag_date, rows[0].rest, rows[0].forced) == (DAY, False, True)
    assert rows[0].updated_by == "son"


def test_bo_ca_hai_tich_thi_XOA_dong_chu_khong_luu_dong_rong(session: Session) -> None:
    """Dòng (false, false) không mang thông tin nào; giữ lại là tích rác."""
    _terminals(session)
    pr_repo.set_flags(session, PSN, DAY, rest=True, forced=True)
    assert len(pr_repo.list_flags(session, PSN)) == 1

    assert pr_repo.set_flags(session, PSN, DAY, rest=False, forced=False) is None
    assert pr_repo.list_flags(session, PSN) == []


def test_database_tu_choi_dong_rong(session: Session) -> None:
    """Chốt chặn cuối: chỗ nào quên nhánh xoá thì DB từ chối, không im lặng nhận."""
    _terminals(session)
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO plan_day_flags (psn, flag_date, rest, forced) "
                "VALUES (:p, :d, false, false)"
            ),
            {"p": PSN, "d": DAY},
        )
    session.rollback()


def test_dat_lai_lan_hai_trong_cung_session_tra_ve_gia_tri_MOI(
    session: Session,
) -> None:
    """Thiếu populate_existing thì ORM trả object cũ và báo thành công kèm số cũ."""
    _terminals(session)
    pr_repo.set_flags(session, PSN, DAY, rest=True, forced=False)
    row = pr_repo.set_flags(session, PSN, DAY, rest=False, forced=True)
    assert row is not None
    assert (row.rest, row.forced) == (False, True)


def test_co_cua_bon_nay_khong_lan_sang_bon_khac(session: Session) -> None:
    _terminals(session)
    pr_repo.set_flags(session, PSN, DAY, rest=True, forced=False)

    assert pr_repo.list_flags(session, PSN2) == []
    assert len(pr_repo.list_flags(session, PSN)) == 1


def test_loc_theo_khoang_ngay(session: Session) -> None:
    _terminals(session)
    for d in (date(2026, 9, 30), date(2026, 10, 4), date(2026, 10, 20)):
        pr_repo.set_flags(session, PSN, d, rest=True, forced=False)

    rows = pr_repo.list_flags(
        session, PSN, start=date(2026, 10, 1), end=date(2026, 10, 10)
    )
    assert [r.flag_date for r in rows] == [date(2026, 10, 4)]


def test_nghi_va_nap_cung_ngay_la_HOP_LE(session: Session) -> None:
    """Ngày xưởng nghỉ vẫn có thể có xe tới giao — cố ý không cấm."""
    _terminals(session)
    row = pr_repo.set_flags(session, PSN, DAY, rest=True, forced=True)
    assert row is not None and row.rest and row.forced


# ---------------------------------------------------------------------- router


def test_put_roi_get_qua_router(session: Session) -> None:
    _terminals(session)
    out = plan_router.put_flags(
        PSN, DAY, PlanDayFlagIn(rest=False, forced=True), session, "son"
    )
    assert (out.psn, out.flag_date, out.forced) == (PSN, DAY, True)

    rows = plan_router.list_flags(PSN, session, None)  # type: ignore[arg-type]
    assert [(r.flag_date, r.forced) for r in rows] == [(DAY, True)]


def test_bo_het_tich_tra_200_khong_phai_404(session: Session) -> None:
    """"Ngày này không đánh dấu gì" là trạng thái hợp lệ người dùng vừa yêu cầu."""
    _terminals(session)
    plan_router.put_flags(PSN, DAY, PlanDayFlagIn(rest=True, forced=False), session, "s")

    out = plan_router.put_flags(
        PSN, DAY, PlanDayFlagIn(rest=False, forced=False), session, "s"
    )
    assert (out.rest, out.forced) == (False, False)
    assert plan_router.list_flags(PSN, session, None) == []  # type: ignore[arg-type]


def test_psn_la_khong_bi_tu_choi(session: Session) -> None:
    _terminals(session)
    with pytest.raises(HTTPException) as e:
        plan_router.put_flags(
            "KHONG-CO", DAY, PlanDayFlagIn(rest=True, forced=False), session, "s"
        )
    assert e.value.status_code == 404
