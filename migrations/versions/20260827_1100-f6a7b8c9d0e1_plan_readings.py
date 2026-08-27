"""plan_readings: bảng thể tích đo tay cho trang Kế hoạch

Thêm SCHEMA, không sửa dữ liệu. Một bảng mới, không chạm bảng nào đang có — nên
chạy được trên production đang có dữ liệu mà không backfill, không lock bảng cũ.

Vì sao là bảng riêng chứ không phải cột thêm vào ``telemetry``: ``telemetry`` có
đúng một đường ghi là ingestion từ vendor, và mọi con số "đo được" của hệ thống
(mức tiêu thụ, nhận diện lần nạp, cảnh báo, báo cáo) đọc từ đó. Cho một form web
ghi vào cùng bảng thì không còn ai phân biệt được số máy với số người. Phạm vi đã
chốt với người dùng: số nhập tay chỉ dùng cho trang Kế hoạch.

PK ``(psn, reading_date)`` là khoá tự nhiên — một bồn một ngày một số đo. Nhờ đó
"nhập lại số của hôm nay" là UPSERT chứ không sinh dòng trùng thứ hai, và ràng
buộc duy nhất tốn một index thay vì hai. Cùng lập luận như PK của ``telemetry``.

``reading_date`` là DATE chứ không phải timestamptz: kế hoạch làm việc theo ngày
lịch Việt Nam ("thể tích đầu ngày"), nên hạ granularity xuống ngày là cách duy
nhất không phải chọn múi giờ. Dự án này đã có ba múi giờ để nhầm (vendor UTC+8,
công ty UTC+7, lưu UTC) — đừng thêm chỗ thứ tư.

FK ``ON DELETE RESTRICT`` theo đúng luật của ``telemetry``: không đường nào trong
app xoá terminal, và nếu về sau có thì mất im lặng dữ liệu người nhập tay là kết
cục tệ nhất. ``ON UPDATE CASCADE`` để còn sửa được một PSN gõ sai.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-08-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plan_readings",
        sa.Column("psn", sa.String(32), nullable=False),
        sa.Column("reading_date", sa.Date(), nullable=False),
        # Lít, khớp với volume_l / capacity_l ở mọi nơi khác. Trang Kế hoạch quy
        # đổi sang m³ ở biên UI, không ở đây.
        sa.Column("volume_l", sa.Numeric(18, 3), nullable=False),
        sa.Column("entered_by", sa.String(128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("psn", "reading_date", name="pk_plan_readings"),
        # 0 hợp lệ: bồn cạn là số đo thật, và là lúc cần nhập tay nhất. Âm thì vô nghĩa.
        sa.CheckConstraint(
            "volume_l >= 0", name="ck_plan_readings_volume_non_negative"
        ),
        sa.ForeignKeyConstraint(
            ["psn"],
            ["terminals.psn"],
            name="fk_plan_readings_psn_terminals",
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
    )


def downgrade() -> None:
    # Bảng mới nên downgrade là một lệnh — constraint đi cùng bảng.
    op.drop_table("plan_readings")
