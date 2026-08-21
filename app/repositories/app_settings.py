"""Đọc/ghi bảng app_settings (một dòng duy nhất)."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import AppSetting

log = logging.getLogger(__name__)

ROW_ID = 1


def load(session: Session) -> dict[str, Any]:
    """Cấu hình đang lưu. Bảng rỗng -> ``{}``, KHÔNG phải lỗi.

    Bảng rỗng là trạng thái bình thường của một hệ thống chưa ai vào Cài đặt lần
    nào: lúc đó mọi giá trị lấy từ .env. Nên hàm này không bao giờ raise vì thiếu
    dòng.
    """
    row = session.execute(
        select(AppSetting.data).where(AppSetting.id == ROW_ID)
    ).scalar_one_or_none()
    return dict(row) if row else {}


def save(session: Session, patch: dict[str, Any], *, by: str | None = None) -> dict[str, Any]:
    """Trộn ``patch`` vào cấu hình đang lưu rồi trả về bản đầy đủ.

    Trộn (merge) chứ không thay thế: trang Cài đặt gửi lên đúng những ô người dùng
    vừa sửa. Nếu thay thế cả cục thì một form chỉ có phần Email sẽ âm thầm xoá mọi
    thiết lập ở phần Vận hành.

    Giá trị ``None`` trong ``patch`` nghĩa là **xoá override đó** (trả field về giá
    trị .env), khác với "không gửi field đó lên" (giữ nguyên). Phân biệt được hai
    ý này là lý do dùng merge tường minh ở tầng Python thay vì ``data || :patch``
    của Postgres — toán tử đó coi null là một giá trị cần ghi.
    """
    current = load(session)
    for k, v in patch.items():
        if v is None:
            current.pop(k, None)
        else:
            current[k] = v

    stmt = pg_insert(AppSetting).values(id=ROW_ID, data=current, updated_by=by)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=["id"],
            # UPDATE thô KHÔNG kích hoạt onupdate=func.now() của SQLAlchemy, nên
            # updated_at phải set tường minh — đúng với mọi bulk UPDATE trong repo này.
            set_={"data": current, "updated_by": by, "updated_at": func.now()},
        )
    )
    session.flush()
    return current


def meta(session: Session) -> tuple[Any, Any]:
    """(updated_at, updated_by) để trang Cài đặt hiện 'ai sửa lần cuối, lúc nào'."""
    row = session.execute(
        select(AppSetting.updated_at, AppSetting.updated_by).where(
            AppSetting.id == ROW_ID
        )
    ).one_or_none()
    return (row[0], row[1]) if row else (None, None)
