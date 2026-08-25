"""Validate toạ độ bồn ở tầng API. Hàm thuần, không cần DB.

Ba luật ở đây tồn tại vì ba cách sai cụ thể, không phải vì "nên validate":

1. **0,0** là giá trị module gửi khi MẤT định vị — cả hai thiết bị pilot đều trả
   ``0.000000 / 0.000000`` kèm ``gpsAddress = "--"``. Nhận nó thì bản đồ đặt bồn
   LNG ở Null Island giữa vịnh Guinea.
2. **Đảo thứ tự lat/lon** là lỗi kinh điển. Với Việt Nam (~10,97 N / 106,75 E) mà
   nhập ngược thì latitude = 106,75 — vượt ±90 nên chặn được. Không có luật này thì
   bồn im lặng nhảy sang nửa kia địa cầu.
3. **Nửa toạ độ** không vẽ được mà cũng không phải "chưa khai". DB đã cấm bằng
   CHECK; chặn ở API để trả 422 có lời giải thích thay vì 500 từ IntegrityError.

Và ngữ nghĩa ``null`` của toạ độ khác name/capacity_l: ``null`` tường minh nghĩa là
XOÁ ghim. Nếu không phân biệt "gửi null" với "không gửi" thì một ghim đặt sai không
bao giờ bỏ được.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.api.schemas import TerminalUpdateIn
from app.repositories import terminals as term_repo


class TestNhanToaDoHopLe:
    def test_toa_do_that_cua_bon_pilot(self) -> None:
        got = TerminalUpdateIn(latitude="10.971047", longitude="106.750161")
        assert got.latitude == Decimal("10.971047")
        assert got.longitude == Decimal("106.750161")
        assert got.location_sent is True

    def test_toa_do_am_van_hop_le(self) -> None:
        """Nam bán cầu / tây bán cầu: 0 không phải sàn."""
        got = TerminalUpdateIn(latitude="-33.87", longitude="-70.67")
        assert got.location_sent is True

    def test_bien_cua_khoang(self) -> None:
        assert TerminalUpdateIn(latitude="90", longitude="180").location_sent
        assert TerminalUpdateIn(latitude="-90", longitude="-180").location_sent


class TestChanGiaTriSai:
    def test_null_island_bi_tu_choi(self) -> None:
        with pytest.raises(ValidationError, match="mất định vị"):
            TerminalUpdateIn(latitude="0", longitude="0")

    def test_null_island_dang_thap_phan(self) -> None:
        """Vendor gửi chuỗi '0.000000', không phải '0'."""
        with pytest.raises(ValidationError, match="mất định vị"):
            TerminalUpdateIn(latitude="0.000000", longitude="0.000000")

    def test_kinh_do_bang_0_van_hop_le(self) -> None:
        """Chỉ CẶP 0,0 bị cấm. Kinh tuyến Greenwich là vị trí thật."""
        assert TerminalUpdateIn(latitude="51.48", longitude="0").location_sent

    def test_dao_thu_tu_lat_lon_bi_chan(self) -> None:
        # 106,75 là kinh độ bồn pilot, đặt sai vào ô latitude.
        with pytest.raises(ValidationError):
            TerminalUpdateIn(latitude="106.750161", longitude="10.971047")

    def test_ngoai_khoang_kinh_do(self) -> None:
        with pytest.raises(ValidationError):
            TerminalUpdateIn(latitude="10.97", longitude="200")

    def test_chi_gui_latitude(self) -> None:
        with pytest.raises(ValidationError, match="cả latitude và longitude"):
            TerminalUpdateIn(latitude="10.97")

    def test_chi_gui_longitude(self) -> None:
        with pytest.raises(ValidationError, match="cả latitude và longitude"):
            TerminalUpdateIn(longitude="106.75")

    def test_mot_ben_null_mot_ben_co_gia_tri(self) -> None:
        with pytest.raises(ValidationError, match="cùng null để xoá"):
            TerminalUpdateIn(latitude="10.97", longitude=None)


class TestXoaGhim:
    def test_gui_null_ca_hai_la_xoa(self) -> None:
        got = TerminalUpdateIn(latitude=None, longitude=None)
        assert got.location_sent is True
        assert got.latitude is None
        assert got.longitude is None

    def test_khong_gui_thi_khong_phai_xoa(self) -> None:
        """Sửa tên KHÔNG được âm thầm xoá toạ độ đã ghim."""
        got = TerminalUpdateIn(name="Bồn A - Kho Long An")
        assert got.location_sent is False
        assert got.latitude is None


class _FakeResult:
    def __init__(self, row: tuple) -> None:
        self._row = row

    def one(self) -> tuple:
        return self._row


class _CapturingSession:
    """Session giả: ghi lại statement, KHÔNG mở kết nối nào."""

    def __init__(self) -> None:
        self.stmts: list = []

    def execute(self, stmt, *a, **kw):  # type: ignore[no-untyped-def]
        self.stmts.append(stmt)
        return _FakeResult((uuid4(), True))


class TestUpsertKhongGhiDeGhimTay:
    """Ingest chỉ được ĐIỀN VÀO CHỖ TRỐNG, không được ghi đè.

    Đây là luật quan trọng nhất của đường tự động, và cũng là luật duy nhất cần DB
    thật mới quan sát được hành vi (xem tests/test_ingestion.py). Test này kiểm nó ở
    tầng SQL nên chạy được ở mọi nơi: biên dịch đúng câu lệnh mà upsert() phát ra
    rồi assert mệnh đề COALESCE có mặt.

    Vì sao đáng kiểm: nếu ghi đè vô điều kiện thì (a) mất vị trí chính xác người
    vận hành tự đo, và (b) một ngày module mất định vị sẽ XOÁ toạ độ đang đúng —
    GPS của vendor là dữ liệu thỉnh thoảng có.
    """

    def _sql(self) -> str:
        s = _CapturingSession()
        term_repo.upsert(
            s,  # type: ignore[arg-type]  # chỉ cần .execute(); không nối DB nào
            "2604200016",
            default_latitude=Decimal("10.971047"),
            default_longitude=Decimal("106.750161"),
        )
        assert s.stmts, "upsert() không phát ra statement nào"
        return str(s.stmts[0].compile(dialect=postgresql.dialect())).lower()

    def test_toa_do_duoc_coalesce(self) -> None:
        sql = self._sql()
        assert "coalesce(terminals.latitude, excluded.latitude)" in sql
        assert "coalesce(terminals.longitude, excluded.longitude)" in sql

    def test_khong_gan_thang_gia_tri_vendor(self) -> None:
        """`SET latitude = excluded.latitude` trần là đúng thứ phải không có."""
        sql = self._sql()
        assert "latitude = excluded.latitude" not in sql
        assert "longitude = excluded.longitude" not in sql


class TestGiuNguyenHanhViCu:
    def test_body_rong_van_bi_tu_choi(self) -> None:
        with pytest.raises(ValidationError, match="ít nhất một field"):
            TerminalUpdateIn()

    def test_ten_trang_van_bi_tu_choi(self) -> None:
        with pytest.raises(ValidationError):
            TerminalUpdateIn(name="   ")

    def test_chi_sua_dung_tich(self) -> None:
        got = TerminalUpdateIn(capacity_l="10425")
        assert got.capacity_l == 10425
        assert got.location_sent is False
