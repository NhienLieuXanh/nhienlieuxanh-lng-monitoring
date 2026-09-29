"""Thể tích ĐẦU NGÀY thật cho những ngày đã qua của trang Kế hoạch.

Bug thật, 29/09/2026: đổi "Ngày bắt đầu" về 27/09 thì dòng 27/09 hiện 34,39 m³ —
số đo lúc 15:59 ngày 29/09 — trong khi bồn thật hôm đó 15,40, và bảng bịa ra một
lần nạp 29/09 trong khi xe thật tới 28/09. Trang chỉ có MỘT con số khởi đầu và
chiếu nó ngược về ngày đã chọn. Endpoint này đưa cho trang con số thật của từng ngày.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.routers import telemetry as tel_router
from app.config import Settings
from app.db.models import Telemetry, Terminal
from app.repositories import telemetry as tel

VN = ZoneInfo("Asia/Ho_Chi_Minh")
PSN = "OPEN-TEST-01"
TZ = "Asia/Ho_Chi_Minh"


def _seed(session: Session, pts: list[tuple[datetime, float | None]]) -> None:
    t = Terminal(psn=PSN, capacity_l=Decimal("60000"))
    session.add(t)
    session.flush()
    for at, v in pts:
        session.add(
            Telemetry(
                terminal_id=t.id, psn=PSN, sampled_at=at,
                volume_l=None if v is None else Decimal(str(v)),
                source="tst", raw_payload={},
            )
        )
    session.flush()


@pytest.mark.db
def test_lay_lan_do_DAU_TIEN_cua_moi_ngay_gio_VN(session: Session) -> None:
    """00:10 giờ VN ngày 28 là 17:10 UTC ngày 27 — vẫn là ĐẦU ngày 28, không phải cuối 27."""
    _seed(session, [
        (datetime(2026, 9, 27, 0, 5, tzinfo=VN), 15400.0),
        (datetime(2026, 9, 27, 23, 55, tzinfo=VN), 9900.0),
        (datetime(2026, 9, 28, 0, 10, tzinfo=VN), 9880.0),
        (datetime(2026, 9, 28, 14, 29, tzinfo=VN), 45000.0),   # sau lần nạp
        (datetime(2026, 9, 29, 0, 1, tzinfo=VN), 40200.0),
    ])
    rows = tel.daily_open(session, PSN, date(2026, 9, 27), date(2026, 9, 29), tz_name=TZ)
    assert [(d, v) for d, _, v in rows] == [
        (date(2026, 9, 27), 15400.0),
        (date(2026, 9, 28), 9880.0),
        (date(2026, 9, 29), 40200.0),
    ]
    assert rows[1][1] == datetime(2026, 9, 28, 0, 10, tzinfo=VN)


@pytest.mark.db
def test_bo_qua_dong_khong_co_the_tich_va_ngay_khong_co_so_do(session: Session) -> None:
    _seed(session, [
        (datetime(2026, 9, 27, 0, 0, tzinfo=VN), None),        # chỉ có áp suất
        (datetime(2026, 9, 27, 0, 30, tzinfo=VN), 15000.0),
        (datetime(2026, 9, 29, 9, 0, tzinfo=VN), 30000.0),     # 28 im cả ngày
    ])
    rows = tel.daily_open(session, PSN, date(2026, 9, 26), date(2026, 9, 30), tz_name=TZ)
    assert [(d, v) for d, _, v in rows] == [
        (date(2026, 9, 27), 15000.0),
        (date(2026, 9, 29), 30000.0),
    ]
    # Bồn im cả đêm: lần đo đầu ngày 29 là 09:00, và người gọi phải thấy được giờ đó.
    assert rows[1][1].astimezone(VN).hour == 9


@pytest.mark.db
def test_khong_lan_sang_bon_khac(session: Session) -> None:
    _seed(session, [(datetime(2026, 9, 27, 1, 0, tzinfo=VN), 15000.0)])
    assert tel.daily_open(session, "KHAC-01", date(2026, 9, 27), date(2026, 9, 27), tz_name=TZ) == []


def _goi(session: object, frm: date, to: date) -> object:
    return tel_router.daily_open(
        PSN, session, Settings(), None, from_=frm, to=to,  # type: ignore[arg-type]
    )


def test_route_tu_choi_khoang_nguoc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tel_router, "_require_terminal", lambda s, p: None)
    with pytest.raises(HTTPException) as e:
        _goi(None, date(2026, 9, 29), date(2026, 9, 27))
    assert e.value.status_code == 422


def test_route_tran_366_ngay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tel_router, "_require_terminal", lambda s, p: None)
    monkeypatch.setattr(tel_router.tel_repo, "daily_open", lambda *a, **k: [])
    assert _goi(None, date(2026, 1, 1), date(2026, 12, 31)) == []   # 365 ngày
    assert _goi(None, date(2028, 1, 1), date(2028, 12, 31)) == []   # 366 (năm nhuận)
    with pytest.raises(HTTPException) as e:
        _goi(None, date(2026, 1, 1), date(2027, 1, 2))
    assert e.value.status_code == 422
