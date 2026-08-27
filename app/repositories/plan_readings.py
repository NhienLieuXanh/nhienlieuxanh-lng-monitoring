"""Đọc/ghi bảng ``plan_readings`` — thể tích đo tay dùng cho trang Kế hoạch.

Đơn vị ở tầng này là **lít**, khớp với ``telemetry.volume_l`` và
``terminals.capacity_l``. Trang Kế hoạch làm việc bằng m³ và quy đổi ở biên UI.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import PlanReading


def list_for(
    session: Session,
    psn: str,
    *,
    start: date | None = None,
    end: date | None = None,
) -> list[PlanReading]:
    """Số đo tay của một bồn, cũ trước mới sau.

    Không lọc ngày thì trả toàn bộ. Trang Kế hoạch chỉ vẽ tối đa 62 ngày nên
    lượng dòng ở đây luôn nhỏ; lọc là để không kéo nhiều năm lịch sử về client
    khi bảng đã dày lên.
    """
    stmt = select(PlanReading).where(PlanReading.psn == psn)
    if start is not None:
        stmt = stmt.where(PlanReading.reading_date >= start)
    if end is not None:
        stmt = stmt.where(PlanReading.reading_date <= end)
    return list(session.execute(stmt.order_by(PlanReading.reading_date)).scalars())


def upsert(
    session: Session,
    psn: str,
    day: date,
    volume_l: Decimal,
    *,
    by: str | None = None,
) -> PlanReading:
    """Ghi số đo của một ngày; nhập lại cùng ngày là GHI ĐÈ, không thêm dòng.

    Ghi đè là hành vi đúng ở đây, khác hẳn ``telemetry`` (nơi mặc định là
    ``DO NOTHING`` vì một điểm đo của máy là sự thật lịch sử bất biến). Số này do
    người nhập, nên gõ sai rồi sửa lại là việc bình thường và phải sửa được ngay
    tại chỗ. ``updated_at`` set tường minh vì ``onupdate`` của SQLAlchemy KHÔNG
    chạy trên câu lệnh Core như ``ON CONFLICT DO UPDATE``.
    """
    stmt = (
        pg_insert(PlanReading)
        .values(psn=psn, reading_date=day, volume_l=volume_l, entered_by=by)
        .on_conflict_do_update(
            index_elements=["psn", "reading_date"],
            set_={"volume_l": volume_l, "entered_by": by, "updated_at": func.now()},
        )
        .returning(PlanReading)
    )
    return session.execute(stmt).scalar_one()


def delete(session: Session, psn: str, day: date) -> bool:
    """Xoá số đo của một ngày. Trả về False nếu ngày đó vốn không có số.

    Xoá là cách người dùng nói "bỏ số tay đi, quay về dùng ước tính" — ô nhập để
    trống trên trang Kế hoạch gọi tới đây. Phân biệt được "đã xoá" với "vốn không
    có" để API trả 404 đúng chỗ thay vì báo thành công cho một lệnh không làm gì.
    """
    res = session.execute(
        sa_delete(PlanReading).where(
            PlanReading.psn == psn, PlanReading.reading_date == day
        )
    )
    return bool(res.rowcount)
