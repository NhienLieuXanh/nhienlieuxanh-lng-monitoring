"""plan_settings: thông số lập kế hoạch lưu theo từng bồn

Thêm SCHEMA, một bảng mới, không chạm bảng nào đang có.

Vì sao cần. Trang Kế hoạch là phần duy nhất còn dùng được khi thiết bị đã chết — nó
không đọc telemetry một dòng nào. Nhưng toàn bộ thông số của nó chỉ sống trong DOM:
mở lại trang là gõ lại sáu con số, mỗi ngày, cho mỗi bồn. Với thứ dùng hằng ngày, đó
là ma sát đủ để người vận hành quay về Excel.

``capacity_l`` CỐ Ý không có trong bảng này. Dung tích bồn đã thuộc
``terminals.capacity_l`` và phải chỉ có MỘT chỗ; hai chỗ giữ dung tích là cách chắc
chắn nhất để chúng lệch nhau — đúng chuyện đang xảy ra giữa số vendor gửi (10425 L)
và dung tích người vận hành thực sự lập kế hoạch với.

Mọi cột nullable: lưu được từng phần, thiếu thì rơi về mặc định của app.

``refill_time`` là TIME chứ không TIMESTAMP: "8 giờ sáng theo giờ kho" là một giờ
trong ngày lặp lại, không phải một thời điểm — nên nó không có múi giờ để chọn sai.

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-08-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plan_settings",
        sa.Column("psn", sa.String(32), nullable=False),
        sa.Column("max_fill_percent", sa.Numeric(6, 3), nullable=True),
        # Lít, khớp với volume_l / capacity_l ở mọi nơi khác.
        sa.Column("daily_use_l", sa.Numeric(18, 3), nullable=True),
        sa.Column("reserve_l", sa.Numeric(18, 3), nullable=True),
        sa.Column("refill_time", sa.Time(), nullable=True),
        sa.Column("horizon_days", sa.SmallInteger(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("updated_by", sa.String(128), nullable=True),
        sa.PrimaryKeyConstraint("psn", name="pk_plan_settings"),
        sa.CheckConstraint(
            "max_fill_percent IS NULL OR (max_fill_percent > 0 AND max_fill_percent <= 100)",
            name="ck_plan_settings_max_fill_percent_range",
        ),
        sa.CheckConstraint(
            "daily_use_l IS NULL OR daily_use_l >= 0",
            name="ck_plan_settings_daily_use_non_negative",
        ),
        sa.CheckConstraint(
            "reserve_l IS NULL OR reserve_l >= 0",
            name="ck_plan_settings_reserve_non_negative",
        ),
        sa.CheckConstraint(
            "horizon_days IS NULL OR (horizon_days >= 1 AND horizon_days <= 62)",
            name="ck_plan_settings_horizon_days_range",
        ),
        sa.ForeignKeyConstraint(
            ["psn"],
            ["terminals.psn"],
            name="fk_plan_settings_psn_terminals",
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
    )


def downgrade() -> None:
    op.drop_table("plan_settings")
