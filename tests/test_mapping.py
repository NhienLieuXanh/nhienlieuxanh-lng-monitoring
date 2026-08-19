"""Mapping vendor, kiểm trên response THẬT đã capture.

Các test này là lý do fixture tồn tại: chúng khoá lại những kết luận đã tốn công
xác minh, để một lần refactor mapping không âm thầm làm mất chúng.
"""

from __future__ import annotations

import json
from decimal import Decimal
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest

from app.adapters.xingke import mapping as M
from app.adapters.xingke.envelope import extract_page
from app.adapters.xingke.normalizer import (
    merge_terminals,
    normalize_reading,
    normalize_terminal,
)
from app.domain.contracts import MappingReport, PercentSource
from tests.conftest import FIXTURES

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _rows(name: str):
    payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return extract_page(payload["data"])


def test_envelope_success_is_code_200():
    """code == 200, KHÔNG phải 0.

    Plan gốc giả thiết `code == 0`; theo nó thì MỌI request thành công đều bị raise.
    """
    from app.adapters.xingke.envelope import SUCCESS_CODES

    assert 200 in SUCCESS_CODES


def test_page_shape_is_content_totalelements():
    """Vendor dùng content/totalElements, không phải records/total."""
    rows, total = _rows("psn_search_real.json")
    assert len(rows) == 1
    assert total == 1


def test_all_keys_accounted_for():
    """Không key nào của vendor bị bỏ sót mà cũng không được khai là chủ động bỏ.

    Đây là test bắt được việc vendor thêm field mới: nó sẽ hiện trong unmapped_keys.
    """
    rep = MappingReport()
    rows, _ = _rows("psn_search_real.json")
    for row in rows:
        M.record_unmapped(M.build_index(row), rep)
    assert rep.unmapped_keys == set()


def test_reading_matches_real_values():
    rep = MappingReport()
    rows, _ = _rows("psn_search_real.json")
    r = normalize_reading(
        rows[0], psn="2604200016", source="xingke", vendor_tz=SHANGHAI, report=rep
    )
    assert r is not None
    assert r.volume_l == Decimal("61")
    assert r.pressure_mpa == Decimal("0.071")
    assert r.battery_v == Decimal("3.6")
    assert r.signal_percent == Decimal("20")
    # height là MỰC LỎNG mmWC, không phải chiều cao bồn.
    assert r.level_mmwc == Decimal("42")
    assert r.diff_pressure_kpa == Decimal("0.41")
    # temperatureOne là null trên thiết bị thật; `temperature`=0 KHÔNG được map vào.
    assert r.temperature_c is None
    assert r.capacity_l == Decimal("10425")
    assert r.medium_name == "LNG"


def test_volume_percent_is_0_to_100_scale():
    """0.59 nghĩa là 0.59% ĐẦY, không phải 59%.

    Xác nhận bằng chính dữ liệu: 61 / 10425 * 100 = 0.5851 ~ 0.59.
    """
    rep = MappingReport()
    rows, _ = _rows("psn_search_real.json")
    r = normalize_reading(
        rows[0], psn="2604200016", source="xingke", vendor_tz=SHANGHAI, report=rep
    )
    assert r is not None and r.volume_l is not None and r.capacity_l is not None
    derived = r.volume_l / r.capacity_l * Decimal(100)
    assert r.volume_percent == Decimal("0.59")
    assert abs(derived - r.volume_percent) < Decimal("0.01")
    assert r.volume_percent_source is PercentSource.VENDOR


def test_naive_vendor_timestamp_parsed_as_shanghai():
    """'2026-07-23 16:03:29' là giờ Thượng Hải => 08:03:29 UTC => 15:03 giờ VN.

    Mock ban đầu ghi +07:00 và lệch đúng 1 tiếng. Sai TZ ở đây không phải lỗi hiển
    thị — nó làm hỏng khoá dedup (psn, sampled_at).
    """
    ts = M.parse_vendor_ts("2026-07-23 16:03:29", SHANGHAI)
    assert ts.isoformat() == "2026-07-23T08:03:29+00:00"
    assert ts.astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).hour == 15


