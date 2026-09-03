"""Bốn cách nguồn đo phút hỏng mà KHÔNG tầng nào thấy được.

Đo trên production ngày 2026-09-03: bật nguồn xong, một cycle chạy 18,7 s, gọi
mạng thật 8,5 s, và trả về ``by_source.ykh.n_rows=0``, ``rejected_rows=0``,
``unmapped_keys=[]``, ``error_count=0``, ``error_summary=null``. Nói cách khác:
một nguồn hoàn toàn không đưa về dữ liệu nào trông y hệt một nguồn khoẻ đang rảnh.

Nguyên nhân là ba lỗ im lặng cùng loại:
  * ``_split_objects`` bỏ qua mọi byte không phải "{" -> body không phải JSON cho
    ra 0 object, 0 lỗi.
  * client chỉ kiểm ``>= 500``, ``== 429``, ``>= 400`` -> 3xx là "thành công rỗng".
  * ``fetch_alarms`` có ``payload if isinstance(payload, list) else []`` -> một
    JSON object lạ thành "hôm nay không có báo động".

Bài quan trọng nhất trong file này là ``test_mang_rong_that_khong_bi_bao_oan``:
một bản sửa biến ngày rỗng thật thành lỗi sẽ tệ hơn cái nó sửa.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from app.adapters.yokohama.adapter import YokohamaAdapter
from app.adapters.yokohama.client import YokohamaClient
from app.adapters.yokohama.config import YokohamaSettings
from app.adapters.yokohama.errors import YokohamaSchemaError

RECORD = {"dateTime": "27/08/2026 12:04", "tankPrecent": 53.58}


def _client(handler, **kw) -> YokohamaClient:
    settings = YokohamaSettings(
        enabled=True, base_url="https://example.test/", **kw
    )
    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=settings.base_url
    )
    return YokohamaClient(settings, client=http)


def _stream(handler) -> list[dict]:
    return list(_client(handler).iter_record_objects({"device": "all"}))


# ---------------------------------------------------------------- 3xx


def test_stream_302_khong_phai_thanh_cong_rong() -> None:
    """Chuyển hướng tới trang đăng nhập từng đi thẳng qua mọi lớp kiểm."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "/Account/Login"}, text="")

    with pytest.raises(YokohamaSchemaError, match="redirect"):
        _stream(handler)


def test_get_json_302_bao_dung_ly_do() -> None:
    """Trước đây 302 chỉ hiện ra dưới dạng JSONDecodeError mơ hồ."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "/Account/Login"}, text="")

    with pytest.raises(YokohamaSchemaError, match="redirect"):
        _client(handler).get_json("/Alarm/GetAlarmData")


def test_message_loi_khong_chua_location() -> None:
    """``Location`` có thể chứa địa chỉ nội bộ — không được vào message/DB."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"Location": "http://10.20.30.40:8080/Account/Login"}, text=""
        )

    with pytest.raises(YokohamaSchemaError) as ei:
        _stream(handler)
    assert "10.20.30.40" not in str(ei.value)
    assert "Account/Login" not in str(ei.value)


# ---------------------------------------------------------------- HTML


def test_trang_html_khong_phai_du_lieu() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<html><body>Sign in</body></html>",
        )

    with pytest.raises(YokohamaSchemaError, match="HTML"):
        _stream(handler)


def test_text_plain_van_hop_le() -> None:
    """Client gửi ``Accept: ... text/plain ...`` nên đòi đúng ``json`` là báo oan."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            text=json.dumps([RECORD]),
        )

    assert len(_stream(handler)) == 1


# ------------------------------------------------- body không có mảng nào


def test_body_khong_co_mang_nao_la_loi() -> None:
    """Chuỗi không chứa "[" hay "{": ``_split_objects`` bỏ qua sạch, im lặng."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="Access denied by policy")

    with pytest.raises(YokohamaSchemaError, match="không có mảng"):
        _stream(handler)


def test_body_rong_la_loi() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="")

    with pytest.raises(YokohamaSchemaError, match="không có mảng"):
        _stream(handler)


def test_loi_mang_mach_chua_du_lieu_chan_doan() -> None:
    """``error_summary`` phải đủ để chẩn đoán mà không cần vào log Vercel."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/plain"}, text="denied"
        )

    with pytest.raises(YokohamaSchemaError) as ei:
        _stream(handler)
    msg = str(ei.value)
    assert "status=200" in msg
    assert "text/plain" in msg
    assert "bytes=6" in msg


# ---------------------------------------------- KHÔNG báo oan ngày rỗng thật


def test_mang_rong_that_khong_bi_bao_oan() -> None:
    """``[]`` là câu trả lời hợp lệ: nguồn không có bản ghi nào cho cửa sổ này.

    Đây là bài giữ cho bản sửa không đi quá: mốc phân biệt là sự có mặt của "[",
    không phải số object thu được.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="[]")

    assert _stream(handler) == []


def test_mang_co_du_lieu_van_chay() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps([RECORD, RECORD]))

    assert len(_stream(handler)) == 2


# ---------------------------------------------------------------- alarms


class _FakeClient:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def get_json(self, path: str, params: dict | None = None) -> object:
        return self._payload

    def close(self) -> None:
        return None


def _adapter(payload: object) -> YokohamaAdapter:
    settings = YokohamaSettings(
        enabled=True, base_url="https://example.test/", psn="YKH-TANK-01"
    )
    return YokohamaAdapter(settings, client=_FakeClient(payload))  # type: ignore[arg-type]


def test_alarms_json_object_la_loi_khong_phai_rong() -> None:
    with pytest.raises(YokohamaSchemaError, match="không phải mảng"):
        _adapter({"error": "unauthorized"}).fetch_alarms(date(2026, 8, 27))


def test_alarms_mang_rong_van_hop_le() -> None:
    assert _adapter([]).fetch_alarms(date(2026, 8, 27)) == []


# --------------------------------------- text lỗi không được vào log công khai


def test_summary_chi_dem_khong_nha_text_loi() -> None:
    """``.github/workflows/ingest.yml`` in thân response vào log GitHub CÔNG KHAI.

    Message của ``YokohamaSchemaError`` chứa đường dẫn endpoint của cổng nguồn, và
    docstring của ``errors.py`` nói rõ nó không được phát ra ngoài. ``ActionOut``
    của ``/api/cron/ingest`` mang ``stats.summary()``, nên hàm đó phải chỉ đếm.
    Nguyên nhân đi vào ``ingest_runs.error_summary`` trong DB riêng, đọc qua
    endpoint admin — đủ để chẩn đoán, không rò ra log mở.
    """
    from app.services.ingestion import IngestStats

    stats = IngestStats()
    stats.errors.append(
        "YKH-TANK-01: YokohamaSchemaError: stream /Data/GetRecordData "
        "status=302 host=10.20.30.40 redirect"
    )
    out = stats.summary()
    assert "errors=1" in out
    assert "10.20.30.40" not in out
    assert "GetRecordData" not in out
    assert "302" not in out
