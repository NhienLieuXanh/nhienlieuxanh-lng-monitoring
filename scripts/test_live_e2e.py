"""Kiểm thử end-to-end một server LNG đang chạy — KIỂM NỘI DUNG, không kiểm hình dạng.

    .venv\\Scripts\\python.exe scripts\\test_live_e2e.py
    .venv\\Scripts\\python.exe scripts\\test_live_e2e.py http://127.0.0.1:8000
    .venv\\Scripts\\python.exe scripts\\test_live_e2e.py --allow-writes

Mặc định bắn vào production và **chỉ đọc**.

Vì sao viết lại từ đầu — bản trước báo "ALL 13 SUITES PASSED" trên production trong
khi tự in ra ``History (0 points)`` và ``Trips generated: 0`` ngay trong dòng PASS.
Nó chỉ assert HTTP 200 và sự tồn tại của khoá JSON, nên toàn bộ 13 mục vẫn xanh nếu
bảng ``telemetry`` trống sạch. Ba luật để không lặp lại:

1. **Ba kết quả, không phải hai.** ``PASS`` là đã đối chiếu được giá trị. ``EMPTY`` là
   endpoint trả rỗng nên KHÔNG kiểm được gì — nó không bao giờ được đếm là PASS.
   ``FAIL`` là giá trị sai. Chỉ khi không còn EMPTY thì mới in "mọi mục đều đúng".
2. **Đối chiếu chéo, không tự khai.** Một endpoint nói gì cũng được; giá trị chỉ đáng
   tin khi hai nguồn độc lập nói cùng một con số, hoặc khi nó khớp một công thức tính
   tay được. Ví dụ ``fill_percent`` phải khớp ``volume_l/capacity_l x 100`` — chính
   phép so này bắt được lỗi thang 0-1 vs 0-100 mà không constraint nào bắt.
3. **Không assert khoảng.** ``in (401, 503)`` biến một endpoint sập thành "bảo mật đã
   xác thực". Trạng thái nào thì assert đúng trạng thái đó.

Ghi dữ liệu: mặc định TẮT. Bật bằng ``--allow-writes`` thì mọi lần ghi đều chụp
baseline trước, assert lần ghi có hiệu lực, phục hồi, rồi **assert phục hồi khớp
baseline**. Bản trước gọi PATCH phục hồi mà không kiểm kết quả và lấy giá trị đang có
làm mốc gốc, nên một lượt lỗi giữa đường là đóng đinh giá trị sai cho mọi lượt sau.

Đăng nhập: ưu tiên đăng nhập THẬT qua ``/api/auth/login`` với ``E2E_USERNAME`` /
``E2E_PASSWORD`` (hoặc ``--user`` / ``--password``). Không có thì tự ký cookie phiên
bằng ``SESSION_SECRET`` và báo rõ ``[SKIP]`` — cookie tự ký không chứng minh luồng
đăng nhập hoạt động, nên nó KHÔNG được in ra như một mục PASS.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx
import itsdangerous

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.config import get_settings  # noqa: E402

PROD = "https://nhienlieuxanh-lng-monitoring.vercel.app"

# Mọi endpoint dữ liệu. Giữ khớp với SESSION_GET trong tests/test_e2e_product.py.
PROTECTED = (
    "/api/auth/me",
    "/api/alerts",
    "/api/stats/summary",
    "/api/terminals",
    "/api/terminals/{psn}",
    "/api/telemetry/{psn}",
    "/api/telemetry/{psn}/latest",
    "/api/forecast",
    "/api/forecast/{psn}",
    "/api/analytics",
    "/api/analytics/{psn}",
    "/api/refills/{psn}",
    "/api/delivery-plan",
    "/api/export/tanks.csv",
    "/api/export/refills.csv",
    "/api/export/telemetry.csv",
    "/api/settings",
)
CJK_RANGES = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0xF900, 0xFAFF))


class Report:
    """Ba kết quả. ``EMPTY`` tồn tại để dữ liệu rỗng không bao giờ đọc thành PASS."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def _add(self, kind: str, name: str, detail: str) -> None:
        self.rows.append((kind, name, detail))
        print(f"  [{kind:5}] {name}" + (f" — {detail}" if detail else ""))

    def ok(self, name: str, detail: str = "") -> None:
        self._add("PASS", name, detail)

    def fail(self, name: str, detail: str) -> None:
        self._add("FAIL", name, detail)

    def empty(self, name: str, detail: str) -> None:
        self._add("EMPTY", name, detail)

    def skip(self, name: str, detail: str) -> None:
        self._add("SKIP", name, detail)

    def count(self, kind: str) -> int:
        return sum(1 for k, _, _ in self.rows if k == kind)


