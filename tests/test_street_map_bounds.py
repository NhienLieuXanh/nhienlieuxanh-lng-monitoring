"""Bản đồ đường phố (tile OpenStreetMap): chứng minh nó KHÔNG BAO GIỜ chạm Biển Đông.

Tile ngoài từng bị từ chối vì chủ quyền (xem ``test_world_map.py``): nội dung tile
do bên ngoài quyết định, không chứng minh được Hoàng Sa và Trường Sa hiện đúng.
Ngày 26/10/2026 người dùng chọn thêm bản đồ đường phố với điều kiện KHOÁ trong đất
liền. File này chứng minh lời khoá đó bằng số học trên lưới tile, không cần đọc ảnh:

* ở mọi mức zoom mà trang cho phép, tile chứa mép đông của hộp kết thúc trước
  quần đảo gần bờ nhất (Hoàng Sa, phía tây nhất ~111,2°E);
* trang thật sự truyền hộp đó cho CẢ lớp tile (``bounds`` — không xin tile ngoài
  hộp) LẪN khung nhìn (``maxBounds`` + viscosity 1 — không kéo ra ngoài được).

Hằng số đọc từ ĐÚNG file được ship (``app/static/index.html``). E2E kiểm phần còn
lại trên trình duyệt thật: mọi tile được xin đều nằm trong giới hạn này.
"""

from __future__ import annotations

import math
import pathlib
import re

INDEX = pathlib.Path(__file__).resolve().parent.parent / "app" / "static" / "index.html"

# Kinh độ phía tây nhất của các quần đảo ngoài khơi Việt Nam (Hoàng Sa: đảo Tri Tôn
# ~111,2°E; Trường Sa còn xa hơn về đông). Lấy tròn xuống để chừa biên an toàn.
ISLANDS_WEST_LON = 111.0
MAX_ZOOM = 19


def _street() -> dict[str, float]:
    src = INDEX.read_text(encoding="utf-8")
    m = re.search(r"const MP_STREET = \{([^}]*)\}", src)
    assert m, "không tìm thấy MP_STREET trong index.html"
    return {k: float(v) for k, v in re.findall(r"(\w+):\s*(-?[\d.]+)", m.group(1))}


def _tile_east_edge(lon: float, z: int) -> float:
    """Kinh độ mép đông của tile (lưới Web Mercator) chứa kinh độ ``lon`` ở zoom ``z``."""
    n = 2**z
    x = math.floor((lon + 180.0) / 360.0 * n)
    return (x + 1) / n * 360.0 - 180.0


def test_khong_tile_nao_toi_quan_dao_o_moi_muc_zoom() -> None:
    b = _street()
    assert b["minZoom"] >= 7
    for z in range(int(b["minZoom"]), MAX_ZOOM + 1):
        edge = _tile_east_edge(b["east"], z)
        assert edge < ISLANDS_WEST_LON, f"zoom {z}: tile mép đông kết thúc ở {edge}°E"


def test_zoom_thap_hon_se_lan_ra_bien_nen_phai_bi_cam() -> None:
    """Đối chứng: chứng minh phép tính ở trên THẬT SỰ bắt được lỗi. Ở zoom 6 một
    tile rộng 5,625° và tile chứa 109,6°E trùm lên tận 112,5°E — tức Hoàng Sa."""
    assert _tile_east_edge(109.6, 6) >= ISLANDS_WEST_LON


def test_hop_nam_tron_trong_dat_lien_va_chua_cac_bon() -> None:
    b = _street()
    assert b["east"] < ISLANDS_WEST_LON
    # Hai khu đặt bồn hiện có (Bình Dương / TP.HCM) phải nằm trong hộp.
    for lat, lon in [(10.932519, 106.734816), (10.971047, 106.750161)]:
        assert b["south"] <= lat <= b["north"] and b["west"] <= lon <= b["east"]


def test_trang_truyen_hop_cho_ca_lop_tile_lan_khung_nhin() -> None:
    src = INDEX.read_text(encoding="utf-8")
    tile = re.search(r"L\.tileLayer\(MP_TILE_URL, \{(.*?)\}\)", src, re.S)
    assert tile, "không tìm thấy lớp tile"
    assert "bounds: box" in tile.group(1)
    assert "minZoom: MP_STREET.minZoom" in tile.group(1)
    view = re.search(r"L\.map\(\"mp-street\", \{(.*?)\}\)", src, re.S)
    assert view, "không tìm thấy khung bản đồ đường phố"
    assert "maxBounds: box" in view.group(1)
    assert "maxBoundsViscosity: 1" in view.group(1)
    assert "minZoom: MP_STREET.minZoom" in view.group(1)
