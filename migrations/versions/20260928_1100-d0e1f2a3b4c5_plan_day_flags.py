"""Cờ "ngày nghỉ" và "nạp chỉ định" của trang Kế hoạch, lưu ở server

Trước đây hai cờ này là hai ``Set`` trong bộ nhớ trang: tải lại trang là mất, và
hai người cùng theo một bồn thấy hai kế hoạch khác nhau. Chúng quyết định NGÀY
ĐẶT HÀNG nên không được sống trong một tab trình duyệt.

Bảng riêng chứ không nới ``plan_readings``, dù cùng hạt (psn, ngày): một *số đo*
luôn có một con số, và nhồi cờ vào đó buộc ``volume_l`` thành nullable, tức cho
phép "số đo tay không có số". Trang Kế hoạch dựng Map rồi hỏi ``has(key)``, nên
một dòng như vậy trả true kèm null và làm hỏng cả chuỗi số học phía sau.

CHECK ``rest OR forced`` để không có dòng rỗng; repository xoá dòng khi người
dùng bỏ cả hai tích. Cả hai cùng true là hợp lệ: ngày xưởng nghỉ vẫn có thể có xe
tới giao.

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-09-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d0e1f2a3b4c5"
down_revision: str | None = "c9d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plan_day_flags",
        sa.Column("psn", sa.String(length=32), nullable=False),
        sa.Column("flag_date", sa.Date(), nullable=False),
        sa.Column(
            "rest", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "forced", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("updated_by", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("rest OR forced", name=op.f("ck_plan_day_flags_flag_not_empty")),
        sa.ForeignKeyConstraint(
            ["psn"],
            ["terminals.psn"],
            name=op.f("fk_plan_day_flags_psn_terminals"),
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("psn", "flag_date", name=op.f("pk_plan_day_flags")),
    )


def downgrade() -> None:
    op.drop_table("plan_day_flags")
