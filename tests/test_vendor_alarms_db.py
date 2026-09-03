"""``vendor_alarms`` trên PostgreSQL THẬT.

Vì sao phải có DB thật ở đây: ``bulk_insert`` gọi
``on_conflict_do_nothing(constraint="uq_vendor_alarms_natural")`` — tham chiếu
constraint bằng CHUỖI TÊN. Tên trong model và tên trong migration khớp nhau khi
đọc bằng mắt, nhưng đọc không phải bằng chứng: sai một chữ thì lỗi chỉ lộ ra lúc
chạy thật, trên production, giữa một cycle ingest.

Và bài test quan trọng nhất ở đây là ``test_same_second_same_device_two_messages``:
nó chứng minh khoá tự nhiên PHẢI có ``message_hash``. Sự cố van thật là bốn dòng
trong cùng một giây — hai thiết bị, mỗi thiết bị hai message khác nhau. Khoá
(site_code, device_id, raised_at) không có message_hash sẽ ăn mất hai dòng, và
mất đúng dòng nói van hỏng kiểu gì.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import VendorAlarm
from app.domain.contracts import NormalizedAlarm
from app.repositories import vendor_alarms as repo

VN = ZoneInfo("Asia/Ho_Chi_Minh")
SITE = "TST"
# Một giây duy nhất, dùng cho mọi dòng: đó chính là điều kiện cần tái hiện.
T0 = datetime(2020, 1, 1, 8, 0, 0, tzinfo=VN)

pytestmark = pytest.mark.db


def _alarm(device: str, message: str, at: datetime = T0) -> NormalizedAlarm:
    return NormalizedAlarm(
        source="tst",
        site_code=SITE,
        device_id=device,
        raised_at=at,
        vendor_ts_raw=at.strftime("%d/%m/%Y %H:%M:%S"),
        message=message,
        symbol="danger",
    )


def _count(session: Session) -> int:
    return session.execute(
        select(func.count())
        .select_from(VendorAlarm)
        .where(VendorAlarm.site_code == SITE)
    ).scalar_one()


def _four_valve_alarms() -> list[NormalizedAlarm]:
    return [
        _alarm("SV4", "Van SV4: dang bi lock"),
        _alarm("SV4", "Van SV4: loi dong van"),
        _alarm("SV3", "Van SV3: dang bi lock"),
        _alarm("SV3", "Van SV3: loi dong van"),
    ]


def test_same_second_same_device_two_messages(session: Session) -> None:
    """Bốn dòng cùng một giây phải còn đủ bốn, không bị khoá tự nhiên ăn mất."""
    inserted, dupes = repo.bulk_insert(session, _four_valve_alarms())
    assert (inserted, dupes) == (4, 0)
    assert _count(session) == 4

    hashes = (
        session.execute(
            select(VendorAlarm.message_hash).where(VendorAlarm.site_code == SITE)
        )
        .scalars()
        .all()
    )
    assert len(set(hashes)) == 4, "message_hash phải phân biệt được bốn dòng"


def test_reinsert_is_idempotent(session: Session) -> None:
    """Nạp lại đúng bộ đó không sinh dòng mới.

    Đây là đường đi thật: mỗi cycle fetch lại cả ngày, nên hầu hết dòng là trùng.
    Bài này cũng là chỗ duy nhất câu ON CONFLICT tham chiếu constraint theo tên
    được PostgreSQL thi hành — sai tên thì fail ở đây, không phải trên production.
    """
    first = repo.bulk_insert(session, _four_valve_alarms())
    assert first == (4, 0)

    second = repo.bulk_insert(session, _four_valve_alarms())
    assert second == (0, 4)
    assert _count(session) == 4


def test_duplicates_inside_one_batch_do_not_raise(session: Session) -> None:
    """Trùng NGAY TRONG một batch phải bị bỏ qua, không nổ.

    ``DO UPDATE`` sẽ raise "cannot affect row a second time" ở tình huống này;
    ``DO NOTHING`` thì không. Pin lại để ai đó đổi sang DO UPDATE là thấy đỏ.
    """
    one = _alarm("SV4", "Van SV4: dang bi lock")
    inserted, dupes = repo.bulk_insert(session, [one, one, one])
    assert inserted == 1
    assert dupes == 2
    assert _count(session) == 1


def test_different_second_is_a_separate_row(session: Session) -> None:
    """Cùng thiết bị, cùng message, khác giây → hai sự kiện, không phải một."""
    msg = "Van SV4: dang bi lock"
    later = T0.replace(second=1)
    inserted, dupes = repo.bulk_insert(
        session, [_alarm("SV4", msg), _alarm("SV4", msg, at=later)]
    )
    assert (inserted, dupes) == (2, 0)
    assert _count(session) == 2