def test_sampled_at_must_be_aware():
    """NormalizedTelemetry từ chối datetime naive."""
    from datetime import datetime

    from app.domain.contracts import NormalizedTelemetry

    with pytest.raises(ValueError, match="tz-aware"):
        NormalizedTelemetry(
            source="x", psn="1", sampled_at=datetime(2026, 1, 1, 0, 0), raw_payload={}
        )


def test_out_of_range_value_rejected_not_stored():
    """Giá trị ngoài khoảng bị loại + ghi vào report, không âm thầm lưu.

    Mô phỏng vendor đổi pressureMpa sang gửi kPa (71 thay vì 0.071).
    """
    rep = MappingReport()
    row = {"time": "2026-07-23 16:03:29", "pressureMpa": 71}
    r = normalize_reading(
        row, psn="X", source="xingke", vendor_tz=SHANGHAI, report=rep
    )
    assert r is not None
    assert r.pressure_mpa is None
    assert any("ngoai khoang" in e or "ngoài khoảng" in e for _, e in rep.errors)


def test_bad_timestamp_rejects_row():
    """Dòng không có instant đáng tin bị loại, không lưu với thời gian đoán."""
    rep = MappingReport()
    r = normalize_reading(
        {"time": "khong-phai-ngay"}, psn="X", source="xingke",
        vendor_tz=SHANGHAI, report=rep,
    )
    assert r is None
    assert rep.rejected_rows == 1


def test_hardware_version_resolves_across_both_spellings():
    """psn/search viết hardwareVersion, device/list viết hardwarVersion (thiếu e).

    Alias index chuẩn hoá key làm cả hai resolve — không phải đề phòng lý thuyết.
    """
    rep = MappingReport()
    dev_rows, _ = _rows("device_list_real.json")
    assert "hardwarVersion" in dev_rows[0]
    assert "hardwareVersion" not in dev_rows[0]
    t = normalize_terminal(dev_rows[0], psn="2604200016", report=rep)
    assert rep.resolved_from["hardware_version"] == "hardwarVersion"
    assert t.psn == "2604200016"


def test_merge_terminals_combines_both_endpoints():
    """moduleNumber/cardNumber chỉ có ở psn/search; deviceMode chỉ ở device/list."""
    rep = MappingReport()
    s_rows, _ = _rows("psn_search_real.json")
    d_rows, _ = _rows("device_list_real.json")
    merged = merge_terminals(
        normalize_terminal(s_rows[0], psn="2604200016", report=rep),
        normalize_terminal(d_rows[0], psn="2604200016", report=rep),
    )
    assert merged is not None
    assert merged.modem_number == "860000000000000"   # từ psn/search
    assert merged.sim_iccid == "89860000000000000000"
    assert merged.capacity_l == Decimal("10425")


def test_twelve_row_fixture_full_coverage():
    rep = MappingReport()
    rows, total = _rows("psn_search_real_12rows.json")
    assert total == 12
    out = [
        normalize_reading(
            r, psn="2605090007", source="xingke", vendor_tz=SHANGHAI, report=rep
        )
        for r in rows
    ]
    assert all(x is not None for x in out)
    # 8/9 field đo có dữ liệu; chỉ temperature_c luôn null.
    assert rep.coverage() == "8/9"
    assert rep.always_null() == ["temperature_c"]
    assert rep.unmapped_keys == set()


def test_sample_cadence_is_30_minutes():
    """Cadence đo từ dữ liệu thật — cơ sở cho ngưỡng stale 90 phút = 3 sample."""
    rows, _ = _rows("psn_search_real_12rows.json")
    ts = sorted(
        M.parse_vendor_ts(M.find_timestamp(M.build_index(r)), SHANGHAI) for r in rows
    )
    # round(), không phải //: timestamp thật có drift giây (22:17:03 -> 20:47:02),
    # nên một khoảng 29m59s sẽ floor thành 29 và làm test sai trong khi dữ liệu đúng.
    gaps = {round((b - a).total_seconds() / 60) for a, b in pairwise(ts)}
    assert gaps == {30}
