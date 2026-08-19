"""Exception handler. Không bao giờ echo chi tiết nội bộ ra client.

Text exception của adapter có thể nhúng URL vendor, PSN, hoặc token. Một handler
mặc định trả str(exc) là đủ để rò tất cả những thứ đó qua một response 500 mà rồi
sẽ bị paste vào chat hoặc ticket.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

# Chuỗi tuyệt đối không được xuất hiện trong response. Kiểm ở tầng handler là lớp
# phòng vệ cuối; lớp chính là response_model tường minh.
_REDACT = ("xk-iot", "xingke", "Bearer ", "password")


def _scrub(text: str) -> str:
    out = text
    for token in _REDACT:
        if token.lower() in out.lower():
            return "chi tiết đã được ẩn"
    return out


def install(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException) -> JSONResponse:
        # HTTPException do ta chủ động raise nên detail an toàn — nhưng vẫn quét,
        # vì một 502 bọc lỗi adapter cũng đi qua đây.
        if isinstance(exc.detail, str):
            exc.detail = _scrub(exc.detail)
        return await http_exception_handler(request, exc)  # type: ignore[return-value]

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        request_id = uuid.uuid4().hex[:12]
        # Trace đầy đủ vào log (nơi ta kiểm soát), chỉ request_id ra client.
        log.exception(
            "unhandled error request_id=%s %s %s",
            request_id, request.method, request.url.path,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal error", "request_id": request_id},
        )
