"""Cài đặt, Phân tích, và mọi trang trên màn hình điện thoại."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e.conftest import go
from tests.e2e.seed import FUJI, YKH


def test_cai_dat_mot_the_email_ba_buoc_va_muc_nang_cao_gap_san(app_page: Page) -> None:
    """Trang Cài đặt làm lại 06/10/2026: người dùng thấy rối và không biết điền gì."""
    go(app_page, "settings")
    view = app_page.locator("#view-settings")
    expect(view).to_contain_text("Hệ thống chỉ gửi email trong hai trường hợp")
    expect(view).to_contain_text("Bồn sắp chạm mức dự trữ")
    expect(view).to_contain_text("Nhà máy phát báo động")
    for step in ("Ai nhận cảnh báo?", "Gửi từ hộp thư nào?", "Lưu và gửi thư thử"):
        expect(view.get_by_role("heading", name=step)).to_be_visible()
    # Chưa cấu hình gì -> gợi ý Brevo, kèm hướng dẫn của Brevo.
    expect(app_page.locator("#s-provider")).to_have_value("brevo")
    expect(app_page.locator("#s-guide-brevo")).to_be_visible()
    # Nhãn TUỲ CHỈNH / MẶC ĐỊNH đã bỏ: nhiễu.
    expect(view).not_to_contain_text("MẶC ĐỊNH")
    expect(view.locator(".src")).to_have_count(0)
    # Nâng cao gấp sẵn; mở ra thì mỗi ô có một câu giải thích.
    more = app_page.locator("#set-more")
    expect(more).not_to_have_attribute("open", "")
    expect(app_page.locator("#s-truck")).to_be_hidden()
    more.locator("summary").click()
    expect(app_page.locator("#s-truck")).to_be_visible()
    expect(more).to_contain_text("20 tấn ≈ 44 m³")


def test_luu_email_va_trang_thai_noi_ro_vi_sao_chua_gui_duoc(app_page: Page) -> None:
    go(app_page, "settings")
    pill = app_page.locator("#set-mail-pill")
    expect(pill).to_have_text("Đang tắt")                 # server e2e chạy NOTIFY_ENABLED=false
    app_page.locator("#s-emails").fill("van-hanh@example.com")
    app_page.locator("#s-notify").select_option("true")
    app_page.locator("#s-provider").select_option("brevo")
    app_page.locator("#s-user").fill("e2e-login@smtp-brevo.com")
    with app_page.expect_response(lambda r: r.url.endswith("/api/settings") and r.request.method == "PATCH"):
        app_page.locator("#s-save").click()
    expect(app_page.locator("#s-msg")).to_have_text("Đã lưu.")
    # Brevo bắt đăng nhập, chưa có mật khẩu -> nói thẳng là chưa gửi được và vì sao.
    expect(pill).to_have_text("✕ Chưa gửi được")
    expect(app_page.locator("#set-smtp-state")).to_contain_text("bắt buộc đăng nhập")

    app_page.locator("#s-save-test").click()
    expect(app_page.locator("#s-msg")).to_contain_text("bắt buộc đăng nhập")

    app_page.reload()
    go(app_page, "settings")
    expect(app_page.locator("#s-emails")).to_have_value("van-hanh@example.com")
    expect(app_page.locator("#s-provider")).to_have_value("brevo")
    # Trả lại trạng thái tắt cho các test khác.
    app_page.locator("#s-notify").select_option("false")
    with app_page.expect_response(lambda r: r.url.endswith("/api/settings") and r.request.method == "PATCH"):
        app_page.locator("#s-save").click()
    expect(pill).to_have_text("Đang tắt")


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
