"""Literal trong schema API phải KHÔNG BAO GIỜ lệch với enum trong domain.

Lỗi thật, 2026-09-04: domain thêm hạng dữ liệu "chưa đủ lịch sử", còn
``QualityOut.grade`` vẫn là bản Literal cũ chép tay. Kết quả là pydantic từ chối
chính giá trị mà domain vừa sinh ra, và ``GET /api/analytics?window_days=90`` trả
500 ngay khi một bồn rơi vào hạng mới — người dùng thấy một dòng
"500 — Internal error" thay cho cả trang Phân tích.

Không test nào bắt được vì không bài nào chạy endpoint với một bồn ở hạng đó. Nên
file này KHÔNG kiểm một giá trị, nó kiểm QUAN HỆ: mọi giá trị domain sinh ra được
thì schema phải nhận. Thêm một hạng mới vào domain mà quên schema là test đỏ ngay,
không phải 500 trên production.
"""

from __future__ import annotations

import typing

import pytest

from app.api import schemas as S
from app.domain import analytics as A

# (tên field, model API, alias kiểu trong domain)
BOUND = [
    ("grade", S.QualityOut, A.Grade),
    ("risk", S.DeviceHealthOut, A.Risk),
    ("kind", S.AnomalyOut, A.AnomalyKind),
]


@pytest.mark.parametrize("field,model,domain_alias", BOUND)
def test_literal_schema_trung_khop_domain(field, model, domain_alias) -> None:
    schema_vals = typing.get_args(model.model_fields[field].annotation)
    domain_vals = typing.get_args(domain_alias)
    assert domain_vals, f"{domain_alias} phải là một Literal có giá trị"
    assert schema_vals == domain_vals, (
        f"{model.__name__}.{field} lệch domain.\n"
        f"  schema: {list(schema_vals)}\n"
        f"  domain: {list(domain_vals)}\n"
        "Đừng chép tay danh sách — import alias kiểu từ app.domain."
    )


@pytest.mark.parametrize("field,model,domain_alias", BOUND)
def test_moi_gia_tri_domain_qua_duoc_pydantic(field, model, domain_alias) -> None:
    """Kiểm ở tầng pydantic, không chỉ tầng typing.

    ``get_args`` khớp nhau vẫn có thể sai nếu ai đó bọc thêm một validator; đây là
    bài chứng minh giá trị THẬT đi qua được.
    """
    for v in typing.get_args(domain_alias):
        model.__pydantic_validator__.validate_assignment(
            model.model_construct(), field, v
        )


def test_hang_du_lieu_moi_qua_duoc_endpoint_phan_tich() -> None:
    """Ca cụ thể đã gây 500: hạng "chưa đủ lịch sử" phải dựng được QualityOut."""
    q = S.QualityOut(
        samples=76,
        window_days=90.0,
        cadence_minutes=30.0,
        cadence_jitter_minutes=0.0,
        expected_samples=4320,
        coverage=0.02,
        gaps=0,
        longest_gap_hours=0.5,
        flatline_runs=0,
        longest_flatline_hours=None,
        grade="chưa đủ lịch sử",
        reasons=[],
    )
    assert q.grade == "chưa đủ lịch sử"
