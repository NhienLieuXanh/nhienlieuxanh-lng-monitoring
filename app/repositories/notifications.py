"""Đọc/ghi bảng notifications."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Notification

log = logging.getLogger(__name__)


def last_sent_map(session: Session) -> dict[tuple[str, str], datetime]:
    """Lần gửi **thành công** gần nhất theo (psn, code), một query.

    Lấy cả bảng về dạng map trong một lần thay vì hỏi từng cảnh báo: mỗi vòng
    ingest có thể có hàng chục cảnh báo và đây là đường nóng chạy mỗi 10 phút.
    Bảng này chỉ lớn theo số cảnh báo THẬT đã gửi (bị cửa chặn gửi lại giới hạn),
    nên GROUP BY trên nó vẫn nhỏ trong nhiều năm.

    Chỉ tính ``status='sent'``: một lần gửi thất bại KHÔNG được mở cửa chặn, nếu
    không thì SMTP hỏng sẽ làm cảnh báo im lặng suốt cửa sổ resend.
    """
    rows = session.execute(
        select(Notification.psn, Notification.code, func.max(Notification.sent_at))
        .where(Notification.status == "sent")
        .group_by(Notification.psn, Notification.code)
    ).all()
    return {(psn, code): ts for psn, code, ts in rows if ts is not None}


def record(
    session: Session,
    *,
    psn: str,
    code: str,
    severity: str,
    status: str,
    message: str | None = None,
    detail: str | None = None,
    channel: str = "email",
) -> Notification:
    row = Notification(
        psn=psn,
        code=code,
        severity=severity,
        channel=channel,
        status=status,
        message=message,
        # Cắt detail: nó chứa text exception của SMTP, có thể rất dài và không có
        # trần thì một lỗi lạ sẽ nhét cả stack trace vào cột log.
        detail=None if detail is None else detail[:1000],
    )
    session.add(row)
    session.flush()
    return row


def recent(
    session: Session, *, limit: int = 50, psn: str | None = None
) -> list[Notification]:
    stmt = select(Notification).order_by(Notification.sent_at.desc()).limit(limit)
    if psn:
        stmt = stmt.where(Notification.psn == psn)
    return list(session.execute(stmt).scalars().all())


def count_all(session: Session) -> int:
    return int(
        session.execute(select(func.count()).select_from(Notification)).scalar_one()
    )
