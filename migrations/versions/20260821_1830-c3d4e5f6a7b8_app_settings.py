"""app_settings: cấu hình vận hành đặt được trong app

Viết tay, đã đối chiếu với DDL do
``CreateTable(AppSetting.__table__).compile(dialect=postgresql.dialect())`` sinh ra
từ model (máy phát triển bị firewall chặn kết nối Postgres tới Neon nên
--autogenerate không so sánh được với DB thật).

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        # JSONB thay vì cột rời từng setting: tập setting còn mọc thêm, và mỗi lần
        # thêm một ô trong trang Cài đặt mà phải viết migration là ma sát vô ích.
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.String(128), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_app_settings"),
        # Một dòng duy nhất. Không có CHECK này thì sớm muộn sẽ có hai bản cấu hình
        # và không ai biết bản nào đang có hiệu lực.
        sa.CheckConstraint("id = 1", name="ck_app_settings_single_row"),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
