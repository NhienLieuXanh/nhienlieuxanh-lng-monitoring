"""notifications: nhật ký thông báo + chống gửi lại

Viết tay thay vì --autogenerate, cùng lý do như migration đầu: máy phát triển bị
firewall chặn kết nối tới Postgres (Neon), nên autogenerate không so sánh được với
DB thật. Đã đối chiếu byte-level với DDL do
``CreateTable(Notification.__table__).compile(dialect=postgresql.dialect())`` sinh
ra từ model, kể cả tên constraint theo naming convention ở ``app/db/base.py``.

Sau khi deploy, xác nhận không lệch bằng:

    alembic upgrade head
    alembic check          # phải báo "No new upgrade operations detected."

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("psn", sa.String(32), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column(
            "channel",
            sa.String(16),
            server_default=sa.text("'email'"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_notifications"),
        # varchar + CHECK thay vì PG ENUM, giống bảng terminals: thêm giá trị vào
        # enum trong một migration transactional là nỗi đau đã biết, còn CHECK chỉ
        # là một dòng drop + create.
        sa.CheckConstraint(
            "status IN ('sent','failed')",
            name="ck_notifications_notify_status_valid",
        ),
        sa.CheckConstraint(
            "severity IN ('critical','warning','info')",
            name="ck_notifications_notify_severity_valid",
        ),
        # KHÔNG có FK sang terminals: dòng log phải ghi được cả khi PSN chưa được
        # provision, và một lần insert log không được thất bại vì ràng buộc tham
        # chiếu đúng lúc đang có sự cố.
    )
    # Query nóng duy nhất: "lần gửi gần nhất cho (psn, code)". Một backward
    # index-scan trên đúng index này, không cần bản DESC riêng.
    op.create_index(
        "ix_notifications_psn_code_sent_at",
        "notifications",
        ["psn", "code", "sent_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_psn_code_sent_at", table_name="notifications")
    op.drop_table("notifications")
