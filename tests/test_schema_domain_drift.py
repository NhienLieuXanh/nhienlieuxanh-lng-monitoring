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
from app.domain import forecast as F
from app.domain.contracts import TerminalStatus

# (tên field, model API, alias kiểu trong domain)
#
# Quét toàn bộ schemas.py sau khi sửa ca đầu tiên tìm ra SÁU chỗ nữa cùng lỗi:
# confidence, method (2 chỗ), verdict, urgency (2 chỗ) — mỗi cái là một 500 đang
# chờ ngày ai đó thêm một giá trị vào enum domain. Chúng đều đã đổi sang import
# alias, và danh sách dưới đây là thứ giữ cho chúng không quay lại.
BOUND = [
    ("grade", S.QualityOut, A.Grade),
    ("risk", S.DeviceHealthOut, A.Risk),
    ("kind", S.AnomalyOut, A.AnomalyKind),
    ("confidence", S.ConsumptionOut, F.Confidence),
    ("method", S.IdleTrendOut, F.Method),
    ("method", S.HoldTimeOut, F.Method),
    ("verdict", S.GasCrossCheckOut, F.DualVerdict),
    ("urgency", S.SuggestionOut, F.Urgency),
    ("urgency", S.DeliveryStopOut, F.Urgency),
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


# ---------------------------------------------------------------------------
# StrEnum thì KHÔNG đổi sang import: pydantic serialize enum khác serialize str,
# và đổi kiểu ở đây sẽ đổi JSON mà không ai yêu cầu. Nhưng tập giá trị vẫn phải
# khớp, nên đặt tripwire thay vì đổi kiểu.
# ---------------------------------------------------------------------------


def test_status_literal_khop_TerminalStatus() -> None:
    vals = typing.get_args(S.TerminalOut.model_fields["status"].annotation)
    assert set(vals) == {m.value for m in TerminalStatus}, (
        "Literal status trong schema lệch TerminalStatus. Thêm một trạng thái vào "
        "enum mà quên schema là 500 trên mọi endpoint trả TerminalOut."
    )


def test_severity_literal_khop_Severity() -> None:
    from app.domain.alerts import Severity

    vals = typing.get_args(S.AlertOut.model_fields["severity"].annotation)
    assert set(vals) == {m.value for m in Severity}
