"""Hai endpoint báo động nguồn, trên PostgreSQL thật.

Bài quan trọng nhất là ``test_ti_le_gop_la_so_do_duoc``: tuyên bố "716 -> 52" trước
đây chỉ tồn tại trong tài liệu và không ai đối chiếu được với dữ liệu. Sau commit
này nó là một con số API trả ra, tính từ chính các dòng đang có trong bảng.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from app.api.deps import HistoryQuery
from app.api.routers.vendor_alarms import vendor_alarm_summary, vendor_alarms
from app.domain.contracts import NormalizedAlarm
from app.repositories import vendor_alarms as repo

UTC = ZoneInfo("UTC")
SITE = "TST"
NOW = datetime.now(tz=UTC)

pytestmark = pytest.mark.db


def _q(*, limit: int = 100, page: int = 1, days: int = 7) -> HistoryQuery:
    return HistoryQuery(
        from_=NOW - timedelta(days=days),
        to=NOW + timedelta(minutes=1),
        page=page,
        limit=limit,
        order="desc",
    )


def _alarm(device: str, message: str, at: datetime) -> NormalizedAlarm:
    return NormalizedAlarm(
        source="tst",
        site_code=SITE,
        device_id=device,
        raised_at=at,
        vendor_ts_raw=at.strftime("%d/%m/%Y %H:%M:%S"),
        message=message,
        symbol="danger",
    )


def _seed_repeated(session: Session) -> tuple[int, int]:
    """Nguồn phát lại cùng một dòng mỗi lần quét — đúng hành vi thật.

    Trả về (số dòng thô, số việc mong đợi).
    """
    rows: list[NormalizedAlarm] = []
    # Ba "việc": SV4 lock, SV4 lỗi đóng, LT1 mức thấp. Mỗi việc lặp nhiều lần ở
    # các thời điểm khác nhau, đúng như một điều kiện còn đúng qua nhiều lần quét.
    plan = [
        ("SV4", "Van SV4: dang bi lock", 10),
        ("SV4", "Van SV4: loi dong van", 7),
        ("LT1", "Muc bon LT1: duoi muc du tru", 25),
    ]
    for device, msg, n in plan:
        for i in range(n):
            rows.append(_alarm(device, msg, NOW - timedelta(minutes=i + 1)))
    inserted, _ = repo.bulk_insert(session, rows)
    session.flush()
    return inserted, len(plan)


def test_danh_sach_tho_phan_trang_duoc(session: Session) -> None:
    raw_total, _ = _seed_repeated(session)

    page1 = vendor_alarms(session, _q(limit=10), None, SITE, None)  # type: ignore[arg-type]
    assert page1.total == raw_total
    assert len(page1.items) == 10
    assert page1.has_next is True
    # Mới nhất trước: người vận hành mở trang này để xem chuyện vừa xảy ra.
    assert page1.items[0].raised_at >= page1.items[-1].raised_at

    page2 = vendor_alarms(session, _q(limit=10, page=2), None, SITE, None)  # type: ignore[arg-type]
    ids1 = {(i.device_id, i.raised_at, i.message) for i in page1.items}
    ids2 = {(i.device_id, i.raised_at, i.message) for i in page2.items}
    # Phân trang không được trả trùng — đây là lý do order_by có tie-breaker id.
    assert ids1.isdisjoint(ids2)


def test_loc_theo_thiet_bi(session: Session) -> None:
    _seed_repeated(session)
    only_lt1 = vendor_alarms(session, _q(), None, SITE, "LT1")  # type: ignore[arg-type]
    assert only_lt1.total == 25
    assert {i.device_id for i in only_lt1.items} == {"LT1"}


def test_ti_le_gop_la_so_do_duoc(session: Session) -> None:
    """42 dòng thô -> 3 việc. Tỉ lệ gộp do API tính, không do ai tuyên bố."""
    raw_total, want_episodes = _seed_repeated(session)

    out = vendor_alarm_summary(session, _q(), None, SITE)  # type: ignore[arg-type]

    assert out.raw_total == raw_total == 42
    assert out.episodes_total == want_episodes == 3
    assert len(out.items) == 3
    # 1 - 3/42 = 92,857...% — tình cờ rất gần con số 92,7% từng được nêu.
    assert abs(float(out.reduction_percent) - (1 - 3 / 42) * 100) < 0.01

    by_device = {(e.device_id, e.count) for e in out.items}
    assert by_device == {("SV4", 10), ("SV4", 7), ("LT1", 25)}
    # Mỗi việc mang khoảng thời gian, để người đọc biết nó kéo dài bao lâu.
    for e in out.items:
        assert e.first_raised_at <= e.last_raised_at


def test_bang_rong_thi_ti_le_gop_la_0_khong_phai_100(session: Session) -> None:
    """Gộp 0 dòng thành 0 việc không phải một thành tích."""
    out = vendor_alarm_summary(session, _q(), None, SITE)  # type: ignore[arg-type]
    assert out.raw_total == 0
    assert out.episodes_total == 0
    assert float(out.reduction_percent) == 0.0


def test_bon_dong_cung_mot_giay_van_la_bon_viec(session: Session) -> None:
    """Sự cố van thật: 4 dòng cùng giây, 2 thiết bị, 2 message mỗi thiết bị.

    Nếu gộp theo (device, giây) thì còn 2 việc và mất đúng dòng nói van hỏng kiểu
    gì. Gộp theo message_hash thì còn đủ 4.
    """
    t = NOW - timedelta(hours=1)
    repo.bulk_insert(
        session,
        [
            _alarm("SV4", "Van SV4: dang bi lock", t),
            _alarm("SV4", "Van SV4: loi dong van", t),
            _alarm("SV3", "Van SV3: dang bi lock", t),
            _alarm("SV3", "Van SV3: loi dong van", t),
        ],
    )
    session.flush()

    out = vendor_alarm_summary(session, _q(), None, SITE)  # type: ignore[arg-type]
    assert out.raw_total == 4
    assert out.episodes_total == 4
    assert float(out.reduction_percent) == 0.0


def test_khong_phat_ra_message_hash_va_source(session: Session) -> None:
    """``message_hash`` là chi tiết nội bộ của khoá trùng; ``source`` là tên nguồn."""
    _seed_repeated(session)
    page = vendor_alarms(session, _q(limit=1), None, SITE, None)  # type: ignore[arg-type]
    assert page.items
    keys = set(page.items[0].model_dump())
    assert "message_hash" not in keys
    assert "source" not in keys
    assert "site_code" in keys  # site_code nói ĐỊA ĐIỂM, được phép
