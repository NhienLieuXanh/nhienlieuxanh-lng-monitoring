"""Số lần đo theo ngày, và nhịp THẬT của nguồn.

Hai thứ này ra đời vì trang Phân tích chỉ có ô số và một danh sách dòng chữ —
người dùng nói đúng: "cần trực quan hoá dữ liệu bằng biểu đồ chứ không phải vài
dòng nhìn rất rối". Một cột mỗi ngày trả lời "dữ liệu của tôi có dùng được không"
trong một cái nhìn.

Và chúng vá một chỗ nói sai: thẻ ghi "kỳ vọng 4320 ở nhịp 30 phút" cho một nguồn
phát mỗi PHÚT. 30 phút là lưới gộp của tầng phân tích, không phải nhịp của nguồn.
Hai con số lệch nhau 30 lần nên phải có hai cái tên.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from app.db.models import Telemetry, Terminal
from app.repositories import telemetry as tel

UTC = ZoneInfo("UTC")
VN = ZoneInfo("Asia/Ho_Chi_Minh")
PSN = "DAILY-TEST-01"

pytestmark = pytest.mark.db


def _seed(session: Session, stamps: list[datetime]) -> None:
    t = Terminal(psn=PSN, capacity_l=Decimal("60000"))
    session.add(t)
    session.flush()
    for i, at in enumerate(stamps):
        session.add(
            Telemetry(
                terminal_id=t.id, psn=PSN, sampled_at=at,
                volume_l=Decimal(str(50000 - i)), source="tst", raw_payload={},
            )
        )
    session.flush()


def test_gom_theo_ngay_gio_DIA_PHUONG_khong_phai_UTC(session: Session) -> None:
    """23:30 giờ VN ngày 3/9 là UTC 16:30 ngày 3/9 — cùng ngày.

    Nhưng 06:30 giờ VN ngày 4/9 là UTC 23:30 ngày 3/9. Gom theo UTC thì mẫu đó rơi
    sang ngày 3, và mỗi cột trên biểu đồ lệch 7 giờ. "Ngày 3/9 có bao nhiêu lần đo"
    là câu hỏi của người vận hành ở Việt Nam.
    """
    _seed(session, [
        datetime(2026, 9, 3, 23, 30, tzinfo=VN),   # ngày 3 giờ VN
        datetime(2026, 9, 4, 0, 30, tzinfo=VN),    # ngày 4 giờ VN (= 17:30 UTC ngày 3)
        datetime(2026, 9, 4, 6, 30, tzinfo=VN),    # ngày 4 giờ VN (= 23:30 UTC ngày 3)
        datetime(2026, 9, 4, 12, 0, tzinfo=VN),
    ])
    rows = tel.daily_counts(
        session,
        PSN,
        datetime(2026, 9, 1, tzinfo=UTC),
        datetime(2026, 9, 6, tzinfo=UTC),
        tz_name="Asia/Ho_Chi_Minh",
    )
    assert dict(rows) == {date(2026, 9, 3): 1, date(2026, 9, 4): 3}


def test_chi_tra_ngay_CO_du_lieu(session: Session) -> None:
    """Ngày trống không xuất hiện: chỉ client biết cửa sổ là bao nhiêu ngày."""
    _seed(session, [datetime(2026, 9, 4, 8, 0, tzinfo=VN)])
    rows = tel.daily_counts(
        session, PSN,
        datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 6, tzinfo=UTC),
        tz_name="Asia/Ho_Chi_Minh",
    )
    assert len(rows) == 1


def test_tang_dan_theo_ngay(session: Session) -> None:
    _seed(session, [
        datetime(2026, 9, 4, 8, 0, tzinfo=VN),
        datetime(2026, 9, 2, 8, 0, tzinfo=VN),
        datetime(2026, 9, 3, 8, 0, tzinfo=VN),
    ])
    rows = tel.daily_counts(
        session, PSN,
        datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 6, tzinfo=UTC),
        tz_name="Asia/Ho_Chi_Minh",
    )
    assert [d for d, _ in rows] == sorted(d for d, _ in rows)


def test_nhip_nguon_KHAC_nhip_luoi_gop(session: Session) -> None:
    """Nguồn nhịp 1 phút phải báo 1 phút, không phải 30 phút của lưới gộp.

    Đây là con số trang Phân tích từng nói sai. Ngày đầy đủ nhất có 1440 mẫu ->
    1440/1440 = 1,0 phút.
    """
    from app.api.routers import analytics as ar
    from app.config import get_settings

    day = datetime(2026, 9, 3, 0, 0, tzinfo=VN)
    _seed(session, [day + timedelta(minutes=i) for i in range(1440)])
    out = ar._build(
        session, get_settings(), PSN, "bồn test", 60000.0,
        window_days=90.0, now=datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
    )
    assert out.source_cadence_minutes == 1.0, "nhịp THẬT của nguồn"
    assert out.quality.cadence_minutes == 30.0, "nhịp của lưới gộp — khác, và đúng"
    assert len(out.daily) == 1
    assert out.daily[0].samples == 1440


def test_nhip_suy_tu_ngay_DAY_DU_NHAT_khong_phai_trung_binh(session: Session) -> None:
    """Ngày đầu và ngày cuối cửa sổ luôn là ngày dở; trộn vào sẽ báo nhịp thưa hơn."""
    from app.api.routers import analytics as ar
    from app.config import get_settings

    full = datetime(2026, 9, 3, 0, 0, tzinfo=VN)
    stamps = [full + timedelta(minutes=i) for i in range(1440)]      # ngày trọn
    stamps += [full + timedelta(days=1, minutes=i) for i in range(60)]  # ngày dở
    _seed(session, stamps)
    out = ar._build(
        session, get_settings(), PSN, None, 60000.0,
        window_days=90.0, now=datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
    )
    # trung binh hai ngay se ra 1440/((1440+60)/2) = 1,92 phut — sai
    assert out.source_cadence_minutes == 1.0
