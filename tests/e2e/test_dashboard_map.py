"""Bảng điều khiển và Bản đồ."""

from __future__ import annotations

import re

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
    app_page.locator(f'#tank-list [data-psn="{YKH}"]').click()
    detail = app_page.locator("#tank-detail")
    expect(detail).to_contain_text(YKH)
    expect(detail).to_contain_text("Trực tuyến")
    expect(detail).to_contain_text("m³")


def test_so_thap_phan_viet_theo_kieu_viet_nam(app_page: Page) -> None:
    """Soát UI 26/10/2026: thể tích in toFixed(3) thành "52.340 m³" — người Việt đọc
    là năm mươi hai NGHÌN. Mọi số thập phân dùng dấu phẩy, dấu chấm chỉ để nhóm
    hàng nghìn."""
    expect(app_page.locator("#fleet-meta")).to_contain_text(re.compile(r"Tổng \d{1,3}(\.\d{3})*,\d m³"))
    pcts = app_page.locator("#tank-list .tk-pct").all_inner_texts()
    assert pcts and all(re.fullmatch(r"\d{1,3}(,\d)?%", p) for p in pcts), pcts
    app_page.locator(f'#tank-list [data-psn="{YKH}"]').click()
    detail = app_page.locator("#tank-detail").inner_text()
    assert not re.search(r"\d\.\d{3}(?!\d)", detail), detail


def test_khung_chi_tiet_mo_dau_bang_cau_ket_luan(app_page: Page) -> None:
    """Soát UI 26/10/2026: khung chi tiết mở bằng bốn ô số đo to bằng nhau, còn con
    số người vận hành hành động theo (bao lâu nữa tới mức dự trữ) không có ở đó."""
    app_page.locator(f'#tank-list [data-psn="{YKH}"]').click()
    lead = app_page.locator("#td-lead")
    expect(lead).to_contain_text("tới mức dự trữ")
    expect(lead.locator(".big")).to_have_text(re.compile(r"^\d+,\d\s*(ngày|giờ)$"))
    # Cùng con số với cột "Còn lại" của danh sách.
    pill = app_page.locator(f'#tank-list [data-psn="{YKH}"] .runpill').inner_text()
    assert lead.locator(".big").inner_text().replace("\n", "").replace(" ", "") == pill.replace(" ", "")
    app_page.locator(f'#tank-list [data-psn="{FUJI}"]').click()
    expect(app_page.locator("#td-lead")).to_have_attribute("data-sev", "crit")
    expect(app_page.locator("#td-lead")).to_contain_text("không có số đo mới")


def test_nhan_truc_bieu_do_7_va_30_ngay_luon_co_ngay(app_page: Page) -> None:
    """Lỗi production 30/09: ở 7/30 ngày trục ghi "05:29, 05:29, …" không có ngày."""
    app_page.locator(f'#tank-list [data-psn="{YKH}"]').click()
    for label in ("30 ngày", "7 ngày"):
        app_page.get_by_role("button", name=label, exact=True).click()
        app_page.wait_for_function("() => chartInstance && chartInstance.scales.x.ticks.length > 2")
        app_page.wait_for_timeout(300)
        ticks = _ticks(app_page)
        assert "/" in ticks[0], ticks
        assert len(set(ticks)) == len(ticks), f"{label}: nhãn trùng nhau {ticks}"


def test_bon_ngoai_tuyen_noi_ro_so_lieu_cu(app_page: Page) -> None:
    row = app_page.locator(f'#tank-list [data-psn="{FUJI}"]')
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


def test_o_bon_khong_cat_chu_khong_xuong_dong_tung_chu(app_page: Page) -> None:
    """Ảnh người dùng 06/10/2026: "Bồn LNG / - Fuji Seal" xuống dòng từng chữ, cột
    Trạng thái bị cắt "Ngoại tuy…". Ô bồn (thiết kế phòng điều khiển 26/10/2026)
    phải giữ được điều đó: không phần tử nào tràn, tên tối đa hai dòng."""
    for width in (1280, 1440):
        app_page.set_viewport_size({"width": width, "height": 900})
        app_page.wait_for_timeout(300)
        clipped = app_page.evaluate(
            """() => [...document.querySelectorAll('#tank-list .tk, #tank-list .tk *')]
                 .filter(c => c.scrollWidth > c.clientWidth + 1 && !c.classList.contains('tk-gauge'))
                 .map(c => c.className + ': ' + c.textContent.trim().slice(0, 30))"""
        )
        assert clipped == [], f"{width}px: ô bồn bị cắt chữ {clipped}"
        over = app_page.evaluate(
            """() => { const g = document.querySelector('#tank-list');
                       return g.scrollWidth - g.clientWidth; }"""
        )
        assert over <= 1, f"{width}px: lưới bồn tràn ngang {over}px"
        heights = app_page.evaluate(
            "() => [...document.querySelectorAll('#tank-list .tname')].map(e => e.getBoundingClientRect().height)"
        )
        assert heights and max(heights) < 40, f"{width}px: tên bồn quá hai dòng {heights}"


def _rgb(css: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", css)[:3])


def test_mau_chi_xuat_hien_khi_bat_thuong(app_page: Page) -> None:
    """Thiết kế phòng điều khiển (ISA-101): bình thường là XÁM, chỉ bồn có việc phải
    làm mới có màu, và luôn kèm chữ. Thanh trên im khi máy ổn."""
    expect(app_page.locator(f'#tank-list [data-psn="{YKH}"]')).to_have_attribute("data-sev", "ok")
    expect(app_page.locator(f'#tank-list [data-psn="{FUJI}"]')).to_have_attribute("data-sev", "crit")
    liq = app_page.evaluate(
        """(ps) => ps.map(p => getComputedStyle(
             document.querySelector(`#tank-list [data-psn="${p}"] .tk-liq`)).backgroundColor)""",
        [YKH, FUJI],
    )
    r, g, b = _rgb(liq[0])
    assert max(r, g, b) - min(r, g, b) <= 24, f"bồn bình thường không được tô màu: {liq[0]}"
    r, g, b = _rgb(liq[1])
    assert r > g + 80 and r > b + 80, f"bồn mất liên lạc phải đỏ: {liq[1]}"
    expect(app_page.locator(f'#tank-list [data-psn="{FUJI}"]')).to_contain_text("Ngoại tuyến")
    expect(app_page.locator("#health-pill")).to_be_hidden()
    # inner_text: chữ NHÌN THẤY (to_contain_text đọc cả chữ của phần tử đang ẩn).
    assert "Hệ thống bình thường" not in app_page.locator("header.topbar").inner_text()


def test_o_so_lieu_chi_tiet_khong_xuong_dong_tung_chu(app_page: Page) -> None:
    """Hệ quả của việc nới danh sách: khung chi tiết hẹp, nhãn ô số liệu bị bẻ chữ."""
    for width in (1280, 1440, 1920):
        app_page.set_viewport_size({"width": width, "height": 900})
        app_page.locator(f'#tank-list [data-psn="{FUJI}"]').click()
        app_page.wait_for_timeout(300)
        tall = app_page.evaluate(
            """() => [...document.querySelectorAll('#tank-detail .readout dt')]
                 .filter(e => e.getBoundingClientRect().height > 22).map(e => e.textContent.trim())"""
        )
        assert tall == [], f"{width}px: nhãn bị xuống dòng {tall}"
