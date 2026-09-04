"""Xoá dòng YKH-TANK-01 thuộc chuỗi dữ liệu KHÁC

Cổng nguồn trả về HAI chuỗi dữ liệu khác nhau, và định dạng ngày trong REQUEST là
thứ duy nhất chọn chuỗi nào — tham số ``device`` bị bỏ qua hoàn toàn. Đo trực tiếp
2026-09-04, tái lập 3/3 mỗi chiều:

    gửi mm/dd -> Refill Count 70, 53,19 m³, totalizer 1.132k   <- TRANG MAIN của cổng
    gửi dd/mm -> Refill Count 38, 45,58 m³, totalizer   747k

Platform nạp bằng dd/mm từ lúc bật nguồn, nên toàn bộ dữ liệu YKH-TANK-01 đang có
thuộc chuỗi KHÔNG PHẢI bồn mà người vận hành nhìn thấy. Đối chiếu ảnh trang Main
04/09/2026 11:23:14: Volume 53.19 m³, Level 88.65 %, Pressure 4.66 bar, GM
Totalizer 1132428.36 Nm³, Tank Refill Count 70 Times.

Vì sao phải xoá thay vì để lẫn: cùng một PSN chứa hai chuỗi thì thể tích nhảy
45,58 -> 53,19 (một lần nạp GIẢ +7,6 m³) và totalizer nhảy 747k -> 1.132k
(+385.000 Nm³ GIẢ). Dự báo cạn, nhận diện lần nạp và đối chứng tiêu thụ hai chiều
đều đọc trên chuỗi đó, nên một mối nối giả làm sai mọi con số phía sau nó.

``terminals.last_seen_at`` cũng phải trả về NULL: cột đó có guard monotonic nên
xoá telemetry không tự hạ nó xuống, và một bồn khai "vừa báo xong" mà không còn
dòng telemetry nào là đúng loại tín hiệu dối mà dự án này đang dọn.

KHÔNG PHỤC HỒI ĐƯỢC. ``downgrade()`` không dựng lại được dữ liệu đã xoá: nguồn
không cho backfill xa (nó chỉ stream từ bản ghi mới nhất), và các dòng đó thuộc
một chuỗi mà chính ta không xác định được là thiết bị nào. Đã được chủ dữ liệu
đồng ý tường minh trước khi viết migration này.

Chỉ chạm ĐÚNG một PSN. Dữ liệu Xingke không bị ảnh hưởng.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9d0e1f2a3b4"
down_revision: str | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PSN = "YKH-TANK-01"


def upgrade() -> None:
    conn = op.get_bind()
    n = conn.execute(
        sa.text("DELETE FROM telemetry WHERE psn = :psn"), {"psn": PSN}
    ).rowcount
    # In ra để lần chạy trên GitHub Actions có con số kiểm chứng được, thay vì
    # phải tin rằng nó đã làm đúng việc.
    print(f"da xoa {n} dong telemetry cua {PSN}")

    m = conn.execute(
        sa.text(
            "UPDATE terminals SET last_seen_at = NULL, status = 'offline', "
            "updated_at = now() WHERE psn = :psn"
        ),
        {"psn": PSN},
    ).rowcount
    print(f"da tra last_seen_at ve NULL cho {m} terminal")


def downgrade() -> None:
    # Cố ý no-op, không phải quên. Xem docstring: dữ liệu đã xoá không có nguồn nào
    # dựng lại được.
    pass
