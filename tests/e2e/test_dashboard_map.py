"""Bảng điều khiển và Bản đồ."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e.conftest import go
from tests.e2e.seed import FUJI, YKH


def _ticks(page: Page) -> list[str]:
    """Nhãn trục thời gian ĐANG HIỆN trên biểu đồ (qua chính callback của trục)."""
    return page.evaluate(
        """() => { const sc = chartInstance.scales.x;
                   return sc.ticks.map((t, i) => sc.options.ticks.callback.call(sc, t.value, i, sc.ticks)); }"""
    )


def test_chon_bon_thay_so_lieu_va_bieu_do(app_page: Page) -> None:
    app_page.locator(f'#tank-list tr[data-psn="{YKH}"]').click()
    detail = app_page.locator("#tank-detail")
    expect(detail).to_contain_text(YKH)
    expect(detail).to_contain_text("Trực tuyến")
    expect(detail).to_contain_text("m³")


def test_nhan_truc_bieu_do_7_va_30_ngay_luon_co_ngay(app_page: Page) -> None:
    """Lỗi production 30/09: ở 7/30 ngày trục ghi "05:29, 05:29, …" không có ngày."""
    app_page.locator(f'#tank-list tr[data-psn="{YKH}"]').click()
    for label in ("30 ngày", "7 ngày"):
        app_page.get_by_role("button", name=label, exact=True).click()
        app_page.wait_for_function("() => chartInstance && chartInstance.scales.x.ticks.length > 2")
        app_page.wait_for_timeout(300)
        ticks = _ticks(app_page)
        assert "/" in ticks[0], ticks
        assert len(set(ticks)) == len(ticks), f"{label}: nhãn trùng nhau {ticks}"


def test_bon_ngoai_tuyen_noi_ro_so_lieu_cu(app_page: Page) -> None:
    row = app_page.locator(f'#tank-list tr[data-psn="{FUJI}"]')
    expect(row).to_contain_text("Số liệu cũ")
    row.click()
    expect(app_page.locator("#tank-detail")).to_contain_text("Ngoại tuyến")


def test_ban_do_co_hai_bon_va_chu_quyen_bien_dao(app_page: Page) -> None:
    go(app_page, "map")
    view = app_page.locator("#view-map")
    expect(view).to_contain_text("2/2 bồn đã ghim vị trí")
    app_page.get_by_role("button", name="Việt Nam", exact=True).click()
    expect(view.get_by_text("Hoàng Sa", exact=True)).to_be_visible()
    expect(view.get_by_text("Trường Sa", exact=True)).to_be_visible()
    expect(view).to_contain_text("10.932519, 106.734816")


def test_danh_sach_bon_khong_cat_chu_khong_xuong_dong_tung_chu(app_page: Page) -> None:
    """Ảnh người dùng 06/10/2026: "Bồn LNG / - Fuji Seal" xuống dòng từng chữ, cột
    Trạng thái bị cắt "Ngoại tuy…", "Đo cuối: 69 ng…"."""
    for width in (1280, 1440):
        app_page.set_viewport_size({"width": width, "height": 900})
        app_page.wait_for_timeout(300)
        clipped = app_page.evaluate(
            """() => [...document.querySelectorAll('table.tanks th, #tank-list td')]
                 .filter(c => c.scrollWidth > c.clientWidth + 1)
                 .map(c => c.textContent.trim().slice(0, 30))"""
        )
        assert clipped == [], f"{width}px: ô bị cắt chữ {clipped}"
        over = app_page.evaluate(
            """() => { const t = document.querySelector('table.tanks');
                       return t.scrollWidth - t.parentElement.clientWidth; }"""
        )
        assert over <= 1, f"{width}px: bảng tràn ra ngoài khung danh sách {over}px (cột Trạng thái bị cắt)"
        heights = app_page.evaluate(
            "() => [...document.querySelectorAll('#tank-list .tname')].map(e => e.getBoundingClientRect().height)"
        )
        assert heights and max(heights) < 24, f"{width}px: tên bồn xuống dòng {heights}"


def test_o_so_lieu_chi_tiet_khong_xuong_dong_tung_chu(app_page: Page) -> None:
    """Hệ quả của việc nới danh sách: khung chi tiết hẹp, nhãn ô số liệu bị bẻ chữ."""
    for width in (1280, 1440, 1920):
        app_page.set_viewport_size({"width": width, "height": 900})
        app_page.locator(f'#tank-list tr[data-psn="{FUJI}"]').click()
        app_page.wait_for_timeout(300)
        tall = app_page.evaluate(
            """() => [...document.querySelectorAll('#tank-detail .readout dt')]
                 .filter(e => e.getBoundingClientRect().height > 22).map(e => e.textContent.trim())"""
        )
        assert tall == [], f"{width}px: nhãn bị xuống dòng {tall}"
