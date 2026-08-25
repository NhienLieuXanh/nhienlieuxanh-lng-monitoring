"""Bản đồ nhúng trong app: chứng minh KHÔNG có đường lưỡi bò.

Đây là lý do tồn tại của việc tự nhúng file vector thay vì gọi tile của một nhà
cung cấp. Với tile raster thì không viết được test nào như file này: nội dung do
bên ngoài quyết định, đổi lúc nào không biết, và muốn kiểm phải đọc ảnh. Với một
file GeoJSON nằm trong repo thì kiểm được bằng hình học.

Cách kiểm: thả điểm thăm dò vào **vùng biển hở** nằm bên trong yêu sách đường lưỡi
bò, rồi assert không polygon nào phủ điểm đó. Nếu file có vẽ đường lưỡi bò (dưới
dạng polygon lãnh thổ hay vùng tranh chấp) thì ít nhất một điểm sẽ rơi vào một
feature và test đỏ.

Phép thử này chỉ có giá trị khi thuật toán point-in-polygon thật sự chạy, nên có
``test_diem_doi_chung_tren_dat``: nếu hàm luôn trả rỗng thì test biển pass một cách
vô nghĩa. Hai test phải đi cùng nhau.

Test đọc ĐÚNG file được ship (``app/static/world-vi.geojson``), không dựng lại từ
nguồn. Chạy lại `scripts/build_world_map.py` mà làm sai thì test này bắt được.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_settings
from app.config import Settings
from app.main import create_app

MAP_FILE = (
    pathlib.Path(__file__).resolve().parent.parent
    / "app"
    / "static"
    / "world-vi.geojson"
)

# Giữ dưới 400 KB. Không phải con số tuỳ ý: file hiện tại ~173 KB, còn dữ liệu
# Natural Earth thô là ~839 KB vì mang 168 thuộc tính mỗi vùng. Trần này bắt được
# việc ai đó copy thẳng file gốc vào đây thay vì chạy script dựng.
MAX_BYTES = 400_000

# Điểm thăm dò: biển hở nằm TRONG yêu sách đường lưỡi bò, cách bờ và cách mọi đảo.
# Chọn thưa khắp hình chữ U để một đoạn vạch bất kỳ cũng khó lọt qua hết.
BIEN_DONG = (
    ("giữa Biển Đông", 14.0, 114.0),
    ("đông Hải Nam", 18.0, 116.0),
    ("vùng Trường Sa", 10.0, 113.0),
    ("EEZ Việt Nam ngoài khơi Nam Bộ", 8.0, 110.0),
    ("ngoài khơi Nha Trang", 12.0, 110.5),
    ("vùng Hoàng Sa", 16.0, 112.5),
    ("giáp Đài Loan", 20.0, 118.0),
    ("nam Biển Đông", 6.0, 108.0),
    ("đông Biển Đông", 15.0, 117.0),
    ("tây Palawan", 11.0, 117.0),
)

# Điểm trên đất liền, để chứng minh phép thử có hiệu lực.
DAT_LIEN = (
    ("Hà Nội", 21.028, 105.854, "Việt Nam"),
    ("TP.HCM", 10.776, 106.700, "Việt Nam"),
    # Toạ độ thật duy nhất từng thấy trong dữ liệu vendor (DISCOVERY.md).
    ("bồn pilot", 10.971047, 106.750161, "Việt Nam"),
    ("Bắc Kinh", 39.904, 116.407, "Trung Quốc"),
    ("Manila", 14.599, 120.984, "Philippines"),
)

# Không dùng regex quét chữ Hán để tìm "vùng tranh chấp": tên tiếng Việt không có
# chữ Hán, nên quét theo TỪ KHOÁ đã biết mới đúng thứ cần bắt.
TU_KHOA_TRANH_CHAP = (
    "paracel",
    "spratly",
    "nine-dash",
    "nine dash",
    "南沙",
    "西沙",
    "九段",
)

CJK = re.compile(r"[　-〿㐀-䶿一-鿿豈-﫿]")


@pytest.fixture(scope="module")
def fc() -> dict:
    return json.loads(MAP_FILE.read_text(encoding="utf-8"))


def _trong_vong(vong: list, x: float, y: float) -> bool:
    """Ray casting. ``vong[0]`` là biên ngoài, các vòng sau là lỗ."""

    def ben_trong(ring: list) -> bool:
        c = False
        n = len(ring)
        for i in range(n):
            x1, y1 = ring[i]
            x2, y2 = ring[(i + 1) % n]
            if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                c = not c
        return c

    if not ben_trong(vong[0]):
        return False
    return not any(ben_trong(r) for r in vong[1:])


def _ai_phu(fc: dict, lat: float, lon: float) -> list[str]:
    return [
        f["properties"]["n"]
        for f in fc["features"]
        if any(_trong_vong(poly, lon, lat) for poly in f["geometry"]["coordinates"])
    ]


class TestKhongCoDuongLuoiBo:
    @pytest.mark.parametrize(("ten", "lat", "lon"), BIEN_DONG)
    def test_bien_ho_khong_thuoc_polygon_nao(
        self, fc: dict, ten: str, lat: float, lon: float
    ) -> None:
        phu = _ai_phu(fc, lat, lon)
        assert phu == [], (
            f"{ten} ({lat}N {lon}E) nằm trong {phu} — bản đồ đang vẽ yêu sách trên "
            f"vùng biển hở"
        )

    @pytest.mark.parametrize(("ten", "lat", "lon", "mong_doi"), DAT_LIEN)
    def test_diem_doi_chung_tren_dat(
        self, fc: dict, ten: str, lat: float, lon: float, mong_doi: str
    ) -> None:
        """Nếu thiếu test này, một hàm luôn trả rỗng làm test biển pass vô nghĩa."""
        assert _ai_phu(fc, lat, lon) == [mong_doi], ten

    def test_quet_luoi_vung_bien_ho(self, fc: dict) -> None:
        """Mạnh hơn 10 điểm rời: quét lưới 0,5° trên ba vùng biển hở.

        10 điểm có thể lọt qua một vạch mảnh. Ba hộp dưới đây phủ phần lớn vùng
        nước sâu trong yêu sách, và **cố ý tránh** Hoàng Sa (~16,8 N 112,3 E),
        Trường Sa (~8–11,5 N) và Hải Nam: một bản đồ Việt Nam vẽ hai quần đảo đó
        là đất thì đúng, không phải lỗi — thứ test này bắt là yêu sách trên **mặt
        nước**.

        Đã kiểm bằng đột biến: thêm một polygon yêu sách giả thì 10/10 điểm thăm
        dò bắt được, còn vẽ dạng LineString thì test_dinh_dang_geojson đỏ.
        """
        HOP = (
            ("bắc, đông Hoàng Sa", 112.0, 118.0, 17.0, 20.5),
            ("giữa, giữa VN và Philippines", 109.8, 116.0, 12.0, 15.5),
            ("nam, ngoài khơi Nam Bộ", 108.5, 113.0, 6.0, 8.5),
        )
        trung = []
        for ten, lo1, lo2, la1, la2 in HOP:
            lo = lo1
            while lo <= lo2 + 1e-9:
                la = la1
                while la <= la2 + 1e-9:
                    if phu := _ai_phu(fc, la, lo):
                        trung.append((ten, round(la, 2), round(lo, 2), phu))
                    la += 0.5
                lo += 0.5
        assert trung == []

    def test_khong_feature_nao_la_vung_tranh_chap(self, fc: dict) -> None:
        thay = []
        for f in fc["features"]:
            blob = " ".join(
                v.lower() for v in f["properties"].values() if isinstance(v, str)
            )
            thay += [k for k in TU_KHOA_TRANH_CHAP if k in blob]
        assert thay == []


class TestNhanTiengViet:
    def test_ten_nuoc_bang_tieng_viet(self, fc: dict) -> None:
        ten = {f["properties"]["n"] for f in fc["features"]}
        assert "Việt Nam" in ten
        assert "Trung Quốc" in ten
        assert "Campuchia" in ten
        # Tên tiếng Anh không được lọt vào chỗ đã có bản tiếng Việt.
        assert "Vietnam" not in ten
        assert "China" not in ten

    def test_khong_co_chu_han(self) -> None:
        """Chặn việc một lần dựng lại lấy nhầm NAME_ZH.

        Cùng luật với test_isolation: không ký tự CJK nào được ra tới màn hình
        người dùng Việt Nam.
        """
        assert CJK.findall(MAP_FILE.read_text(encoding="utf-8")) == []

    def test_moi_vung_deu_co_ten(self, fc: dict) -> None:
        assert [f for f in fc["features"] if not f["properties"].get("n")] == []


class TestCauTrucFile:
    def test_dinh_dang_geojson(self, fc: dict) -> None:
        assert fc["type"] == "FeatureCollection"
        assert len(fc["features"]) > 150
        assert {f["geometry"]["type"] for f in fc["features"]} == {"MultiPolygon"}

    def test_chi_giu_hai_thuoc_tinh(self, fc: dict) -> None:
        """Bắt việc copy thẳng dữ liệu thô (168 thuộc tính mỗi vùng) vào repo."""
        assert {tuple(sorted(f["properties"])) for f in fc["features"]} == {("i", "n")}

    def test_vong_khep_kin_va_du_diem(self, fc: dict) -> None:
        """Vòng hở hoặc dưới 4 điểm làm path SVG vẽ sai mà không báo lỗi gì."""
        for f in fc["features"]:
            for poly in f["geometry"]["coordinates"]:
                for ring in poly:
                    assert len(ring) >= 4
                    assert ring[0] == ring[-1]

    def test_toa_do_trong_khoang_hop_le(self, fc: dict) -> None:
        for f in fc["features"]:
            for poly in f["geometry"]["coordinates"]:
                for ring in poly:
                    for lon, lat in ring:
                        assert -180 <= lon <= 180
                        assert -90 <= lat <= 90

    def test_khong_phinh_dung_luong(self) -> None:
        assert MAP_FILE.stat().st_size < MAX_BYTES


def test_app_serve_duoc_file_ban_do(settings: Settings) -> None:
    """Dữ liệu đúng mà app không serve được thì trang Bản đồ vẫn trắng.

    Không vào lifespan nên không cần DB — file tĩnh không chạm database.
    """
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    r = TestClient(app).get("/ui/world-vi.geojson")
    assert r.status_code == 200
    assert r.json()["type"] == "FeatureCollection"
