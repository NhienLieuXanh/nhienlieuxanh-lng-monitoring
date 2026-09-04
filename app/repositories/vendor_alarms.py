"""Đọc/ghi bảng ``vendor_alarms``.

Ghi: ON CONFLICT DO NOTHING — một báo động đã xảy ra là lịch sử bất biến.
Đọc: hai hình dạng, thô và đã gộp. Xem ``summarize`` để biết vì sao cần cả hai.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import VendorAlarm
from app.domain.contracts import NormalizedAlarm

CHUNK = 500


def message_hash(message: str) -> str:
    return hashlib.md5(message.encode("utf-8"), usedforsecurity=False).hexdigest()


def to_row(alarm: NormalizedAlarm) -> dict:
    return {
        "site_code": alarm.site_code,
        "device_id": alarm.device_id,
        "raised_at": alarm.raised_at,
        "vendor_ts_raw": alarm.vendor_ts_raw,
        "message": alarm.message,
        "message_hash": message_hash(alarm.message),
        "symbol": alarm.symbol,
        "source": alarm.source,
    }


def list_for(
    session: Session,
    *,
    start: datetime,
    end: datetime,
    site_code: str | None = None,
    device_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
    ascending: bool = False,
) -> tuple[list[VendorAlarm], int]:
    """Một trang báo động thô, kèm tổng số để phân trang.

    Mặc định mới-nhất-trước: người vận hành mở trang này để xem chuyện vừa xảy ra.
    """
    filt = [VendorAlarm.raised_at >= start, VendorAlarm.raised_at <= end]
    if site_code:
        filt.append(VendorAlarm.site_code == site_code)
    if device_id:
        filt.append(VendorAlarm.device_id == device_id)

    total = session.execute(
        select(func.count()).select_from(VendorAlarm).where(*filt)
    ).scalar_one()
    order = VendorAlarm.raised_at.asc() if ascending else VendorAlarm.raised_at.desc()
    rows = (
        session.execute(
            select(VendorAlarm)
            .where(*filt)
            # id làm tie-breaker: sự cố van thật là bốn dòng CÙNG một giây, nên
            # chỉ sắp theo raised_at là thứ tự không xác định và phân trang có
            # thể trả trùng dòng hoặc bỏ sót dòng giữa hai trang.
            .order_by(order, VendorAlarm.id.asc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


@dataclass(frozen=True, slots=True)
class AlarmEpisode:
    """Nhiều dòng báo động giống nhau gộp thành MỘT việc cần xử lý."""

    site_code: str
    device_id: str
    message: str
    #: Khoá "cùng một việc" — chính thứ ``summarize`` group theo, và chính thứ nằm
    #: trong khoá tự nhiên ở đường ghi. Phát ra để tầng cảnh báo dựng được mã
    #: chặn-gửi-lại theo ĐÚNG đơn vị việc, không phải theo thiết bị (một van có
    #: thể báo hai lỗi khác nhau, và gộp chúng làm mất đúng thông tin cần).
    message_hash: str
    count: int
    first_raised_at: datetime
    last_raised_at: datetime


def summarize(
    session: Session,
    *,
    start: datetime,
    end: datetime,
    site_code: str | None = None,
) -> tuple[list[AlarmEpisode], int]:
    """Gộp báo động thành "việc", và trả kèm SỐ DÒNG THÔ đã gộp.

    Vì sao gộp: nguồn phát lại cùng một dòng mỗi lần quét trong khi điều kiện còn
    đúng, nên 716 dòng thô KHÔNG phải 716 việc. Người vận hành cần biết "van SV4
    báo lỗi đóng, 12 lần, từ X đến Y" — một dòng — chứ không phải mười hai dòng
    giống nhau.

    Gộp theo ``message_hash`` chứ không theo ``message``: hash là đúng thứ đang
    nằm trong khoá tự nhiên ở đường ghi, nên định nghĩa "cùng một việc" ở đây
    khớp chính xác định nghĩa "trùng" ở kia. Hai định nghĩa lệch nhau là cách
    chắc chắn để hai con số không bao giờ cộng lại đúng.

    Trả kèm tổng số dòng thô để tỉ lệ gộp là con số ĐO ĐƯỢC chứ không phải lời
    tuyên bố — đây là thứ cần để kiểm tuyên bố "716 -> 52".
    """
    filt = [VendorAlarm.raised_at >= start, VendorAlarm.raised_at <= end]
    if site_code:
        filt.append(VendorAlarm.site_code == site_code)

    raw_total = session.execute(
        select(func.count()).select_from(VendorAlarm).where(*filt)
    ).scalar_one()

    rows = session.execute(
        select(
            VendorAlarm.site_code,
            VendorAlarm.device_id,
            VendorAlarm.message_hash,
            func.min(VendorAlarm.message).label("message"),
            func.count().label("n"),
            func.min(VendorAlarm.raised_at).label("first_at"),
            func.max(VendorAlarm.raised_at).label("last_at"),
        )
        .where(*filt)
        .group_by(
            VendorAlarm.site_code, VendorAlarm.device_id, VendorAlarm.message_hash
        )
        # Việc mới nhất lên đầu; nhiều lần hơn thắng khi cùng mốc.
        .order_by(func.max(VendorAlarm.raised_at).desc(), func.count().desc())
    ).all()

    return [
        AlarmEpisode(
            site_code=r.site_code,
            device_id=r.device_id,
            message=r.message,
            message_hash=r.message_hash,
            count=int(r.n),
            first_raised_at=r.first_at,
            last_raised_at=r.last_at,
        )
        for r in rows
    ], int(raw_total)


def bulk_insert(session: Session, alarms: Sequence[NormalizedAlarm]) -> tuple[int, int]:
    """Trả về (inserted, duplicates)."""
    if not alarms:
        return 0, 0
    rows = [to_row(a) for a in alarms]
    inserted = 0
    for start in range(0, len(rows), CHUNK):
        chunk = rows[start : start + CHUNK]
        stmt = (
            pg_insert(VendorAlarm)
            .values(chunk)
            .on_conflict_do_nothing(
                constraint="uq_vendor_alarms_natural"
            )
        )
        n = len(session.execute(stmt.returning(VendorAlarm.id)).fetchall())
        inserted += n
    return inserted, len(rows) - inserted
