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


# ------------------------- thứ tự ngày là cấu hình -------------------------




def test_thu_tu_khong_hop_le_bi_tu_choi() -> None:
    from app.adapters.yokohama.mapping import TimestampParseError, parse_vendor_ts

    with pytest.raises(TimestampParseError, match="không hợp lệ"):
        parse_vendor_ts("27/08/2026 12:00", VN, order="ymd")


def test_chi_thu_MOT_thu_tu_khong_bao_gio_ca_hai() -> None:
    """Thử cả hai nghĩa là chọn bừa với ngày mơ hồ — một lần sai là hỏng lịch sử."""
    from app.adapters.yokohama.mapping import TimestampParseError, parse_vendor_ts

    # 27 không thể là tháng: dưới mdy phải THẤT BẠI, không được lặng lẽ lùi về dmy.
    with pytest.raises(TimestampParseError):
        parse_vendor_ts("27/08/2026 12:00", VN, order="mdy")
    # và ngược lại
    with pytest.raises(TimestampParseError):
        parse_vendor_ts("08/27/2026 12:00", VN, order="dmy")


def test_ngay_mo_ho_hai_thu_tu_ra_hai_ket_qua_khac_nhau() -> None:
    from app.adapters.yokohama.mapping import parse_vendor_ts

    a = parse_vendor_ts("03/09/2026 10:00", VN, order="dmy")
    b = parse_vendor_ts("03/09/2026 10:00", VN, order="mdy")
    assert a.astimezone(VN).date() == date(2026, 9, 3)
    assert b.astimezone(VN).date() == date(2026, 3, 9)


# ------------------- no_data phải nói về CẢ cycle, không phải một ngày -------------------


def test_no_data_khong_ke_ten_psn_da_dua_ve_du_lieu() -> None:
    """Ca thật, run 215: YKH đưa về 1038 dòng nhưng vẫn bị đếm là "không dữ liệu".

    Cửa sổ fetch gồm nhiều ngày. Nguồn phút chỉ có bản ghi hôm nay, nên lần gọi
    cho hôm qua trả rỗng và PSN vào psns_no_data trước khi lần gọi cho hôm nay
    thành công.
    """
    from app.services.ingestion import IngestStats

    st = IngestStats()
    st.psns_no_data.append("YKH-TANK-01")   # ngày hôm qua: rỗng
    st.psns_with_data.append("YKH-TANK-01")  # ngày hôm nay: 1038 dòng
    st.psns_no_data.append("2604200016")     # thiết bị chết thật
    assert st.no_data_psns() == ["2604200016"]
    assert "no_data=1" in st.summary()


def test_psn_chet_that_van_bi_ke_ten() -> None:
    from app.services.ingestion import IngestStats

    st = IngestStats()
    st.psns_no_data.extend(["2604200016", "2605090007"])
    assert st.no_data_psns() == ["2604200016", "2605090007"]
    assert "no_data=2" in st.summary()


# ============ do truc tiep tren cong song 2026-09-04 ============


def test_bao_dong_phai_hoi_khoang_khong_phai_mot_ngay() -> None:
    """``FromDate == ToDate`` LUÔN trả 0 — biên trên của cổng là loại trừ.

    Đo trên cổng sống, cả ba định dạng ngày, ba ngày liên tiếp:
        From == To    ->   0    0    0
        From .. To+1  ->  85  194  192
    Đó là lý do bảng "Báo động của nhà máy" rỗng suốt, không phải định dạng ngày.
    """
    seen: dict[str, str] = {}

    class _Cap:
        def get_json(self, path: str, params: dict | None = None) -> list:
            seen.update(params or {})
            return []

        def close(self) -> None:
            return None

    settings = YokohamaSettings(enabled=False, psn=PSN)
    ad = YokohamaAdapter(settings, client=_Cap())  # type: ignore[arg-type]
    ad.fetch_alarms(date(2026, 9, 3))
    assert seen["FromDate"] == "2026-09-03"
    assert seen["ToDate"] == "2026-09-04", "phải hỏi tới ngày KẾ TIẾP"



def test_readings_tra_ve_tang_dan() -> None:
    """Hợp đồng ``FetchResult.readings`` là TĂNG DẦN.

    Nguồn stream newest-first rồi append, nên nếu không sắp lại thì danh sách ra
    giảm dần — và ingestion đọc ``reversed()`` kèm chú thích "lấy cặp gần nhất",
    tức nó sẽ lấy bản đọc CŨ NHẤT để ghim toạ độ.
    """
    now = datetime.now(tz=VN).replace(second=0, microsecond=0)
    rows = [_rec(now - timedelta(minutes=i)) for i in range(5)]  # newest-first
    res = _tel_adapter(rows).fetch_telemetry(PSN, now.date())
    ats = [r.sampled_at for r in res.readings]
    assert ats == sorted(ats), "phải tăng dần"
    assert res.readings[-1].sampled_at.astimezone(VN).strftime("%H:%M") == now.strftime("%H:%M")



