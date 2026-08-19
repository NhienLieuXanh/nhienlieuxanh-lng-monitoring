"""Thi hành biên adapter BẰNG MÁY, không bằng kỷ luật.

Ràng buộc cứng của người dùng: "Tách biệt hoàn toàn với vendor (Adapter pattern)"
và "Không phụ thuộc vào field tên của Xingke ở tầng API và Dashboard".

Code review không giữ được luật này qua sáu tháng và ba người. Test thì giữ được.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"

# Những tầng KHÔNG được biết vendor tồn tại. app/factory.py được miễn: nó CHÍNH LÀ
# nơi lắp ráp, và việc chỉ có đúng một file được miễn là điều làm biên này có nghĩa.
PURE_DIRS = ("domain", "services", "repositories", "api", "db")
VENDOR_MARKER = re.compile(r"adapters\.xingke|adapters/xingke", re.I)


def _py_files(rel: str) -> list[Path]:
    return sorted((APP / rel).rglob("*.py"))


@pytest.mark.parametrize("layer", PURE_DIRS)
def test_pure_layers_do_not_import_vendor(layer: str):
    """domain/services/repositories/api/db không được import adapters.xingke."""
    offenders = []
    for f in _py_files(layer):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mod = None
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
            elif isinstance(node, ast.Import):
                mod = ",".join(a.name for a in node.names)
            if mod and VENDOR_MARKER.search(mod):
                offenders.append(f"{f.relative_to(ROOT)}:{node.lineno} -> {mod}")
    assert offenders == [], (
        "Tầng thuần import module vendor — biên adapter bị phá:\n"
        + "\n".join(offenders)
    )


def test_domain_does_not_import_any_adapter():
    """domain là tầng trong cùng: không import cả adapters.fake."""
    for f in _py_files("domain"):
        src = f.read_text(encoding="utf-8")
        assert "app.adapters" not in src, f"{f.relative_to(ROOT)} import app.adapters"


def test_only_factory_selects_concrete_adapter():
    """Đúng MỘT file được phép import adapter cụ thể (ngoài chính adapters/ và cli).

    Nếu con số này tăng thì có người đã bỏ qua factory, và FakeAdapter thôi là
    drop-in.
    """
    importers = []
    for f in APP.rglob("*.py"):
        rel = f.relative_to(APP).as_posix()
        if rel.startswith("adapters/"):
            continue
        src = f.read_text(encoding="utf-8")
        if "XingkeAdapter" in src or "adapters.xingke" in src:
            importers.append(rel)
    assert importers == ["factory.py"], (
        f"chỉ factory.py được chọn adapter cụ thể, nhưng thấy: {importers}"
    )


def test_api_schemas_never_expose_raw_payload_or_source():
    """raw_payload và source là hai vector rò tên vendor.

    raw_payload là JSON vendor nguyên bản (key tiếng Trung/pinyin); source có giá
    trị 'xingke'. Cả hai phải vắng mặt khỏi mọi response model.
    """
    src = (APP / "api" / "schemas.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    banned = {"raw_payload", "source"}
    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        # IngestRunDetailOut chỉ dùng ở /admin/* và cố ý mang mapping_report.
        if node.name == "IngestRunDetailOut":
            continue
        for stmt in node.body:
            if (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.target.id in banned
            ):
                problems.append(f"{node.name}.{stmt.target.id}")
    assert problems == [], f"response model phơi field cấm: {problems}"


def test_dashboard_has_no_vendor_name_and_no_mock():
    static = APP / "static" / "index.html"
    if not static.exists():
        pytest.skip("dashboard chưa được copy vào app/static/")
    src = static.read_text(encoding="utf-8")
    for banned in ("xingke", "Xingke", "xk-iot", "mockTanks", "mockHistory"):
        assert banned not in src, f"dashboard còn chứa {banned!r}"


def test_dashboard_does_not_multiply_percent_by_100():
    """Lỗi nghiêm trọng nhất của prototype: 0.59% hiển thị thành 59%."""
    static = APP / "static" / "index.html"
    if not static.exists():
        pytest.skip("dashboard chưa được copy vào app/static/")
    src = static.read_text(encoding="utf-8")
    assert "volume_percent || 0) * 100" not in src
    assert re.search(r"volume_percent\s*\*\s*100", src) is None
    # Và phải dùng fill_percent (số đối chứng do server tính).
    assert "fill_percent" in src


def test_dashboard_wires_health_and_operator_rename():
    """Dashboard giai đoạn 1 đọc /api/health và PATCH tên — không nhúng admin token."""
    static = APP / "static" / "index.html"
    src = static.read_text(encoding="utf-8")
    assert "/api/health" in src
    assert "/api/auth/login" in src
    assert "id=\"login-gate\"" in src
    assert "method: \"PATCH\"" in src or "method: 'PATCH'" in src
    assert "X-Admin-Token" not in src
    assert "function esc(" in src


def test_openapi_metadata_has_no_vendor_name():
    from app.main import create_app

    spec = create_app().openapi()
    blob = str(spec).lower()
    for banned in ("xingke", "xk-iot"):
        assert banned not in blob, f"OpenAPI schema chứa {banned!r}"
