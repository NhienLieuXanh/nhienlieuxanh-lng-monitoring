"""telemetry: extra measure columns; vendor_alarms

Cột mới đều nullable, không DEFAULT — Postgres 11+ ADD COLUMN metadata-only,
không rewrite bảng đang có dữ liệu Xingke.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8c9d0e1f2a3"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLS = (
    "gm_totalizer_nm3",
    "gm_flow_rate_nm3h",
    "gm_pressure_kpa",
    "gm_temperature_c",
    "ps1_bar",
    "ps2_bar",
    "gd1_percent",
    "gd2_percent",
    "gd3_percent",
)


def upgrade() -> None:
    for name in _COLS:
        op.add_column("telemetry", sa.Column(name, sa.Numeric(18, 6), nullable=True))
    op.add_column("telemetry", sa.Column("refill_counter", sa.Integer(), nullable=True))

    op.create_table(
        "vendor_alarms",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("site_code", sa.String(16), nullable=False),
        sa.Column("device_id", sa.String(64), nullable=False),
        sa.Column("raised_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("vendor_ts_raw", sa.String(64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("message_hash", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_vendor_alarms"),
        sa.UniqueConstraint(
            "site_code",
            "device_id",
            "raised_at",
            "message_hash",
            name="uq_vendor_alarms_natural",
        ),
    )
    op.create_index(
        "ix_vendor_alarms_site_raised_at",
        "vendor_alarms",
        ["site_code", "raised_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_vendor_alarms_site_raised_at", table_name="vendor_alarms")
    op.drop_table("vendor_alarms")
    op.drop_column("telemetry", "refill_counter")
    for name in reversed(_COLS):
        op.drop_column("telemetry", name)
