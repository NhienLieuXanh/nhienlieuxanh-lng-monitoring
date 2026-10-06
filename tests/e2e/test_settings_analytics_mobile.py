"""Cài đặt, Phân tích, và mọi trang trên màn hình điện thoại."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e.conftest import go
from tests.e2e.seed import FUJI, YKH


def test_cai_dat_hien_trang_thai_email(app_page: Page) -> None:
    go(app_page, "settings")
    view = app_page.locator("#view-settings")
    expect(view).to_contain_text("Người nhận cảnh báo")
    expect(view).to_contain_text("Máy chủ thư điện tử")


def test_phan_tich_co_the_cho_moi_bon(app_page: Page) -> None:
    go(app_page, "analytics")
    view = app_page.locator("#view-analytics")
    expect(view).not_to_contain_text("Đang phân tích", timeout=20_000)
    expect(view).to_contain_text(YKH)
    expect(view).to_contain_text(FUJI)
    expect(view).not_to_contain_text("1 / 0 lần đo")


def test_dien_thoai_khong_tran_ngang(app_page: Page) -> None:
    app_page.set_viewport_size({"width": 390, "height": 844})
    for view in ("dashboard", "map", "planner", "reports", "settings", "analytics"):
        app_page.locator(f'[data-view="{view}"]').first.dispatch_event("click")
        app_page.wait_for_timeout(400)
        over = app_page.evaluate(
            # Bỏ qua phần tử nằm trong một khung CUỘN NGANG có chủ đích (bảng kế
            # hoạch 9 cột): nằm ngoài khung nhìn ở đó là thiết kế, không phải tràn.
            """(v) => {
                 const inScroller = (e) => { for (let p = e.parentElement; p; p = p.parentElement) {
                     const ox = getComputedStyle(p).overflowX;
                     if ((ox === 'auto' || ox === 'scroll') && p.scrollWidth > p.clientWidth) return true; }
                   return false; };
                 return [...document.querySelectorAll(`#view-${v} label, #view-${v} select, #view-${v} input, #view-${v} button`)]
                   .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.right > innerWidth + 1 && !inScroller(e); })
                   .map(e => e.tagName + '#' + e.id); }""",
            view,
        )
        assert over == [], f"{view}: tràn khỏi màn hình {over}"
        assert app_page.evaluate("document.scrollingElement.scrollWidth") <= 391, view