def near(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


def has_cjk(text: str) -> bool:
    return any(lo <= ord(ch) <= hi for ch in text for lo, hi in CJK_RANGES)


def mint_cookie(secret: str, user: str) -> str:
    """Ký một cookie phiên đúng định dạng Starlette SessionMiddleware."""
    payload = base64.b64encode(json.dumps({"user": user}).encode())
    return itsdangerous.TimestampSigner(secret).sign(payload).decode()


# --------------------------------------------------------------------------- #
# Các nhóm kiểm
# --------------------------------------------------------------------------- #


def check_health(rep: Report, anon: httpx.Client) -> dict[str, Any] | None:
    r = anon.get("/api/health")
    if r.status_code not in (200, 503):
        rep.fail("health", f"trả {r.status_code}, cần 200 hoặc 503")
        return None
    h = r.json()
    if not h["database"]["ok"]:
        rep.fail("health · database", str(h["database"]["detail"]))
        return h
    if not h["migration"]["ok"]:
        rep.fail("health · migration", str(h["migration"]["detail"]))
        return h
    total, on, off = h["terminals_total"], h["terminals_online"], h["terminals_offline"]
    if on + off != total:
        rep.fail("health · đếm bồn", f"{on} trực tuyến + {off} ngoại tuyến != {total}")
        return h
    if total == 0:
        rep.empty("health", "chưa có bồn nào — không kiểm được gì phía sau")
        return h
    rep.ok("health", f"DB ok · migration khớp · {total} bồn ({on} trực tuyến)")
    return h


def check_ui(rep: Report, anon: httpx.Client) -> None:
    """Kiểm NỘI DUNG trang, gồm cả các nhãn vừa chuẩn hoá.

    Nhãn cũ còn trên server nghĩa là bản deploy đang chạy cũ hơn source — lỗi trước
    đây chỉ phát hiện được bằng mắt.
    """
    r = anon.get("/ui/")
    if r.status_code != 200:
        rep.fail("giao diện", f"/ui/ trả {r.status_code}")
        return
    page = r.text
    required = ("<!DOCTYPE html>", "GAS Nhiên Liệu Xanh", 'id="tank-list"')
    missing = [s for s in required if s not in page]
    if missing:
        rep.fail("giao diện", f"thiếu trong trang: {missing}")
        return

    # So khớp nhãn ĐÃ RENDER, không so khớp cả file: comment HTML giải thích lần đổi
    # tên có chứa chính chuỗi cũ, nên tìm thô sẽ luôn báo "còn nhãn cũ". Dùng mốc
    # thẻ (">nhãn<") và chuỗi trong template JS để chỉ bắt phần thật sự hiện ra.
    body = re.sub(r"<!--.*?-->", "", page, flags=re.S)
    want = (">Còn lại<", "Đo cuối:", ">Số liệu cũ<", ">Bản đồ<")
    obsolete = (">Tới dự trữ<", ">Lỗi thời<", "Dữ liệu lỗi thời")
    absent = [s for s in want if s not in body]
    lingering = [s for s in obsolete if s in body]
    if absent or lingering:
        rep.fail(
            "giao diện · nhãn",
            f"thiếu nhãn mới {absent} / còn nhãn cũ {lingering} — bản deploy cũ hơn source",
        )
        return
    rep.ok("giao diện", f"{len(page) // 1024} KB · nhãn đã chuẩn hoá")


# Vùng biển hở nằm trong yêu sách đường lưỡi bò, cách bờ và cách mọi đảo.
_BIEN_DONG = ((14.0, 114.0), (18.0, 116.0), (10.0, 113.0), (8.0, 110.0), (16.0, 112.5))


def _phu_boi(fc: dict[str, Any], lat: float, lon: float) -> list[str]:
    """Ray casting: những vùng nào phủ điểm (lat, lon)."""

    def trong(ring: list[list[float]]) -> bool:
        c = False
        n = len(ring)
        for i in range(n):
            x1, y1 = ring[i]
            x2, y2 = ring[(i + 1) % n]
            if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
                c = not c
        return c

    out = []
    for f in fc["features"]:
        for poly in f["geometry"]["coordinates"]:
            if trong(poly[0]) and not any(trong(r) for r in poly[1:]):
                out.append(f["properties"]["n"])
                break
    return out


def check_map(rep: Report, anon: httpx.Client) -> None:
    """Nền bản đồ ĐANG CHẠY không có đường lưỡi bò.

    tests/test_world_map.py đã canh file trong repo. Phép kiểm này canh thứ khác:
    bản thật sự được deploy. Một lần deploy thiếu file, hay ``includeFiles`` của
    Vercel không gói ``app/static/**``, sẽ ra một trang Bản đồ trắng mà không endpoint
    nào báo lỗi — đúng lớp bug "chỉ lộ ra khi chạy thật" mà script này tồn tại để bắt.
    """
    r = anon.get("/ui/world-vi.geojson")
    if r.status_code != 200:
        rep.fail("bản đồ · nền", f"/ui/world-vi.geojson trả {r.status_code}")
        return
    try:
        fc = r.json()
    except ValueError as exc:
        rep.fail("bản đồ · nền", f"không phải JSON: {exc}")
        return
    if fc.get("type") != "FeatureCollection" or not fc.get("features"):
        rep.fail("bản đồ · nền", "không phải FeatureCollection có dữ liệu")
        return

    # Điểm đối chứng TRƯỚC: nếu thuật toán không chạy thì phép thử biển vô nghĩa.
    if _phu_boi(fc, 21.028, 105.854) != ["Việt Nam"]:
        rep.fail("bản đồ · nền", "điểm đối chứng Hà Nội không ra Việt Nam")
        return
    ve_tren_bien = [
        f"{la}N/{lo}E thuộc {who}"
        for la, lo in _BIEN_DONG
        if (who := _phu_boi(fc, la, lo))
    ]
    if ve_tren_bien:
        rep.fail("bản đồ · nền", f"có yêu sách vẽ trên biển hở: {ve_tren_bien}")
        return

    kinds = {f["geometry"]["type"] for f in fc["features"]}
    if kinds != {"MultiPolygon"}:
        # Một yêu sách vẽ dạng ĐƯỜNG sẽ lọt qua phép thử điểm-trong-đa-giác.
        rep.fail("bản đồ · nền", f"có hình học lạ ngoài MultiPolygon: {kinds}")
        return

    rep.ok(
        "bản đồ · nền",
        f"{len(fc['features'])} vùng · {len(r.content) // 1024} KB · "
        f"{len(_BIEN_DONG)} điểm trên Biển Đông đều trống",
    )


def check_guards(rep: Report, anon: httpx.Client, psn: str) -> None:
    """Không phiên -> đúng 401 ở mọi endpoint dữ liệu. Không nhận 5xx thay thế."""
    bad = []
    for path in PROTECTED:
        url = path.replace("{psn}", psn)
        code = anon.get(url).status_code
        if code != 401:
            bad.append(f"{url}={code}")
    if bad:
        rep.fail("guard 401", f"không trả 401: {bad}")
    else:
        rep.ok("guard 401", f"{len(PROTECTED)} endpoint đều chặn truy cập vô danh")


def check_login_rejects_bad_password(rep: Report, anon: httpx.Client) -> None:
    """Mật khẩu sai -> 401. Kiểm được mà KHÔNG cần biết mật khẩu đúng.

    Chỉ thử MỘT lần: server chặn 5 lần sai / 10 phút / IP, và lượt thử này tiêu vào
    hạn mức của chính người đang chạy script.
    """
    r = anon.post(
        "/api/auth/login",
        json={"username": "e2e-khong-ton-tai", "password": "mat-khau-sai"},
    )
    if r.status_code == 429:
        rep.skip("đăng nhập · từ chối sai", "đang bị chặn tần suất (5 lần/10 phút)")
    elif r.status_code == 401:
        rep.ok("đăng nhập · từ chối sai", "mật khẩu sai bị từ chối 401 (đã dùng 1/5 lượt)")
    else:
        rep.fail("đăng nhập · từ chối sai", f"trả {r.status_code}, cần 401")


def authenticate(
    rep: Report, base: str, user: str | None, password: str | None, secret: str
) -> httpx.Client:
    """Đăng nhập thật nếu có credential; nếu không thì tự ký cookie và báo SKIP."""
    if user and password:
        c = httpx.Client(base_url=base, timeout=30.0)
        r = c.post("/api/auth/login", json={"username": user, "password": password})
        if r.status_code == 200 and r.json().get("username"):
            me = c.get("/api/auth/me")
            if me.status_code == 200 and me.json()["username"] == r.json()["username"]:
                rep.ok("đăng nhập thật", f"phiên hoạt động cho {me.json()['username']!r}")
                return c
            rep.fail("đăng nhập thật", f"/api/auth/me trả {me.status_code} sau khi đăng nhập")
        else:
            rep.fail("đăng nhập thật", f"/api/auth/login trả {r.status_code}")
        c.close()

    rep.skip(
        "đăng nhập thật",
        "không có E2E_USERNAME/E2E_PASSWORD — dùng cookie tự ký, luồng đăng nhập "
        "KHÔNG được kiểm trong lượt này",
    )
    return httpx.Client(
        base_url=base, cookies={"nlx_session": mint_cookie(secret, "e2e")}, timeout=30.0
    )


def check_counts_agree(
    rep: Report, c: httpx.Client, health: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Ba nguồn độc lập phải nói cùng một số lượng bồn."""
    terms = c.get("/api/terminals").json()
    summary = c.get("/api/stats/summary").json()
    items = terms["items"]

    if len(items) != terms["total"]:
        rep.fail("đếm bồn", f"items={len(items)} nhưng total={terms['total']}")
        return items
    if summary["total"] != terms["total"]:
        rep.fail("đếm bồn", f"summary={summary['total']} nhưng terminals={terms['total']}")
        return items
    if health and health["terminals_total"] != terms["total"]:
        rep.fail("đếm bồn", f"health={health['terminals_total']} vs terminals={terms['total']}")
        return items
    if summary["online"] + summary["offline"] != summary["total"]:
        rep.fail("đếm bồn", "summary: trực tuyến + ngoại tuyến != tổng")
        return items
    rep.ok("đếm bồn", f"health / terminals / summary đều nói {terms['total']} bồn")
    return items


def check_fill_scale(rep: Report, items: list[dict[str, Any]]) -> None:
    """``fill_percent`` phải bằng ``volume_l / capacity_l x 100``.

    Phép so này bắt lỗi thang 0-1 vs 0-100 — lỗi không CHECK constraint nào bắt được
    vì 0.59 hợp lệ ở cả hai thang.
    """
    checked, bad = 0, []
    for t in items:
        vol, cap, pct = t.get("volume_l"), t.get("capacity_l"), t.get("fill_percent")
        if vol is None or cap is None or pct is None:
            continue
        expect = float(vol) / float(cap) * 100.0
        checked += 1
        if not near(float(pct), expect, 0.01):
            bad.append(f"{t['psn']}: fill={pct} nhưng {vol}/{cap}x100={expect:.4f}")
    if bad:
        rep.fail("thang fill_percent", "; ".join(bad))
    elif checked == 0:
        rep.empty("thang fill_percent", "không bồn nào có đủ volume_l + capacity_l")
    else:
        rep.ok("thang fill_percent", f"{checked} bồn khớp volume/capacity x 100")


def check_telemetry(rep: Report, c: httpx.Client, psn: str) -> None:
    """Thứ tự sắp xếp và tính nhất quán latest vs history, trên dữ liệu thật.

    Tham số đúng là ``order=asc|desc``. Bản trước truyền ``ascending=false`` — một
    tham số KHÔNG tồn tại; FastAPI bỏ qua nó im lặng nên test "kiểm thứ tự" thực chất
    không kiểm thứ tự nào.
    """
    asc = c.get(f"/api/telemetry/{psn}?order=asc&limit=200")
    desc = c.get(f"/api/telemetry/{psn}?order=desc&limit=200")
    if asc.status_code != 200 or desc.status_code != 200:
        rep.fail("telemetry", f"asc={asc.status_code} desc={desc.status_code}")
        return

    a, d = asc.json()["items"], desc.json()["items"]
    if not a:
        rep.empty(
            "telemetry",
            f"{psn}: 0 điểm trong cửa sổ mặc định — không kiểm được thứ tự lẫn tính "
            "nhất quán (thiết bị đang không báo)",
        )
        return

    ta = [x["sampled_at"] for x in a]
    td = [x["sampled_at"] for x in d]
    if ta != sorted(ta):
        rep.fail("telemetry · order=asc", "không tăng dần")
        return
    if td != sorted(td, reverse=True):
        rep.fail("telemetry · order=desc", "không giảm dần")
        return
    if ta[-1] != td[0]:
        rep.fail("telemetry · order", f"mẫu mới nhất khác nhau: {ta[-1]} vs {td[0]}")
        return

    latest = c.get(f"/api/telemetry/{psn}/latest").json()
    if latest is None:
        rep.fail("telemetry · latest", "history có dữ liệu nhưng /latest trả null")
        return
    if latest["sampled_at"] != td[0]:
        rep.fail(
            "telemetry · latest",
            f"/latest {latest['sampled_at']} khác mẫu mới nhất {td[0]}",
        )
        return
    rep.ok("telemetry", f"{psn}: {len(a)} điểm · asc/desc đúng chiều · latest khớp history")


def check_forecast_math(rep: Report, c: httpx.Client, values: dict[str, Any]) -> None:
    """Số học nội tại của dự báo — tính tay được từ cấu hình đang lưu."""
    forecasts = c.get("/api/forecast").json()
    if not forecasts:
        rep.empty("số học dự báo", "/api/forecast trả rỗng")
        return

    res_pct = float(values["forecast_reserve_percent"])
    fill_pct = float(values["lng_max_fill_percent"])
    problems, verified = [], 0

    for f in forecasts:
        psn, cap = f["psn"], f.get("capacity_l")
        if not cap:
            continue
        verified += 1

        expect_reserve = float(cap) * res_pct / 100.0
        if not near(float(f["reserve_l"]), expect_reserve, 0.01):
            problems.append(f"{psn}: reserve_l={f['reserve_l']} != {expect_reserve:.2f}")

        h = f["hold"]
        if h.get("current_mpa") is not None and h.get("relief_mpa") is not None:
            expect_head = float(h["relief_mpa"]) - float(h["current_mpa"])
            if not near(float(h["headroom_mpa"]), expect_head, 1e-6):
                problems.append(f"{psn}: headroom={h['headroom_mpa']} != {expect_head:.4f}")

        sg, r = f["suggestion"], f["runout"]
        if sg.get("target_l"):
            expect_target = float(cap) * fill_pct / 100.0
            if not near(float(sg["target_l"]), expect_target, 0.01):
                problems.append(f"{psn}: target_l={sg['target_l']} != {expect_target:.2f}")

        d_res, d_empty = r.get("days_to_reserve"), r.get("days_to_empty")
        if d_res is not None and d_empty is not None and float(d_empty) < float(d_res):
            problems.append(f"{psn}: tới cạn ({d_empty}) sớm hơn tới dự trữ ({d_res})")

        # Bồn dưới mức dự trữ PHẢI có days_to_reserve = 0, không phải None hay số âm.
        vol = f.get("volume_l")
        below = vol is not None and float(vol) < expect_reserve
        if below and (d_res is None or float(d_res) != 0.0):
            problems.append(f"{psn}: dưới mức dự trữ nhưng days_to_reserve={d_res}")

    if problems:
        rep.fail("số học dự báo", "; ".join(problems))
    elif verified == 0:
        rep.empty("số học dự báo", "không bồn nào khai dung tích")
    else:
        rep.ok(
            "số học dự báo",
            f"{verified} bồn: dự trữ / khoảng trống áp / mức đích / thứ tự hai mốc đều khớp",
        )


def check_alerts_match_data(
    rep: Report, c: httpx.Client, items: list[dict[str, Any]], values: dict[str, Any]
) -> None:
    """Mỗi cảnh báo phải tương ứng một sự thật trong dữ liệu bồn, và ngược lại."""
    alerts = c.get("/api/alerts").json()
    by_psn = {t["psn"]: t for t in items}
    low_th = float(values["alert_low_volume_percent"])
    problems = []

    for a in alerts:
        t = by_psn.get(a["psn"])
        if t is None:
            problems.append(f"cảnh báo cho PSN lạ {a['psn']}")
            continue
        if a["code"] == "LOW_VOLUME":
            pct = t.get("fill_percent")
            if pct is None or float(pct) >= low_th:
                problems.append(f"{a['psn']}: LOW_VOLUME nhưng fill={pct} >= ngưỡng {low_th}")
        elif a["code"] == "OFFLINE" and t.get("status") != "offline":
            problems.append(f"{a['psn']}: OFFLINE nhưng status={t.get('status')}")

    # Chiều ngược lại quan trọng hơn: bồn dưới ngưỡng mà KHÔNG có cảnh báo là im lặng
    # đúng lúc cần nói.
    for t in items:
        pct = t.get("fill_percent")
        low = pct is not None and float(pct) < low_th
        warned = any(a["psn"] == t["psn"] and a["code"] == "LOW_VOLUME" for a in alerts)
        if low and not warned:
            problems.append(f"{t['psn']}: fill={pct} dưới ngưỡng mà KHÔNG có cảnh báo")

    if problems:
        rep.fail("cảnh báo khớp dữ liệu", "; ".join(problems))
    elif not alerts:
        rep.empty("cảnh báo khớp dữ liệu", "không có cảnh báo nào để đối chiếu")
    else:
        rep.ok("cảnh báo khớp dữ liệu", f"{len(alerts)} cảnh báo, đối chiếu hai chiều đều đúng")


def check_delivery_plan(rep: Report, c: httpx.Client, items: list[dict[str, Any]]) -> None:
    """Tổng phải bằng tổng các chuyến, và bằng tổng các điểm giao."""
    p = c.get("/api/delivery-plan?horizon_days=7").json()
    trips = p.get("trips") or []
    if not trips:
        rep.empty("lịch giao", "0 chuyến trong 7 ngày tới — không kiểm được phép gom")
        return

    known = {t["psn"] for t in items}
    problems, sum_trips, total_stops = [], 0.0, 0
    for t in trips:
        stop_sum = sum(float(s["order_l"]) for s in t["stops"])
        if not near(float(t["total_l"]), stop_sum, 0.01):
            problems.append(f"chuyến {t['seq']}: total_l={t['total_l']} != tổng điểm {stop_sum:.2f}")
        if float(t["total_l"]) > float(t["truck_capacity_l"]) + 1e-6:
            problems.append(f"chuyến {t['seq']}: {t['total_l']} vượt tải {t['truck_capacity_l']}")
        for s in t["stops"]:
            if s["psn"] not in known:
                problems.append(f"chuyến {t['seq']}: điểm giao PSN lạ {s['psn']}")
        sum_trips += float(t["total_l"])
        total_stops += len(t["stops"])

    if not near(float(p["total_l"]), sum_trips, 0.01):
        problems.append(f"total_l={p['total_l']} != tổng các chuyến {sum_trips:.2f}")
    if int(p["stops"]) != total_stops:
        problems.append(f"stops={p['stops']} != số điểm giao thật {total_stops}")

    if problems:
        rep.fail("lịch giao", "; ".join(problems))
    else:
        rep.ok(
            "lịch giao",
            f"{len(trips)} chuyến · {total_stops} điểm · {sum_trips / 1000:.2f} m³ · tổng khớp mọi cấp",
        )


def check_exports(rep: Report, c: httpx.Client, items: list[dict[str, Any]], psn: str) -> None:
    """CSV phải chứa ĐÚNG dữ liệu của API, không chỉ đúng BOM và tên cột."""
    r = c.get("/api/export/tanks.csv?delimiter=comma")
    if r.status_code != 200:
        rep.fail("xuất CSV", f"tanks.csv trả {r.status_code}")
        return
    if not r.content.startswith(b"\xef\xbb\xbf"):
        rep.fail("xuất CSV", "tanks.csv thiếu BOM UTF-8 (Excel tiếng Việt sẽ hỏng)")
        return

    rows = r.content.decode("utf-8-sig").strip().splitlines()[1:]
    if len(rows) != len(items):
        rep.fail("xuất CSV", f"tanks.csv có {len(rows)} dòng nhưng API có {len(items)} bồn")
        return
    absent = [t["psn"] for t in items if not any(t["psn"] in row for row in rows)]
    if absent:
        rep.fail("xuất CSV", f"tanks.csv thiếu PSN {absent}")
        return

    # Dấu phân tách phải thật sự đổi, không chỉ được nhận.
    semi = c.get("/api/export/tanks.csv?delimiter=semicolon").content.decode("utf-8-sig")
    if ";" not in semi.splitlines()[0]:
        rep.fail("xuất CSV", "delimiter=semicolon không đổi dấu phân tách")
        return

    tel = c.get(f"/api/export/telemetry.csv?psn={psn}&delimiter=tab")
    tel_rows = len(tel.content.decode("utf-8-sig").strip().splitlines()) - 1
    detail = f"{len(rows)} bồn khớp API · đổi được dấu phân tách · telemetry {tel_rows} dòng"
    if tel_rows <= 0:
        rep.empty("xuất CSV", detail + " (không kiểm được nội dung telemetry)")
    else:
        rep.ok("xuất CSV", detail)


def check_settings(rep: Report, c: httpx.Client) -> dict[str, Any] | None:
    r = c.get("/api/settings")
    if r.status_code != 200:
        rep.fail("cài đặt", f"trả {r.status_code}")
        return None
    d = r.json()
    if "smtp_password" in d["values"]:
        rep.fail("cài đặt", "smtp_password LỘ trong values — phải chỉ ghi, không đọc")
        return None
    odd = {k: v for k, v in d["sources"].items() if v not in ("env", "app")}
    if odd:
        rep.fail("cài đặt", f"nguồn không hợp lệ: {odd}")
        return None
    # `sources` được phép NHIỀU HƠN `values` đúng ở các field bí mật: chúng chỉ ghi
    # được, không đọc lại, nên có nhãn nguồn mà không có giá trị là hành vi đúng.
    # Mọi khoá lệch KHÁC mới là lỗi.
    write_only = {"smtp_password"}
    extra = set(d["sources"]) - set(d["values"]) - write_only
    if extra or set(d["values"]) - set(d["sources"]):
        rep.fail(
            "cài đặt",
            f"khoá lệch ngoài field bí mật: chỉ-sources={sorted(extra)}, "
            f"chỉ-values={sorted(set(d['values']) - set(d['sources']))}",
        )
        return None
    if not write_only & set(d["sources"]):
        rep.fail("cài đặt", "smtp_password không có nhãn nguồn — không sửa được từ app")
        return None
    n_app = sum(1 for v in d["sources"].values() if v == "app")
    n = len(d["values"])
    rep.ok(
        "cài đặt",
        f"{n} tham số · {n_app} đặt trong app / {n - n_app} từ môi trường · bí mật không lộ",
    )
    return d["values"]


def check_admin_guards(
    rep: Report, c: httpx.Client, anon: httpx.Client, token: str | None
) -> None:
    """Guard admin và cron: assert đúng 401, và kiểm token qua endpoint CHỈ ĐỌC.

    Cron có HAI trạng thái hợp lệ, phân biệt bằng ý nghĩa chứ không bằng cách nhận
    một khoảng mã: chưa cấu hình ``CRON_SECRET`` thì endpoint tự tắt và trả 503; đã
    cấu hình thì nó phải trả đúng 401 cho token sai. Nhận ``in (401, 503)`` như bản
    trước thì một endpoint tắt cũng đọc thành "bảo mật đã xác thực".
    """
    problems = []
    no_tok = anon.get("/api/cron/ingest").status_code
    if no_tok == 503:
        rep.skip("guard cron", "CRON_SECRET chưa cấu hình trên server này — cron đang tắt")
    elif no_tok != 401:
        problems.append(f"cron không token = {no_tok}, cần 401 (đang bật) hoặc 503 (đang tắt)")
    else:
        bad_tok = anon.get(
            "/api/cron/ingest", headers={"Authorization": "Bearer sai"}
        ).status_code
        if bad_tok != 401:
            problems.append(f"cron token sai = {bad_tok}, cần 401")
        else:
            rep.ok("guard cron", "từ chối cả không-token và token-sai bằng 401")

    if c.post("/api/admin/notify/run", headers={"X-Admin-Token": "sai"}).status_code != 401:
        problems.append("admin token sai != 401")
    if problems:
        rep.fail("guard admin/cron", "; ".join(problems))
        return

    rep.ok("guard admin · từ chối", "token sai bị chặn 401")
    if not token:
        rep.skip("guard admin · chấp nhận", "không có token nào để thử")
        return

    # Endpoint CHỈ ĐỌC. Cố ý KHÔNG gọi /api/admin/notify/run: nó gửi email thật.
    r = c.get("/api/admin/ingest/runs?limit=1", headers={"X-Admin-Token": token})
    if r.status_code == 200:
        rep.ok("guard admin · chấp nhận", "token đúng được nhận (qua endpoint chỉ đọc)")
        return
    if r.status_code == 401:
        # KHÔNG kết luận "guard hỏng". Token mặc định lấy từ .env của MÁY NÀY, còn
        # đích mặc định là production — hai giá trị khác nhau là chuyện thường (luân
        # chuyển token, môi trường khác nhau). Bị từ chối thì không phân biệt được
        # "token sai" với "guard chặn tất", nên kết quả trung thực là chưa kiểm được.
        rep.skip(
            "guard admin · chấp nhận",
            "token đang có bị từ chối — rất có thể không phải token của server này; "
            "truyền --admin-token / E2E_ADMIN_TOKEN đúng để kiểm mục này",
        )
        return
    rep.fail("guard admin · chấp nhận", f"trả {r.status_code}, cần 200 hoặc 401")


def check_no_vendor_leak(rep: Report, c: httpx.Client, psn: str) -> None:
    # telemetry.csv bắt buộc có ?psn= (thiếu thì 422), nên phải gắn tham số ở đây —
    # không thì quét rò báo lỗi 422 thay vì kiểm nội dung endpoint đó.
    paths = [p.replace("{psn}", psn) for p in PROTECTED] + ["/api/health"]
    paths = [
        f"{p}?psn={psn}" if p.endswith("/export/telemetry.csv") else p for p in paths
    ]
    problems = []
    for p in paths:
        r = c.get(p)
        if r.status_code != 200:
            problems.append(f"{p}={r.status_code}")
            continue
        body = r.text
        for needle in ("raw_payload", "xingke", "xk-iot"):
            if needle in body.lower():
                problems.append(f"{p}: rò {needle!r}")
        if has_cjk(body):
            problems.append(f"{p}: có ký tự CJK")
    if problems:
        rep.fail("cô lập vendor", "; ".join(problems))
    else:
        rep.ok(
            "cô lập vendor",
            f"{len(paths)} endpoint: không tên vendor, không CJK, không raw_payload",
        )


def check_writes(rep: Report, c: httpx.Client, psn: str) -> None:
    """Ghi rồi phục hồi, và ASSERT phục hồi khớp baseline."""
    base = c.get(f"/api/terminals/{psn}").json()
    b_name, b_cap = base.get("name"), base.get("capacity_l")

    probe = 15000.0
    if b_cap is not None and near(float(b_cap), probe, 0.5):
        probe = 12345.0  # không thử bằng đúng giá trị đang có: sẽ không chứng minh gì

    up = c.patch(f"/api/terminals/{psn}", json={"capacity_l": probe})
    if up.status_code != 200 or not near(float(up.json()["capacity_l"]), probe, 0.01):
        rep.fail("ghi · dung tích", f"PATCH trả {up.status_code}, không nhận giá trị mới")
        return

    back = c.patch(f"/api/terminals/{psn}", json={"capacity_l": b_cap, "name": b_name})
    after = c.get(f"/api/terminals/{psn}").json()
    if back.status_code != 200:
        rep.fail("ghi · dung tích", f"PHỤC HỒI THẤT BẠI ({back.status_code}) — {psn} còn {probe:g}")
        return
    if str(after.get("capacity_l")) != str(b_cap) or after.get("name") != b_name:
        rep.fail(
            "ghi · dung tích",
            f"phục hồi lệch baseline: dung tích {after.get('capacity_l')} vs {b_cap}, "
            f"tên {after.get('name')!r} vs {b_name!r}",
        )
        return
    rep.ok("ghi · dung tích", f"{psn}: đặt {probe:g} rồi phục hồi đúng baseline {b_cap}")

    # Cài đặt: phục hồi ĐÚNG NGUỒN. Ghi lại giá trị cũ khi nguồn là "env" sẽ tạo một
    # override mới — trông như phục hồi nhưng đã đổi vĩnh viễn nhãn nguồn.
    s = c.get("/api/settings").json()
    key = "forecast_lead_time_days"
    b_src, b_val = s["sources"][key], s["values"][key]

    r1 = c.patch("/api/settings", json={key: float(b_val) + 1.5})
    if r1.status_code != 200 or r1.json()["sources"][key] != "app":
        rep.fail("ghi · cài đặt", f"PATCH trả {r1.status_code}, nguồn không chuyển sang app")
        return
    r2 = c.patch("/api/settings", json={key: None if b_src == "env" else b_val})
    got = r2.json()
    if got["sources"][key] != b_src or float(got["values"][key]) != float(b_val):
        rep.fail(
            "ghi · cài đặt",
            f"phục hồi lệch: nguồn {got['sources'][key]} vs {b_src}, "
            f"giá trị {got['values'][key]} vs {b_val}",
        )
        return
    rep.ok("ghi · cài đặt", f"{key}: đổi rồi phục hồi đúng nguồn {b_src!r} và giá trị {b_val}")


# --------------------------------------------------------------------------- #


def main() -> int:
    ap = argparse.ArgumentParser(description="E2E kiểm nội dung trên server đang chạy")
    ap.add_argument("url", nargs="?", default=PROD, help=f"mặc định {PROD}")
    ap.add_argument("--user", default=os.environ.get("E2E_USERNAME"))
    ap.add_argument("--password", default=os.environ.get("E2E_PASSWORD"))
    ap.add_argument(
        "--admin-token",
        default=os.environ.get("E2E_ADMIN_TOKEN"),
        help="ADMIN_TOKEN của SERVER ĐÍCH. Không truyền thì lấy từ .env của máy này, "
        "vốn thường khác server production.",
    )
    ap.add_argument(
        "--allow-writes",
        action="store_true",
        help="cho phép PATCH dữ liệu (có baseline + assert phục hồi). Mặc định TẮT.",
    )
    args = ap.parse_args()

    settings = get_settings()
    base = args.url.rstrip("/")
    rep = Report()

    print("=" * 78)
    print(f"E2E trên {base}")
    print(f"chế độ ghi: {'BẬT' if args.allow_writes else 'TẮT (chỉ đọc)'}")
    print("=" * 78)

    anon = httpx.Client(base_url=base, timeout=30.0)
    health = check_health(rep, anon)
    check_ui(rep, anon)
    check_map(rep, anon)
    check_login_rejects_bad_password(rep, anon)

    c = authenticate(rep, base, args.user, args.password, settings.session_secret)
    try:
        items = check_counts_agree(rep, c, health)
        if not items:
            rep.empty("phần còn lại", "không có bồn nào — mọi kiểm tra phía sau vô nghĩa")
        else:
            psn = items[0]["psn"]
            check_guards(rep, anon, psn)
            check_fill_scale(rep, items)
            values = check_settings(rep, c)
            check_telemetry(rep, c, psn)
            if values:
                check_forecast_math(rep, c, values)
                check_alerts_match_data(rep, c, items, values)
            check_delivery_plan(rep, c, items)
            check_exports(rep, c, items, psn)
            check_admin_guards(rep, c, anon, args.admin_token or settings.admin_token)
            check_no_vendor_leak(rep, c, psn)
            if args.allow_writes:
                check_writes(rep, c, psn)
            else:
                rep.skip("ghi dữ liệu", "chạy với --allow-writes để kiểm PATCH + phục hồi")
    finally:
        c.close()
        anon.close()

    n_pass, n_fail = rep.count("PASS"), rep.count("FAIL")
    n_empty, n_skip = rep.count("EMPTY"), rep.count("SKIP")
    print()
    print("=" * 78)
    print(
        f"{n_pass} đối chiếu được · {n_fail} sai · "
        f"{n_empty} rỗng không kiểm được · {n_skip} bỏ qua"
    )
    print("=" * 78)
    for kind, name, detail in rep.rows:
        if kind in ("FAIL", "EMPTY"):
            print(f"  {kind}: {name} — {detail}")
    if n_fail:
        print(f"\n>>> {n_fail} MỤC SAI <<<")
    elif n_empty:
        # Chỗ bản trước nói dối: rỗng không phải là đạt.
        print(f"\n>>> KHÔNG MỤC NÀO SAI, nhưng {n_empty} mục KHÔNG KIỂM ĐƯỢC vì dữ liệu rỗng <<<")
    else:
        print("\n>>> MỌI MỤC ĐỀU ĐỐI CHIẾU ĐƯỢC VÀ ĐÚNG <<<")
    return n_fail


if __name__ == "__main__":
    sys.exit(main())
