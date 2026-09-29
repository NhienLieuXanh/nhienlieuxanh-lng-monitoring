"""Ngày không giao hàng (nghỉ lễ), và kế hoạch dài quá 62 ngày

Hai thay đổi, cùng xuất phát từ một lần người dùng lập kế hoạch qua cuối năm:

1. ``plan_day_flags.no_delivery`` — ngày KHÔNG GIAO ĐƯỢC, cùng nghĩa với Chủ Nhật.
   Người dùng tích "Ngày nghỉ" cho ngày lễ 24/11 rồi tích thêm "Nạp chỉ định", và
   màn hình không đổi gì: ô "Ngày nghỉ" chỉ có nghĩa "nhà máy không tiêu thụ",
   chưa bao giờ chặn việc giao hàng; còn 24/11 vốn đã là ngày nạp tự nhiên nên
   "Nạp chỉ định" cũng không đổi gì. Cần một cờ riêng, vì hai chuyện độc lập: nhà
   máy có thể vẫn chạy trong ngày bên giao hàng nghỉ lễ.

2. Trần ``plan_settings.horizon_days`` 62 -> 366. Người dùng gõ 90 ngày để với tới
   Tết dương lịch; trang CHẶN IM LẶNG về 62, nên ô hiện 90 trong khi bảng dừng ở
   ngày thứ 62 và "Tổng cần đặt trong kỳ" là của 62 ngày.

Tên ràng buộc CHECK cũ trong DB bị lặp tiền tố hai lần —
``ck_plan_settings_ck_plan_settings_horizon_days_range`` — đo thẳng từ
``pg_constraint`` trước khi viết file này. Xoá theo tên "đúng quy ước" sẽ nổ trên
production, nên xoá bằng IF EXISTS cho CẢ HAI cách đặt tên rồi tạo lại bằng tên
sạch. Nhân tiện dọn được một chỗ lệch model-vs-DB có sẵn.

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-09-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "d0e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HORIZON_NAMES = (
    "ck_plan_settings_ck_plan_settings_horizon_days_range",
    "ck_plan_settings_horizon_days_range",
)


def _drop_horizon_check() -> None:
    for name in _HORIZON_NAMES:
        op.execute(f'ALTER TABLE plan_settings DROP CONSTRAINT IF EXISTS "{name}"')


def upgrade() -> None:
    # --- 1. ngày không giao
    op.add_column(
        "plan_day_flags",
        sa.Column(
            "no_delivery",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.execute(
        "ALTER TABLE plan_day_flags DROP CONSTRAINT IF EXISTS "
        '"ck_plan_day_flags_flag_not_empty"'
    )
    op.create_check_constraint(
        op.f("ck_plan_day_flags_flag_not_empty"),
        "plan_day_flags",
        "rest OR forced OR no_delivery",
    )

    # --- 2. kế hoạch tới 366 ngày
    _drop_horizon_check()
    op.create_check_constraint(
        op.f("ck_plan_settings_horizon_days_range"),
        "plan_settings",
        "horizon_days IS NULL OR (horizon_days >= 1 AND horizon_days <= 366)",
    )


def downgrade() -> None:
    # MẤT DỮ LIỆU, cố ý và ghi rõ: đánh dấu "không giao" không có chỗ nào để về,
    # và kế hoạch > 62 ngày bị cắt về 62. Workflow production chỉ tiến, không lùi;
    # nhánh này tồn tại cho test round-trip trên DB rỗng.
    op.execute("DELETE FROM plan_day_flags WHERE NOT (rest OR forced)")
    op.execute(
        "ALTER TABLE plan_day_flags DROP CONSTRAINT IF EXISTS "
        '"ck_plan_day_flags_flag_not_empty"'
    )
    op.drop_column("plan_day_flags", "no_delivery")
    op.create_check_constraint(
        op.f("ck_plan_day_flags_flag_not_empty"), "plan_day_flags", "rest OR forced"
    )

    op.execute("UPDATE plan_settings SET horizon_days = 62 WHERE horizon_days > 62")
    _drop_horizon_check()
    op.create_check_constraint(
        op.f("ck_plan_settings_horizon_days_range"),
        "plan_settings",
        "horizon_days IS NULL OR (horizon_days >= 1 AND horizon_days <= 62)",
    )
