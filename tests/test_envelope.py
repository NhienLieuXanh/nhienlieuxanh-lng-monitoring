"""Bóc envelope vendor. Khoá bốn shape đã xác minh trên response thật."""

from __future__ import annotations

import json

import httpx
import pytest

from app.adapters.xingke.envelope import extract_page, unwrap
from app.adapters.xingke.errors import XingkeApiError, XingkeAuthError
from tests.conftest import FIXTURES


def _resp(status: int, body: object) -> httpx.Response:
    return httpx.Response(
        status,
        json=body,
        request=httpx.Request("GET", "http://example.test/x"),
    )


def test_success_is_code_200_not_zero():
    data = unwrap(_resp(200, {"code": 200, "msg": "操作成功", "data": {"ok": True}}))
    assert data == {"ok": True}


def test_http_200_with_code_negative_is_business_error():
    """Fixture thật: HTTP 200, code=-1. Phải xét code, không chỉ HTTP status."""
    payload = json.loads((FIXTURES / "error_code_negative.json").read_text(encoding="utf-8"))
    with pytest.raises(XingkeApiError) as ei:
        unwrap(_resp(200, payload))
    assert ei.value.code == -1


def test_auth_shape_with_success_false():
    body = {
        "msg": "AccessDenied",
        "code": 401,
        "data": None,
        "dataNotEmpty": False,
        "success": False,
    }
    with pytest.raises(XingkeAuthError):
        unwrap(_resp(200, body))


def test_gateway_shape_without_code():
    body = {
        "timestamp": "2026-08-18 15:03:18",
        "path": "/login",
        "status": 405,
        "error": "Method Not Allowed",
        "message": "x",
        "requestId": "1ac81d31",
    }
    with pytest.raises(XingkeApiError) as ei:
        unwrap(_resp(405, body))
    assert ei.value.code == 405


def test_extract_page_uses_content_totalelements():
    rows, total = extract_page({"content": [{"a": 1}], "totalElements": 7})
    assert rows == [{"a": 1}]
    assert total == 7


def test_extract_page_unknown_shape_is_empty_not_crash():
    rows, total = extract_page({"foo": 1})
    assert rows == []
    assert total is None
