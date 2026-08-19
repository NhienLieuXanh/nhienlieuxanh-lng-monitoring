"""Đối chiếu timezone vendor. PHẢI PASS trước khi backfill lịch sử.

Vendor gửi timestamp naive (không offset). Sai TZ không làm sai hiển thị — nó
làm hỏng khoá dedup ``(psn, sampled_at)``: sửa parsing về sau thì mọi dòng có
khoá khác, ON CONFLICT không match, và toàn bộ lịch sử bị nhân đôi âm thầm.

Cách đo (đã dùng 2026-08-18, xem DISCOVERY.md mục 5): gọi GET /ls/login (405)
lấy field ``timestamp`` của Spring Cloud Gateway, so với UTC đo tại chỗ. TZ nào
khiến naive-timestamp-gắn-TZ khớp UTC thì thắng.

    .venv\\Scripts\\python.exe scripts\\verify_tz.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

# Cho phép `python scripts/verify_tz.py` tìm được package app.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

UTC = ZoneInfo("UTC")
CANDIDATE_TZS: tuple[str, ...] = (
    "UTC",
    "Asia/Shanghai",
    "Asia/Ho_Chi_Minh",
)
# Lệch ≤ 30s = khớp (latency + lệch đồng hồ). 1 giờ là bác bỏ dứt khoát.
MATCH_SLACK = timedelta(seconds=30)


def parse_gateway_timestamp(body: dict) -> datetime:
    """Lấy timestamp naive từ body Gateway. Raise ValueError nếu không có."""
    raw = body.get("timestamp")
    if not raw or not isinstance(raw, str):
        raise ValueError(f"body Gateway không có timestamp string: {body!r}"[:240])
    try:
        parsed = datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            parsed = datetime.fromisoformat(raw.strip())
        except ValueError as exc:
            raise ValueError(f"không parse được timestamp {raw!r}") from exc
    if parsed.tzinfo is not None:
        # Gateway đã xác minh gửi naive. Nếu một ngày có offset thì so sánh
        # trực tiếp với UTC, không giả định TZ.
        return parsed
    return parsed


def score_timezones(
    naive: datetime, utc_now: datetime
) -> list[tuple[str, float, bool]]:
    """Với mỗi TZ ứng viên: gắn vào naive, đổi sang UTC, đo lệch giây.

    Trả về list (tz_name, delta_seconds, is_match) sắp theo |delta| tăng dần.
    """
    if utc_now.tzinfo is None:
        raise ValueError("utc_now phải tz-aware")
    utc_now = utc_now.astimezone(UTC)
    if naive.tzinfo is not None:
        # Đã aware: chỉ còn một phép đo, không giả định TZ.
        delta = abs((naive.astimezone(UTC) - utc_now).total_seconds())
        return [("attached-offset", delta, delta <= MATCH_SLACK.total_seconds())]

    scored: list[tuple[str, float, bool]] = []
    for name in CANDIDATE_TZS:
        assumed = naive.replace(tzinfo=ZoneInfo(name)).astimezone(UTC)
        delta = abs((assumed - utc_now).total_seconds())
        scored.append((name, delta, delta <= MATCH_SLACK.total_seconds()))
    scored.sort(key=lambda x: x[1])
    return scored


def verdict(configured_tz: str, scores: list[tuple[str, float, bool]]) -> tuple[bool, str]:
    """True nếu TZ đang cấu hình là một trong các giả thiết khớp."""
    matches = [name for name, _, ok in scores if ok]
    if not scores:
        return False, "không có giả thiết nào để đối chiếu"
    best_name, best_delta, _ = scores[0]
    if configured_tz in matches:
        return True, (
            f"XINGKE_VENDOR_TZ={configured_tz} KHỚP bằng chứng "
            f"(lệch {best_delta:.1f}s với giả thiết tốt nhất {best_name})"
        )
    if matches:
        return False, (
            f"XINGKE_VENDOR_TZ={configured_tz} SAI. "
            f"Bằng chứng ủng hộ {matches[0]} (lệch {best_delta:.1f}s). "
            f"Sửa .env rồi chạy lại — ĐỪNG backfill khi chưa khớp."
        )
    return False, (
        f"không giả thiết nào khớp (tốt nhất {best_name} lệch {best_delta:.0f}s). "
        f"Kiểm tra đồng hồ máy, hoặc vendor đổi formatter."
    )


def _fetch_gateway_body(base_url: str, timeout: float = 15.0) -> dict:
    url = base_url.rstrip("/") + "/login"
    # GET /login cố ý: vendor trả 405 + body Gateway có `timestamp` naive.
    resp = httpx.get(
        url,
        timeout=timeout,
        follow_redirects=False,
        headers={"Accept": "application/json", "X-Requsted-With": "XMLHttpRequst"},
    )
    try:
        body = resp.json()
    except Exception as exc:
        raise RuntimeError(
            f"GET {url} HTTP {resp.status_code} không phải JSON: {resp.text[:200]!r}"
        ) from exc
    if not isinstance(body, dict):
        raise RuntimeError(f"GET {url} trả về non-object: {body!r}"[:240])
    return body


def main() -> int:
    from app.config import assert_venv

    assert_venv()
    from app.adapters.xingke.config import get_xingke_settings

    settings = get_xingke_settings()
    utc_now = datetime.now(tz=UTC)
    print(f"cửa sổ UTC tại chỗ : {utc_now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"XINGKE_VENDOR_TZ   : {settings.vendor_tz}")
    print(f"base URL           : {settings.base_url}")

    try:
        body = _fetch_gateway_body(settings.base_url)
    except Exception as exc:
        print(f"THẤT BẠI: không lấy được timestamp Gateway: {exc}", file=sys.stderr)
        return 2

    print("body Gateway:")
    print(json.dumps(body, ensure_ascii=False, indent=2)[:800])

    try:
        naive = parse_gateway_timestamp(body)
    except ValueError as exc:
        print(f"THẤT BẠI: {exc}", file=sys.stderr)
        return 2

    print(f"timestamp vendor   : {naive.isoformat(sep=' ')}  (naive)")
    scores = score_timezones(naive, utc_now)
    print("\ngiả thiết:")
    for name, delta, ok in scores:
        mark = "KHỚP" if ok else "lệch"
        hours = delta / 3600.0
        print(f"  {name:<22} {mark:5}  Δ={delta:7.1f}s  ({hours:+.2f}h)")

    ok, msg = verdict(settings.vendor_tz, scores)
    print()
    print(("OK  " if ok else "SAI ") + msg)
    if not ok:
        print(
            "\nKhông backfill khi script này fail. Sai TZ nhân đôi toàn bộ lịch sử.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
