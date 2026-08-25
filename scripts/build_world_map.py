"""Dựng `app/static/world-vi.geojson` — bản đồ thế giới nhãn tiếng Việt.

Vì sao tự dựng thay vì gọi tile của một nhà cung cấp:

1. **Đường lưỡi bò.** Đây là yêu cầu cứng của công ty. Với tile raster ta không
   kiểm soát được nội dung: nhà cung cấp render theo dữ liệu của họ, có thể đổi
   bất kỳ lúc nào, và không có cách nào viết test chứng minh tile sạch ở mọi mức
   zoom. Với một file vector nằm trong repo thì chứng minh được — xem
   `tests/test_world_map.py`, nó thả điểm thăm dò vào Biển Đông và assert không
   polygon nào phủ.
2. **Không gọi ra ngoài.** File nằm cùng app nên bản đồ chạy được sau firewall
   công ty và không tốn đồng nào.

Nguồn: Natural Earth 1:110m Admin 0 – Countries, **public domain** (không bắt
buộc ghi công, nhưng app vẫn ghi cho minh bạch).
    https://github.com/nvkelso/natural-earth-vector

Chạy lại khi cần cập nhật biên giới:
    .\\.venv\\Scripts\\python.exe scripts\\build_world_map.py

Hai phép biến đổi so với dữ liệu gốc, và lý do:

- **Bỏ 166/168 thuộc tính.** Chỉ giữ `n` (tên tiếng Việt) và `i` (ISO alpha-3).
  Giữ cả 168 field làm file phình lên nhiều lần mà app không đọc field nào khác.
- **Làm tròn toạ độ 2 chữ số** (~1,1 km). Đo thực tế: 1 chữ số ra 154 KB, 2 chữ
  số ra 173 KB, 3 chữ số ra 193 KB — chênh nhau không đáng kể vì dữ liệu 110m đã
  thưa điểm sẵn (~10.600 điểm ở cả ba mức). Nên chọn mức không làm mất nét đường
  bờ biển thay vì mức nhỏ nhất.

KHÔNG đơn giản hoá hình học (Douglas-Peucker): số điểm không phải chỗ tốn dung
lượng đáng để đánh đổi, và làm gãy đường bờ biển Việt Nam thì lỗ nhiều hơn lãi.
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.request
from typing import Any

SRC_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
    "geojson/ne_110m_admin_0_countries.geojson"
)
OUT = (
    pathlib.Path(__file__).resolve().parent.parent
    / "app"
    / "static"
    / "world-vi.geojson"
)
NDIGITS = 2


def clean_ring(ring: list[list[float]]) -> list[list[float]] | None:
    """Làm tròn, bỏ điểm trùng liền kề, đảm bảo vòng khép kín.

    Bỏ điểm trùng là bắt buộc *sau khi* làm tròn: hai điểm cách nhau 300 m thành
    cùng một toạ độ, để lại thì file có điểm rác và path SVG có đoạn dài 0.
    """
    out: list[list[float]] = []
    for pt in ring:
        p = [round(pt[0], NDIGITS), round(pt[1], NDIGITS)]
        if not out or out[-1] != p:
            out.append(p)
    if len(out) >= 4 and out[0] != out[-1]:
        out.append(out[0][:])
    # Dưới 4 điểm thì không còn là đa giác — đảo quá nhỏ ở tỉ lệ này, bỏ.
    return out if len(out) >= 4 else None


def main() -> int:
    # Console Windows mặc định cp1252, `print` một chữ có dấu là UnicodeEncodeError
    # và script chết trước khi làm gì. Ép UTF-8 tại chỗ để chạy tay không cần đặt
    # PYTHONIOENCODING. Cùng họ với các bẫy Windows đã ghi trong README.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"tải {SRC_URL}")
    with urllib.request.urlopen(SRC_URL, timeout=120) as r:
        src = json.loads(r.read().decode("utf-8"))

    feats: list[dict[str, Any]] = []
    pts = 0
    for f in src["features"]:
        p = f["properties"]
        geom = f["geometry"]
        polys = (
            [geom["coordinates"]]
            if geom["type"] == "Polygon"
            else geom["coordinates"]
        )
        kept = []
        for poly in polys:
            rings = [r for r in (clean_ring(r) for r in poly) if r]
            if rings:
                kept.append(rings)
        if not kept:
            continue
        pts += sum(len(r) for poly in kept for r in poly)
        # NAME_VI có đủ cho cả 177 vùng; NAME_EN là lưới an toàn nếu Natural
        # Earth thêm vùng mới chưa kịp dịch.
        name = p.get("NAME_VI") or p.get("NAME_EN") or p.get("NAME")
        feats.append(
            {
                "type": "Feature",
                "properties": {"n": name, "i": p.get("ISO_A3")},
                "geometry": {"type": "MultiPolygon", "coordinates": kept},
            }
        )

    fc = {"type": "FeatureCollection", "features": feats}
    data = json.dumps(fc, ensure_ascii=False, separators=(",", ":"))
    OUT.write_text(data, encoding="utf-8")

    print(f"ghi {OUT}")
    print(f"  {len(feats)} vùng · {pts} điểm · {len(data.encode('utf-8'))} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