def test_tanknumber_la_so_lan_nap_khong_phai_ma_bon() -> None:
    """Trang Main ghi "Tank Refill Count: 70 Times" và payload có tankNumber=70.

    Mapping cũ đã hiểu đúng từ đầu (``extract_refill_counter``). Test này ghim lại
    để không ai — kể cả tôi — diễn giải nó thành mã thiết bị lần nữa: một guard
    dựa trên tiền đề đó sẽ báo lỗi ngay LẦN NẠP KẾ TIẾP.
    """
    now = datetime.now(tz=VN)
    rec = _rec(now)
    rec["tankNumber"] = 70
    res = _tel_adapter([rec]).fetch_telemetry(PSN, now.date())
    assert res.readings[0].refill_counter == 70


def test_ps1_ps2_bang_0_la_gia_tri_that_khong_phai_thieu_du_lieu() -> None:
    """0,00 bar trên PS1/PS2 là ĐIỀU KIỆN ĐANG BÁO ĐỘNG, không phải cảm biến hỏng.

    Trang Main của cổng hiển thị "Pressure (PS1): 0.00 bar" và "Pressure Value
    (PS2): 0.00 bar" (tô cam), và danh sách báo động 7 ngày có PS1 25 lần, PS2 28
    lần. Coi 0 là thiếu dữ liệu tức là che đúng cái điều kiện nhà máy đang báo.

    Ba field khác GIỮ zero_is_missing=True có lý: 0 m³ trên bồn cryogenic, 0 bar
    áp bồn (LNG tự sinh áp nên không thể bằng 0 khi còn lỏng), và 0 trên đồng hồ
    tích luỹ — cả ba là hỏng cảm biến, và số 0 ở đồng hồ còn tạo ra một lần reset
    giả trong đối chứng tiêu thụ.
    """
    now = datetime.now(tz=VN)
    rec = _rec(now)
    rec["pS1_Value"] = 0.0
    rec["pS2_Value"] = 0.0
    rec["pT1_Value"] = 0.0        # áp bồn = 0 -> vẫn phải coi là thiếu
    res = _tel_adapter([rec]).fetch_telemetry(PSN, now.date())
    r = res.readings[0]
    assert r.ps1_bar == 0, "PS1 = 0 phải được GIỮ"
    assert r.ps2_bar == 0, "PS2 = 0 phải được GIỮ"
    assert r.pressure_mpa is None, "áp bồn = 0 vẫn là hỏng cảm biến"

def test_dinh_dang_ngay_la_hai_hang_so_khac_nhau() -> None:
    """Cổng PARSE mm/dd và LUÔN XUẤT dd/mm. Hai chiều khác nhau, cả hai là hằng số.

    Đo bằng ngày KHÔNG mơ hồ nên không còn chỗ suy đoán:
      gửi "08/20/2026" (mm/dd = 20/8) -> trả "20/08/2026", refill 67, tot 1.132.100
      gửi "04/09/2026" (mm/dd = 9/4)  -> trả "09/04/2026", refill 38, tot   749.328
      gửi "09/04/2026" (mm/dd = 4/9)  -> trả "04/09/2026", refill 70, tot 1.132.428
    refill và totalizer tăng đơn điệu theo ngày -> MỘT bồn, MỘT đồng hồ.

    Test này tồn tại vì gửi sai chiều làm cổng trả dữ liệu của MỘT THÁNG KHÁC, và
    đọc sai chiều cất nó dưới mốc hôm nay — 2040 dòng đã bị ghi lệch 5 tháng như
    vậy, không tầng nào báo lỗi.
    """
    from app.adapters.yokohama import mapping as M

    assert M.REQUEST_DATE_FMT == "%m/%d/%Y", "cổng parse mm/dd"
    assert M.RECORD_TS_ORDER == "dmy", "cổng xuất dd/mm"
    assert M.ALARM_TS_ORDER == "dmy"
    # Chúng KHÁC chiều nhau; nếu ai đó làm chúng giống nhau thì một trong hai sai.
    assert M.REQUEST_DATE_FMT.index("%m") < M.REQUEST_DATE_FMT.index("%d")
    assert M.RECORD_TS_ORDER == "dmy"


def test_request_gui_dung_mm_dd() -> None:
    """Nghiệm thu trên đường dây thật: tham số fromDate/toDate phải là mm/dd."""
    seen: dict[str, str] = {}

    class _Cap:
        def iter_record_objects(self, params: dict):
            seen.update(params)
            return iter(())

        def get_json(self, path: str, params: dict | None = None) -> list:
            return []

        def close(self) -> None:
            return None

    settings = YokohamaSettings(enabled=False, psn=PSN)
    ad = YokohamaAdapter(settings, client=_Cap())  # type: ignore[arg-type]
    ad.fetch_telemetry(PSN, date(2026, 9, 4))
    # 4 thang 9 duoi mm/dd la "09/04"
    assert seen["fromDate"].startswith("09/04/2026"), seen["fromDate"]
