"""Client nguồn đo phút: chỉ GET, stream tách object, trần byte."""

from __future__ import annotations

import inspect
import json

import httpx
import pytest
from pydantic import ValidationError

from app.adapters.yokohama.client import YokohamaClient, _split_objects
from app.adapters.yokohama.config import YokohamaSettings
from app.adapters.yokohama.errors import YokohamaSchemaError


def test_enabled_without_url_is_config_error() -> None:
    with pytest.raises(ValidationError, match="YOKOHAMA_BASE_URL"):
        YokohamaSettings(enabled=True, base_url="")


def test_client_has_no_post() -> None:
    src = inspect.getsource(YokohamaClient)
    assert "def post" not in src
    assert "SaveTime" not in src
    assert "ExportExcel" not in src


def test_split_objects_from_chunks() -> None:
    payload = json.dumps(
        [
            {"dateTime": "27/08/2026 12:04", "tankPrecent": 53.58},
            {"dateTime": "27/08/2026 12:03", "tankPrecent": 53.58},
        ]
    )
    objs, rest = _split_objects(payload[:40])
    more, rest2 = _split_objects(rest + payload[40:], flush=True)
    all_objs = objs + more
    assert len(all_objs) == 2
    assert all_objs[0]["dateTime"].startswith("27/08/2026")
    assert rest2 in ("", "]")


def test_stream_yields_then_respects_byte_cap() -> None:
    body = json.dumps([{"n": i} for i in range(50)])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    settings = YokohamaSettings(
        enabled=True,
        base_url="https://example.test/",
        max_stream_bytes=64,
        max_stream_seconds=5,
    )
    http = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=settings.base_url,
    )
    client = YokohamaClient(settings, client=http)
    with pytest.raises(YokohamaSchemaError, match="MAX_STREAM_BYTES"):
        list(client.iter_record_objects({"device": "all"}))
