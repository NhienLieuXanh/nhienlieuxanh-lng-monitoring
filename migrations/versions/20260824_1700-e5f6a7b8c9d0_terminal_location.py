"""terminals: thêm latitude/longitude cho bản đồ vị trí bồn

Thêm SCHEMA, không sửa dữ liệu. Hai cột nullable + bốn CHECK.

Vì sao toạ độ do người nhập chứ không ingest từ vendor: `psn/search` có gửi
``gpsLatitude``/``gpsLongitude``, nhưng cả hai thiết bị pilot đều trả
``0.000000 / 0.000000`` kèm ``gpsAddress = "--"`` — module không có định vị. Nếu
ingest số đó thì bản đồ đặt bồn LNG ở Null Island giữa vịnh Guinea. Bồn là tài sản
cố định tại kho khách hàng nên toạ độ thuộc cấu hình tài sản, giống ``capacity_l``.

Bốn CHECK không phải trang trí:

- ``latlon_paired`` — nửa toạ độ là trạng thái không vẽ được mà cũng không phải
  "chưa khai". Để DB cấm, thay vì tin vào kỷ luật tầng app.
- ``latlon_not_null_island`` — cấm đúng cặp ``0,0``, tức giá trị vendor gửi khi mất
  định vị. Lưới an toàn cho cả người gõ tay và cho một lần import GPS vendor về sau.
- ``latitude_range`` / ``longitude_range`` — bắt lỗi đảo thứ tự lat/lon. Với Việt
  Nam (~10,97 N / 106,75 E) mà nhập ngược thì latitude = 106,75 vượt ±90 và bị chặn
  ngay, thay vì âm thầm đặt bồn ở Siberia.

An toàn khi chạy trên production đang có dữ liệu: cả hai cột NULL cho mọi dòng sẵn
có, nên mọi CHECK pass mà không cần backfill. ``ADD COLUMN`` nullable không rewrite
bảng và không giữ lock lâu.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-08-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECKS = (
    (
        "ck_terminals_latitude_range",
        "latitude IS NULL OR (latitude >= -90 AND latitude <= 90)",
    ),
    (
        "ck_terminals_longitude_range",
        "longitude IS NULL OR (longitude >= -180 AND longitude <= 180)",
    ),
    ("ck_terminals_latlon_paired", "(latitude IS NULL) = (longitude IS NULL)"),
    (
        "ck_terminals_latlon_not_null_island",
        "latitude IS NULL OR latitude <> 0 OR longitude <> 0",
    ),
)


def upgrade() -> None:
    op.add_column("terminals", sa.Column("latitude", sa.Numeric(9, 6), nullable=True))
    op.add_column("terminals", sa.Column("longitude", sa.Numeric(9, 6), nullable=True))
    for name, expr in _CHECKS:
        op.create_check_constraint(name, "terminals", expr)


def downgrade() -> None:
    """Thứ tự ngược: bỏ CHECK trước, rồi bỏ cột.

    Postgres tự bỏ CHECK khi DROP COLUMN, nhưng bỏ tường minh giữ downgrade đọc
    được và không phụ thuộc hành vi ngầm của engine.
    """
    for name, _ in reversed(_CHECKS):
        op.drop_constraint(name, "terminals", type_="check")
    op.drop_column("terminals", "longitude")
    op.drop_column("terminals", "latitude")
