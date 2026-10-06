"""Cổng đăng nhập và khung ứng dụng."""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, expect

from tests.e2e.conftest import USER
from tests.e2e.seed import FUJI, YKH


def test_chua_dang_nhap_thay_man_dang_nhap_va_api_tu_choi(anon_page: Page, server: str) -> None:
    expect(anon_page.locator("#login-gate")).to_be_visible()
    expect(anon_page.locator("#login-user")).to_be_visible()
    expect(anon_page.locator("#login-pass")).to_have_attribute("type", "password")
    for path in ("/api/terminals", "/api/forecast", "/api/plan/settings/" + YKH, "/api/settings"):
        assert httpx.get(server + path).status_code == 401, path


def test_da_dang_nhap_thay_khung_ung_dung_va_hai_bon(app_page: Page) -> None:
    expect(app_page.locator("#login-gate")).to_be_hidden()
    expect(app_page.get_by_text(USER)).to_be_visible()
    rows = app_page.locator("#tank-list tr[data-psn]")
    expect(rows).to_have_count(2)
    expect(app_page.locator(f'#tank-list tr[data-psn="{YKH}"]')).to_contain_text("Trực tuyến")
    expect(app_page.locator(f'#tank-list tr[data-psn="{FUJI}"]')).to_contain_text("Ngoại tuyến")


def test_moi_trang_trong_menu_mo_duoc(app_page: Page) -> None:
    for view in ("map", "planner", "reports", "settings", "analytics", "dashboard"):
        app_page.locator(f'[data-view="{view}"]').first.click()
        expect(app_page.locator(f"#view-{view}")).to_be_visible()
