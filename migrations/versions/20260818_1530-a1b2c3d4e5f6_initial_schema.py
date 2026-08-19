"""initial schema: terminals, telemetry, ingest_runs

Viết tay thay vì --autogenerate vì autogenerate cần một DB đang chạy để so sánh, và
PostgreSQL chưa được cài lúc tạo file này. Đã đối chiếu với DDL do
``CreateTable(...).compile(dialect=postgresql.dialect())`` sinh ra từ model, nên nó
khớp chính xác ``Base.metadata``.

Sau khi cài Postgres, xác nhận không lệch bằng:

    alembic upgrade head
    alembic check          # phải báo "No new upgrade operations detected."

Revision ID: a1b2c3d4e5f6
Revises:
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MEASURE = sa.Numeric(18, 6)


def upgrade() -> None:
    op.create_table(
        "terminals",
        # gen_random_uuid() là core từ Postgres 13 — không cần CREATE EXTENSION pgcrypto.
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("psn", sa.String(32), nullable=False),
        sa.Column("name", sa.String(128), nullable=True),
        sa.Column("modem_number", sa.String(64), nullable=True),
        sa.Column("sim_iccid", sa.String(32), nullable=True),
        sa.Column("hardware_version", sa.String(64), nullable=True),
        sa.Column("software_version", sa.String(64), nullable=True),
        sa.Column("device_model", sa.String(64), nullable=True),
        sa.Column("device_type_name", sa.String(64), nullable=True),
        sa.Column("medium_name", sa.String(64), nullable=True),
        sa.Column("tank_type_name", sa.String(64), nullable=True),
        sa.Column("capacity_l", sa.Numeric(18, 3), nullable=True),
        sa.Column(
            "status",
            sa.String(16),
            server_default=sa.text("'offline'"),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_terminals"),
        # varchar + CHECK thay vì PG ENUM: sửa danh sách giá trị chỉ là drop + create.
        sa.CheckConstraint(
            "status IN ('online','offline')", name="status_valid"
        ),
        sa.CheckConstraint(
            "capacity_l IS NULL OR capacity_l > 0",
            name="capacity_positive",
        ),
        sa.UniqueConstraint("psn", name="uq_terminals_psn"),
        # Target cho composite FK của telemetry: Postgres đòi một UNIQUE khớp đúng
        # cặp cột được tham chiếu.
        sa.UniqueConstraint("id", "psn", name="uq_terminals_id_psn"),
    )

    op.create_table(
        "telemetry",
        # id KHÔNG unique và KHÔNG phải PK — chỉ là tie-breaker đơn điệu cho keyset
        # pagination sau này. Identity() thay cho bigserial.
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("terminal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("psn", sa.String(32), nullable=False),
        # Luôn UTC. Vendor gửi naive string render ở UTC+8; adapter gắn tz rồi convert.
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        # String gốc của vendor: biến việc sửa timezone về sau thành một UPDATE
        # re-derive thuần SQL, không cần fetch lại vendor.
        sa.Column("vendor_ts_raw", sa.String(64), nullable=True),
        sa.Column("level_mmwc", _MEASURE, nullable=True),
        sa.Column("diff_pressure_kpa", _MEASURE, nullable=True),
        sa.Column("pressure_mpa", _MEASURE, nullable=True),
        sa.Column("volume_l", _MEASURE, nullable=True),
        # Thang 0-100 (0.59 = 0.59% đầy). Không CHECK nào bắt được lỗi thang vì 0.59
        # hợp lệ ở cả 0-1 và 0-100 — API phát kèm fill_percent làm đối chứng.
        sa.Column("volume_percent", _MEASURE, nullable=True),
        sa.Column("volume_percent_source", sa.String(16), nullable=True),
        sa.Column("temperature_c", _MEASURE, nullable=True),
        sa.Column("vacuum_pa", _MEASURE, nullable=True),
        sa.Column("signal_percent", _MEASURE, nullable=True),
        sa.Column("battery_v", _MEASURE, nullable=True),
        sa.Column("medium_name", sa.String(64), nullable=True),
        sa.Column("tank_type_name", sa.String(64), nullable=True),
        sa.Column(
            "raw_payload",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "source",
            sa.String(32),
            server_default=sa.text("'xingke'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # PK là (psn, sampled_at): thoả hợp đồng uniqueness bằng MỘT index thay vì
        # hai, và TimescaleDB-ready (create_hypertable đòi cột phân vùng có trong PK
        # và mọi unique index).
        sa.PrimaryKeyConstraint("psn", "sampled_at", name="pk_telemetry"),
        # FK COMPOSITE: làm cho việc psn lệch khỏi terminal_id trở thành KHÔNG THỂ,
        # thay vì dựa vào kỷ luật tầng app.
        sa.ForeignKeyConstraint(
            ["terminal_id", "psn"],
            ["terminals.id", "terminals.psn"],
            name="fk_telemetry_terminal_id_psn_terminals",
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
    )
    # Postgres KHÔNG tự index cột referencing của FK; không có index này thì mỗi
    # UPDATE/DELETE trên terminals phải seq scan cả telemetry.
    op.create_index(
        "ix_telemetry_terminal_id_sampled_at",
        "telemetry",
        ["terminal_id", "sampled_at"],
    )

    op.create_table(
        "ingest_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "inserted", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "duplicates", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "terminals_created",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "error_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column(
            "params",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "mapping_report",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ingest_runs"),
        sa.CheckConstraint(
            "status IN ('success','partial','failed')",
            name="status_valid",
        ),
        sa.CheckConstraint(
            "trigger IN ('scheduler','cli','api')",
            name="trigger_valid",
        ),
    )
    # Truy vấn nóng duy nhất: "lần ingest thành công gần nhất" cho /api/health.
    op.create_index(
        "ix_ingest_runs_status_finished_at",
        "ingest_runs",
        ["status", "finished_at"],
    )


def downgrade() -> None:
    # Thứ tự NGƯỢC: telemetry tham chiếu terminals nên phải drop trước.
    op.drop_index("ix_ingest_runs_status_finished_at", table_name="ingest_runs")
    op.drop_table("ingest_runs")
    op.drop_index("ix_telemetry_terminal_id_sampled_at", table_name="telemetry")
    op.drop_table("telemetry")
    op.drop_table("terminals")
