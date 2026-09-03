"""Báo động do NGUỒN phát, đọc-thuần.

Khác ``/api/alerts`` ở một điểm quan trọng, và cố ý đặt tên khác để không ai nhầm:

* ``/api/alerts``        — cảnh báo do platform TỰ SUY từ số đo (ngưỡng mức, pin,
  sóng, dự báo cạn). Ta sở hữu định nghĩa, ta chọn severity.
* ``/api/alarms/vendor`` — báo động do THIẾT BỊ TẠI NHÀ MÁY phát, ta chỉ lưu lại.
  Ta KHÔNG tự đặt severity ở đây: nguồn gắn ``symbol="danger"`` cho cả 716 dòng,
  nên map thẳng nó sang severity là bịa ra một thang không tồn tại.

Hai hình dạng vì hai câu hỏi khác nhau: "đang có việc gì cần xử lý" (đã gộp) và
"chính xác lúc đó xảy ra gì" (thô, để truy).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query

from app.api.deps import HistoryQueryDep, SessionDep, UserDep
from app.api.schemas import AlarmEpisodeOut, AlarmSummaryOut, Page, VendorAlarmOut
from app.repositories import vendor_alarms as alarm_repo

log = logging.getLogger(__name__)
router = APIRouter(tags=["alarms"])
UTC = ZoneInfo("UTC")

SiteDep = Annotated[
    str | None,
    Query(max_length=16, description="Lọc theo mã địa điểm, ví dụ YKH"),
]
DeviceDep = Annotated[
    str | None,
    Query(max_length=64, description="Lọc theo thiết bị, ví dụ SV4 / LT1 / PS1"),
]


@router.get("/alarms/vendor/summary", response_model=AlarmSummaryOut)
def vendor_alarm_summary(
    session: SessionDep,
    q: HistoryQueryDep,
    _: UserDep,
    site_code: SiteDep = None,
) -> AlarmSummaryOut:
    """Báo động đã gộp thành việc, kèm tỉ lệ gộp ĐO ĐƯỢC.

    Không phân trang: sau khi gộp thì số dòng nhỏ, và mục đích của trang này là
    thấy TOÀN CẢNH. Phân trang một bản tóm tắt là làm mất đúng thứ nó tồn tại để
    cung cấp.
    """
    assert q.from_ is not None and q.to is not None
    episodes, raw_total = alarm_repo.summarize(
        session, start=q.from_, end=q.to, site_code=site_code
    )
    n = len(episodes)
    # Không có dòng thô thì tỉ lệ gộp là 0, không phải 100: gộp 0 dòng thành 0
    # việc không phải một thành tích.
    reduction = 0.0 if raw_total <= 0 else (1.0 - n / raw_total) * 100.0
    return AlarmSummaryOut(
        items=[AlarmEpisodeOut.model_validate(e) for e in episodes],
        raw_total=raw_total,
        episodes_total=n,
        reduction_percent=reduction,
        generated_at=datetime.now(tz=UTC),
    )


@router.get("/alarms/vendor", response_model=Page[VendorAlarmOut])
def vendor_alarms(
    session: SessionDep,
    q: HistoryQueryDep,
    _: UserDep,
    site_code: SiteDep = None,
    device: DeviceDep = None,
) -> Page[VendorAlarmOut]:
    """Danh sách thô, phân trang. Dùng để truy một sự cố cụ thể."""
    assert q.from_ is not None and q.to is not None
    rows, total = alarm_repo.list_for(
        session,
        start=q.from_,
        end=q.to,
        site_code=site_code,
        device_id=device,
        limit=q.limit,
        offset=q.offset,
        ascending=q.ascending,
    )
    return Page[VendorAlarmOut](
        items=[VendorAlarmOut.model_validate(r) for r in rows],
        page=q.page,
        limit=q.limit,
        total=total,
        has_next=q.offset + q.limit < total,
    )
