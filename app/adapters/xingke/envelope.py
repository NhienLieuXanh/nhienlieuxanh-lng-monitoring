"""Bóc envelope và phân trang của Xingke.

Vendor có BỐN response shape khác nhau, tất cả đã xác minh trên response thật
ngày 2026-08-18 (xem DISCOVERY.md §2):

  (a) thành công      {"code":200,"msg":"操作成功","data":{...}}
                      -> CHỈ code/data/msg. KHÔNG có `success`, KHÔNG `dataNotEmpty`.
  (b) lỗi auth        {"msg":"AccessDenied","code":401,"data":null,
                       "dataNotEmpty":false,"success":false}
  (c) lỗi validation  {"code":-1,"msg":"Required Long parameter 'id' is not present"}
                      -> HTTP 200 nhưng code âm.
  (d) lỗi gateway     {"timestamp":...,"path":...,"status":405,"error":...,
                       "requestId":...}   -> KHÔNG có `code`.

Ba kết luận không hiển nhiên:
  * Thành công là code == 200, KHÔNG phải code == 0. Plan gốc giả thiết `code==0`;
    theo nó thì MỌI request thành công đều bị raise.
  * Không được dựa vào `success` có mặt — nó vắng trên response thành công (a).
  * Phải xét `code`, không được chỉ xét HTTP status — xem (c).
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from app.adapters.xingke.errors import (
    XingkeApiError,
    XingkeAuthError,
    XingkeProtocolError,
    XingkeTransientError,
)

log = logging.getLogger(__name__)

# 200 đã xác minh. Giữ 0 làm bảo hiểm cho endpoint khác dùng convention khác —
# rẻ và không gây hại.
SUCCESS_CODES: frozenset[int] = frozenset({200, 0})
AUTH_CODES: frozenset[int] = frozenset({401, 403})
AUTH_MSG_RE = re.compile(
    r"accessdenied|token|unauthor|forbidden|登录|登陆|失效|过期|未授权", re.I
)

_logged_shapes: set[str] = set()


def _log_once(key: str, msg: str, **kw: Any) -> None:
    if key not in _logged_shapes:
        _logged_shapes.add(key)
        log.info("%s %s", msg, kw or "")


def unwrap(resp: httpx.Response) -> Any:
    """Trả về ``data``, hoặc raise exception phân loại đúng."""
    if resp.status_code in (401, 403):
        raise XingkeAuthError(resp.status_code, resp.text[:200])
    if resp.status_code == 429 or resp.status_code >= 500:
        raise XingkeTransientError(resp.status_code, resp.text[:200])

    try:
        body = resp.json()
    except Exception as exc:
        raise XingkeProtocolError(
            f"body không phải JSON (HTTP {resp.status_code}): {resp.text[:200]!r}"
        ) from exc

    if not isinstance(body, dict):
        return body

    # (d) shape của Spring Cloud Gateway: có `status`, không có `code`.
    if "code" not in body and "status" in body:
        status = body.get("status")
        if status in (401, 403):
            raise XingkeAuthError(status, str(body.get("error", "")))
        raise XingkeApiError(status, str(body.get("error", "")), body)

    code = body.get("code")
    msg = str(body.get("msg") or "")

    # `success` chỉ tin được KHI CÓ MẶT. Trên response thành công nó vắng, nên
    # `is True` không dùng được một mình.
    explicit_ok = body.get("success") is True
    explicit_fail = body.get("success") is False
    ok = explicit_ok or (not explicit_fail and code in SUCCESS_CODES)

    if not ok:
        if code in AUTH_CODES or AUTH_MSG_RE.search(msg):
            raise XingkeAuthError(code, msg)
        raise XingkeApiError(code, msg, body)

    return body.get("data")


# Shape đã xác minh của vendor đứng đầu. Các shape sau là bảo hiểm cho endpoint
# khác / thay đổi tương lai: cùng triết lý với alias của field — thứ đã biết
# trước, phương án khả dĩ sau, warning to nếu không khớp cái nào.
_PAGE_SHAPES: tuple[tuple[str, str], ...] = (
    ("content", "totalElements"),  # ĐÃ XÁC MINH cho API này
    ("records", "total"),          # MyBatis-Plus IPage
    ("list", "total"),
    ("rows", "total"),
    ("items", "total"),
)


def _as_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def extract_page(data: Any) -> tuple[list[dict[str, Any]], int | None]:
    """Bóc (rows, total) khỏi payload phân trang.

    Degrade về 0 dòng kèm warning thay vì KeyError giữa lúc ingest.
    """
    if isinstance(data, list):
        return data, len(data)
    if not isinstance(data, dict):
        return [], 0

    for rows_key, total_key in _PAGE_SHAPES:
        rows = data.get(rows_key)
        if isinstance(rows, list):
            if rows_key != "content":
                _log_once(
                    f"page_shape:{rows_key}",
                    "xingke: page shape khác dự kiến",
                    rows_key=rows_key,
                )
            return rows, _as_int(data.get(total_key))

    log.warning(
        "xingke: KHÔNG nhận dạng được page shape, coi như 0 dòng. keys=%s",
        sorted(data),
    )
    return [], None
