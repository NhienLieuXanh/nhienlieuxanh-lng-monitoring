"""Thông tin in trên file "Lịch nạp" xuất Excel, lưu theo từng bồn

Người dùng gửi lịch nạp cho đối tác dưới dạng một bảng Excel chỉ gồm các ngày
nạp: đơn vị cung cấp, khách hàng, địa điểm, lượng (tấn) và ngày. Ba cột chữ là
hằng số của TỪNG BỒN (cùng một bồn thì luôn cùng khách hàng, cùng địa điểm), nên
lưu một lần ở ``plan_settings`` thay vì gõ lại mỗi lần xuất.

``m3_per_tonne``: lượng đặt trên trang tính bằng m³, còn đơn hàng LNG tính bằng
tấn. Người dùng: "20 tấn là khoảng 44 m³" -> 2,2 m³/tấn. Để theo bồn chứ không
cứng trong code vì khối lượng riêng LNG đổi theo thành phần của từng nguồn hàng.
NULL = dùng mặc định 2,2 của app.

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-09-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2a3b4c5d6e7"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("plan_settings", sa.Column("supplier_name", sa.String(length=200), nullable=True))
    op.add_column("plan_settings", sa.Column("customer_name", sa.String(length=200), nullable=True))
    op.add_column("plan_settings", sa.Column("site_name", sa.String(length=200), nullable=True))
    op.add_column(
        "plan_settings", sa.Column("m3_per_tonne", sa.Numeric(8, 4), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_plan_settings_m3_per_tonne_range"),
        "plan_settings",
        "m3_per_tonne IS NULL OR (m3_per_tonne > 0 AND m3_per_tonne <= 10)",
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE plan_settings DROP CONSTRAINT IF EXISTS "
        '"ck_plan_settings_m3_per_tonne_range"'
    )
    op.drop_column("plan_settings", "m3_per_tonne")
    op.drop_column("plan_settings", "site_name")
    op.drop_column("plan_settings", "customer_name")
    op.drop_column("plan_settings", "supplier_name")
