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
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

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


# ------------------------- "logger chết" khác "đường ống hỏng" -------------------------

VN = ZoneInfo("Asia/Ho_Chi_Minh")
PSN = "YKH-TANK-01"


def _rec(dt: datetime) -> dict:
    return {
        "dateTime": dt.strftime("%d/%m/%Y %H:%M"),
        "receivedAt": dt.strftime("%d/%m/%Y %H:%M"),
        "totalizer": 100000.0,
        "flowRate": 0.0,
        "pressure": 300.0,
        "temperature": 25.0,
        "tankVolume": 80.0,
        "tankNumber": 10,
        "tankPrecent": 48.0,
        "pT1_Value": 3.0,
        "pS1_Value": 3.0,
        "pS2_Value": 2.0,
        "tE1_Value": 25.0,
        "gD1_Value": 1.0,
        "gD2_Value": 0.2,
        "gD3_Value": 0.0,
    }


class _MemClient:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def iter_record_objects(self, params: dict):
        yield from list(self.rows)

    def get_json(self, path: str, params: dict | None = None) -> list:
        return []

    def close(self) -> None:
        return None


def _tel_adapter(rows: list[dict]) -> YokohamaAdapter:
    settings = YokohamaSettings(enabled=False, psn=PSN)
    return YokohamaAdapter(settings, client=_MemClient(rows))  # type: ignore[arg-type]


def test_nguon_gui_rong_thi_source_rows_bang_0() -> None:
    """Đường ống hỏng hoặc cổng không có gì: nguồn không gửi object nào."""
    res = _tel_adapter([]).fetch_telemetry(PSN, datetime.now(tz=VN).date())
    assert res.report.n_rows == 0
    assert res.report.source_rows == 0
    assert res.report.newest_source_at is None


def test_nguon_gui_du_lieu_cu_thi_source_rows_lon_hon_0() -> None:
    """Logger nhà máy đã chết nhưng đường ống BÌNH THƯỜNG.

    Đây là bài phân biệt. Cả hai ca đều cho ``n_rows=0`` và đi vào
    ``psns_no_data``, nên trước khi có ``source_rows`` chúng không thể tách ra —
    mà một ca cần thay thiết bị, ca kia cần sửa mạng hoặc URL.
    """
    old = datetime.now(tz=VN) - timedelta(days=40)
    res = _tel_adapter([_rec(old), _rec(old - timedelta(minutes=1))]).fetch_telemetry(
        PSN, datetime.now(tz=VN).date()
    )
    assert res.report.n_rows == 0, "dòng cũ không được giữ cho ngày hôm nay"
    assert res.report.source_rows >= 1, "nhưng nguồn CÓ gửi dữ liệu"
    assert res.report.newest_source_at is not None
    # Mốc phải là UTC ISO để so sánh chuỗi ra đúng thứ tự khi gộp.
    assert res.report.newest_source_at.endswith("+00:00")
    assert res.report.newest_source_at < datetime.now(tz=VN).astimezone(
        ZoneInfo("UTC")
    ).isoformat()


def test_report_cua_stream_khong_bi_bo_di() -> None:
    """``_ensure_cache`` từng tạo report cục bộ rồi bỏ — nên mọi số của nguồn này
    trong ``ingest_runs`` luôn bằng 0 bất kể thực tế."""
    now = datetime.now(tz=VN)
    res = _tel_adapter([_rec(now)]).fetch_telemetry(PSN, now.date())
    assert res.report.n_rows == 1
    assert res.report.source_rows == 1
    # provenance đến từ report của stream, không phải từ result.report rỗng
    assert res.report.resolved_from, "resolved_from phải được mang sang"


def test_stream_report_chi_gan_mot_lan_moi_cycle() -> None:
    """Stream chạy một lần cho mọi ngày; cộng nó vào từng ngày là đếm trùng."""
    now = datetime.now(tz=VN)
    yesterday = now - timedelta(days=1)
    ad = _tel_adapter([_rec(now), _rec(yesterday)])
    first = ad.fetch_telemetry(PSN, yesterday.date())
    second = ad.fetch_telemetry(PSN, now.date())
    assert first.report.source_rows + second.report.source_rows == 2
    assert 0 in (first.report.source_rows, second.report.source_rows)


# ------------------------- ngày đọc đảo tháng -------------------------


def test_accept_language_la_hop_dong_du_lieu() -> None:
    """Header này quyết định cổng trả dd/mm hay mm/dd — nó phải được gửi."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, text="[]")

    _stream(handler)
    assert "vi" in seen.get("accept-language", "")


def test_accept_language_rong_thi_khong_gui_header() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, text="[]")

    settings = YokohamaSettings(
        enabled=True, base_url="https://example.test/", accept_language=""
    )
    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=settings.base_url
    )
    list(YokohamaClient(settings, client=http).iter_record_objects({"device": "all"}))
    assert "accept-language" not in seen


def test_ngay_doc_dao_thang_bi_bat_khong_im_lang() -> None:
    """Ca thật đo được trên production: cổng gửi "09/03/2026 16:53" cho 3 tháng 9.

    Dựng lại bằng cách lấy hôm nay và viết nó ở dạng mm/dd. Chỉ chạy khi hôm nay
    là ngày mơ hồ (ngày <= 12); ngày khác thì mm/dd không parse được và lỗi đã
    hiện ra qua rejected_rows, không cần guard.
    """
    now = datetime.now(tz=VN)
    if now.day > 12:
        pytest.skip("hôm nay ngày > 12: mm/dd không parse được, lỗi đã tự hiện")
    swapped = f"{now.month:02d}/{now.day:02d}/{now.year} {now:%H:%M}"
    rec = _rec(now)
    rec["dateTime"] = swapped
    rec["receivedAt"] = swapped
    with pytest.raises(YokohamaSchemaError, match="ngày mơ hồ") as ei:
        _tel_adapter([rec]).fetch_telemetry(PSN, now.date())
    msg = str(ei.value)
    assert "SỐNG" in msg, "phải nói rõ dữ liệu là sống, không phải cũ"
    assert now.strftime("%H:%M") in msg, "giờ khớp là bằng chứng chính"


def test_bon_cu_that_khong_bi_bao_oan() -> None:
    """Ranh giới của guard: một bồn im lặng thật KHÔNG được biến thành lỗi.

    Ngày 25 của tháng không mơ hồ (25 > 12) nên chỉ có một cách đọc, và guard
    phải im. Đây là bài giữ cho bản sửa không đoán bừa: nếu nhà máy thật sự dừng
    báo, đó là chuyện thay thiết bị, không phải lỗi schema.
    """
    old = (datetime.now(tz=VN) - timedelta(days=200)).replace(day=25)
    res = _tel_adapter([_rec(old)]).fetch_telemetry(PSN, datetime.now(tz=VN).date())
    assert res.report.n_rows == 0
    assert res.report.source_rows == 1
    assert res.report.newest_source_at is not None


def test_ngay_mo_ho_nhung_cach_doc_kia_cung_ngoai_cua_so_thi_im() -> None:
    """Mơ hồ mà đảo lại vẫn ngoài cửa sổ: không có bằng chứng gì, đừng báo."""
    old = (datetime.now(tz=VN) - timedelta(days=400)).replace(day=5, month=4)
    res = _tel_adapter([_rec(old)]).fetch_telemetry(PSN, datetime.now(tz=VN).date())
    assert res.report.n_rows == 0
    assert res.report.source_rows == 1
