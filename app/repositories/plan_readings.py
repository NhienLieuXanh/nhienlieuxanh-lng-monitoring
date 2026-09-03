"""Đọc/ghi ``plan_readings`` (thể tích đo tay) và ``plan_settings`` (thông số
lập kế hoạch theo từng bồn) — cả hai đều chỉ dùng cho trang Kế hoạch.

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

from app.db.models import PlanReading, PlanSetting


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
        # populate_existing là BẮT BUỘC, không phải tối ưu. Không có nó, khi
        # ``(psn, reading_date)`` đã nằm trong identity map của session thì ORM
        # trả về object CŨ và bỏ qua giá trị RETURNING vừa ghi — hàm này báo
        # thành công kèm số cũ. Mỗi request HTTP một session mới nên đường web
        # không lộ, nhưng hai lần upsert trong CÙNG một session (nhập theo lô,
        # import, hay test) thì lộ ngay.
        .execution_options(populate_existing=True)
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


# --------------------------------------------------------------------------- #
# Thông số lập kế hoạch theo từng bồn
# --------------------------------------------------------------------------- #


def get_settings_for(session: Session, psn: str) -> PlanSetting | None:
    """Thông số đã lưu của một bồn. Chưa lưu gì -> ``None``, KHÔNG phải lỗi."""
    return session.execute(
        select(PlanSetting).where(PlanSetting.psn == psn)
    ).scalar_one_or_none()


def save_settings(
    session: Session,
    psn: str,
    patch: dict[str, object],
    *,
    by: str | None = None,
) -> PlanSetting:
    """Trộn ``patch`` vào thông số đang lưu.

    Trộn chứ không thay thế: trang Kế hoạch lưu theo từng ô người dùng vừa sửa, nên
    thay cả cục sẽ xoá mất các ô khác về NULL. Cùng lý do như ``app_settings.save``.

    ``updated_at`` set tường minh vì ``onupdate`` của SQLAlchemy KHÔNG chạy trên câu
    lệnh Core như ``ON CONFLICT DO UPDATE``.
    """
    values: dict[str, object] = {"psn": psn, "updated_by": by, **patch}
    set_: dict[str, object] = {**patch, "updated_by": by, "updated_at": func.now()}
    stmt = (
        pg_insert(PlanSetting)
        .values(**values)
        .on_conflict_do_update(index_elements=["psn"], set_=set_)
        .returning(PlanSetting)
        # Cùng lý do như ``upsert``: không có cờ này thì lần lưu thứ hai trong
        # cùng một session trả về thông số CŨ.
        .execution_options(populate_existing=True)
    )
    return session.execute(stmt).scalar_one()
