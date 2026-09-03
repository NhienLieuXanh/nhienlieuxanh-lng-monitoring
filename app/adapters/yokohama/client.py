"""HTTP client: chỉ GET. Stream mảng JSON, dừng sớm được từ phía consumer."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from typing import Any

import httpx

from app.adapters.yokohama.config import YokohamaSettings
from app.adapters.yokohama.errors import YokohamaSchemaError, YokohamaTransientError

log = logging.getLogger(__name__)

# Không có method POST trên class này — test soi __dict__. Các endpoint ghi
# (SaveTime, ExportExcelFromTable, ExportToExcel) không được gọi.


class YokohamaClient:
    def __init__(
        self,
        settings: YokohamaSettings,
        client: httpx.Client | None = None,
    ) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=settings.base_url,
            timeout=httpx.Timeout(
                settings.timeout_seconds, connect=settings.connect_timeout_seconds
            ),
            headers={"Accept": "application/json, text/plain, */*"},
            follow_redirects=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            resp = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise YokohamaTransientError(None, str(exc)) from exc
        if resp.status_code >= 500 or resp.status_code == 429:
            raise YokohamaTransientError(resp.status_code, resp.text[:200])
        if resp.status_code >= 400:
            raise YokohamaSchemaError(
                f"GET {path} status={resp.status_code}",
                remediation="kiểm tra URL nguồn đo",
            )
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise YokohamaSchemaError(f"GET {path} không phải JSON: {exc}") from exc

    def iter_record_objects(
        self,
        params: dict[str, Any],
        *,
        path: str = "/Data/GetRecordData",
    ) -> Iterator[dict[str, Any]]:
        """Stream mảng JSON object. Consumer break thì đóng kết nối."""
        t0 = time.monotonic()
        n_bytes = 0
        max_b = self._settings.max_stream_bytes
        max_s = self._settings.max_stream_seconds
        try:
            with self._client.stream("GET", path, params=params) as resp:
                if resp.status_code >= 500 or resp.status_code == 429:
                    raise YokohamaTransientError(resp.status_code, "stream")
                if resp.status_code >= 400:
                    raise YokohamaSchemaError(
                        f"stream {path} status={resp.status_code}"
                    )
                buf = ""
                for chunk in resp.iter_text():
                    n_bytes += len(chunk.encode("utf-8", errors="replace"))
                    if n_bytes > max_b:
                        raise YokohamaSchemaError(
                            f"stream vượt MAX_STREAM_BYTES={max_b}",
                            remediation="dừng sớm hoặc hạ cửa sổ ngày",
                        )
                    if time.monotonic() - t0 > max_s:
                        raise YokohamaSchemaError(
                            f"stream vượt MAX_STREAM_SECONDS={max_s}",
                            remediation="nguồn trả chậm; thử lại sau",
                        )
                    buf += chunk
                    objs, buf = _split_objects(buf)
                    yield from objs
                objs, rest = _split_objects(buf, flush=True)
                yield from objs
                if rest.strip() not in ("", "]", ","):
                    log.debug("ykh: stream remainder=%r", rest[:80])
        except httpx.HTTPError as exc:
            raise YokohamaTransientError(None, str(exc)) from exc


def _split_objects(buf: str, *, flush: bool = False) -> tuple[list[dict[str, Any]], str]:
    """Tách object JSON mức ngoài cùng khỏi buffer text."""
    out: list[dict[str, Any]] = []
    i = 0
    n = len(buf)
    while i < n:
        while i < n and buf[i] in " \t\r\n,[":
            i += 1
        if i >= n:
            break
        if buf[i] == "]":
            return out, ""
        if buf[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        start = i
        j = i
        complete = False
        while j < n:
            c = buf[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        raw = buf[start : j + 1]
                        out.append(json.loads(raw))
                        i = j + 1
                        complete = True
                        break
            j += 1
        if not complete:
            return out, buf[start:]
    return out, "" if flush else buf[i:]
