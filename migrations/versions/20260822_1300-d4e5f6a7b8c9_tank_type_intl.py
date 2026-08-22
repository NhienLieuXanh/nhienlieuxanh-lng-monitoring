"""tank_type_name: đổi giá trị vendor tiếng Trung sang tên chuẩn quốc tế

Sửa DỮ LIỆU, không sửa schema.

Vendor gửi ``tankTypeName`` bằng chữ Trung và giá trị đó đi thẳng qua API lên màn
hình vận hành — production đang hiển thị ``立式`` cho người dùng Việt Nam. Phép dịch
đã được thêm ở ranh giới adapter (``VALUE_TRANSLATIONS`` trong
``app/adapters/xingke/mapping.py``) nên mọi lần ingest sau này đã đúng, nhưng:

- ``sync_terminals`` dùng ``COALESCE`` nên chỉ điền vào chỗ đang NULL — giá trị cũ
  trong ``terminals`` sẽ KHÔNG bao giờ bị ghi đè.
- ``telemetry`` là lịch sử bất biến, không lần ingest nào chạm lại dòng cũ. Và
  ``/api/telemetry/{psn}/latest`` đọc ``tank_type_name`` của chính dòng telemetry,
  nên chỉ sửa ``terminals`` là vẫn còn rò.

Vì vậy phải UPDATE cả hai bảng, một lần, ở đây.

Nhận diện theo ĐÚNG chuỗi đã biết, không quét chữ Hán bằng regex: một tên do người
vận hành tự đặt cũng có thể chứa chữ Hán, và migration không được đoán hộ họ. Giá trị
lạ (nếu vendor thêm loại bồn mới) sẽ tự nổi lên qua ``ingest_runs.mapping_report``
nhờ nhánh cảnh báo trong ``extract_text``.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-08-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Giữ khớp với VALUE_TRANSLATIONS["tank_type_name"] trong mapping.py.
TRANSLATIONS = (
    ("立式", "Vertical"),
    ("卧式", "Horizontal"),
)
TABLES = ("terminals", "telemetry")


def _swap(pairs: Sequence[tuple[str, str]]) -> None:
    """UPDATE có tham số hoá — chuỗi vendor không bao giờ nối thẳng vào SQL."""
    conn = op.get_bind()
    for table in TABLES:
        for src, dst in pairs:
            conn.execute(
                sa.text(
                    f"UPDATE {table} SET tank_type_name = :dst "
                    "WHERE tank_type_name = :src"
                ),
                {"src": src, "dst": dst},
            )


def upgrade() -> None:
    _swap(TRANSLATIONS)


def downgrade() -> None:
    """Đổi ngược về chuỗi vendor.

    Đảo được thật: cặp dịch là 1:1, và ``Vertical``/``Horizontal`` không phải giá trị
    vendor từng gửi nên không có dòng nào bị đổi oan.
    """
    _swap([(dst, src) for src, dst in TRANSLATIONS])
