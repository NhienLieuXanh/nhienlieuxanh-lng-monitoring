"""Khuyến nghị đặt hàng phải chỉ ra một chuyến xe CÓ THẬT.

Bug thật, 29/09/2026: khối "Khuyến nghị đặt hàng" ghi "giao lúc 04/10 21:17" —
Chủ Nhật, 9 giờ tối — trong khi bảng kế hoạch ngay bên dưới xếp nạp Thứ Bảy 03/10
lúc 08:00. suggest_order tính giờ giao là một mốc liên tục rồi trả thẳng ra, không
biết Chủ Nhật, ngày lễ, hay giờ nạp.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.domain.forecast import (
    ConsumptionEstimate,
    DeliveryCalendar,
    IdleTrend,
    suggest_order,
)

VN = ZoneInfo("Asia/Ho_Chi_Minh")
CAL = DeliveryCalendar(tz=VN, refill_time=time(8, 0))


def _cons(daily_l: float) -> ConsumptionEstimate:
    return ConsumptionEstimate(
        daily_use_l=daily_l, daily_use_sd_l=300.0, samples=500, window_days=30.0,
        active_days=14.0, coverage=0.98, drawdown_l=daily_l * 14, rise_l=0.0,
        refills=1, refill_l=35000.0, full_days=12, confidence="high",
    )


def _idle() -> IdleTrend:
    # Boil-off 0 để phép tính nhẩm được.
    return IdleTrend(
        boil_off_l_per_day=0.0, boil_off_percent_per_day=0.0,
        pressure_rise_mpa_per_day=None, idle_windows=0, idle_hours=0.0,
        method="reference",
    )


def _goi(volume_l: float, now: datetime, cal: DeliveryCalendar | None):
    return suggest_order(
        volume_l=volume_l, capacity_l=60000.0, consumption=_cons(5990.0),
        idle=_idle(), now=now, lead_time_days=1.0, reserve_l=10000.0, calendar=cal,
    )


# Đúng ca thật: 34,45 m³ lúc 16:00 ngày 29/09, dùng 5,99 m³/ngày.
NOW = datetime(2026, 9, 29, 16, 0, tzinfo=VN)


def test_khong_co_lich_thi_Y_HET_cu() -> None:
    """Không truyền lịch = hành vi cũ — và đó chính là ca Chủ Nhật 21:17 cũ."""
    s = _goi(34450.0, NOW, None)
    assert s.deliver_at is not None
    assert s.deliver_at.astimezone(VN).weekday() == 6


def test_ca_that_khong_con_giao_Chu_Nhat_toi() -> None:
    d = _goi(34450.0, NOW, CAL).deliver_at.astimezone(VN)
    assert d.weekday() != 6, f"vẫn giao Chủ Nhật: {d}"
    assert d.time() == time(8, 0), f"không đúng giờ nạp: {d}"


def test_doi_SOM_chu_khong_doi_muon() -> None:
    """Giao muộn hơn mốc gốc là để bồn tụt dưới điểm đặt hàng."""
    assert _goi(34450.0, NOW, CAL).deliver_at <= _goi(34450.0, NOW, None).deliver_at


def test_giao_som_hon_thi_luong_dat_tinh_lai_theo_luc_xe_toi() -> None:
    goc, moi = _goi(34450.0, NOW, None), _goi(34450.0, NOW, CAL)
    assert moi.order_l < goc.order_l
    ngay = (moi.deliver_at - NOW).total_seconds() / 86400.0
    assert moi.order_l == pytest.approx(54000.0 - (34450.0 - 5990.0 * ngay))


def test_ngay_le_chan_duoc_chuyen_do_va_doi_som() -> None:
    thuong = _goi(34450.0, NOW, CAL).deliver_at.astimezone(VN).date()
    cal = DeliveryCalendar(tz=VN, refill_time=time(8, 0), blocked=frozenset({thuong}))
    moi = _goi(34450.0, NOW, cal).deliver_at.astimezone(VN).date()
    assert moi < thuong


def test_neu_ly_do_doi_ngay_trong_co_so_tinh_toan() -> None:
    assert any("Dời giao hàng" in r for r in _goi(34450.0, NOW, CAL).reasons)


def test_khong_con_chuyen_nao_kip_thi_dat_ngay() -> None:
    # T7 20:00, 12 m³: chạm điểm đặt hàng (10) lúc CN 04:00, mốc giao liên tục T2
    # 04:00. Chuyến T7 08:00 đã qua, CN không giao -> chuyến sớm nhất T2 08:00, TRỄ
    # hơn mốc. Chờ thêm không được gì, chỉ có thể lỡ nốt chuyến này.
    now = datetime(2026, 10, 3, 20, 0, tzinfo=VN)
    s = _goi(12000.0, now, CAL)
    assert s.urgency == "now"
    assert s.order_at == now
    d = s.deliver_at.astimezone(VN)
    assert d.weekday() != 6 and d.time() == time(8, 0)


def test_moi_chuyen_giao_deu_roi_vao_ngay_giao_duoc() -> None:
    for h in range(0, 24 * 14, 5):
        now = datetime(2026, 9, 28, 0, 0, tzinfo=VN) + timedelta(hours=h)
        s = _goi(40000.0, now, CAL)
        if s.deliver_at is None:
            continue
        d = s.deliver_at.astimezone(VN)
        assert CAL.deliverable(d.date()) and d.time() == time(8, 0), (now, d)
        assert s.order_at <= s.deliver_at


@pytest.mark.db
def test_lich_dung_tu_DB_lay_gio_nap_va_ngay_nghi(session) -> None:
    """Trang gộp "Không giao" vào "Ngày nghỉ": một ngày nghỉ chặn giao hàng dù nó
    được lưu bằng cờ nào — ``no_delivery`` (bản hai cột cũ) hay chỉ ``rest``."""
    from app.db.models import Terminal
    from app.repositories import plan_readings as pr_repo

    session.add(Terminal(psn="CAL-01", name="CAL-01"))
    session.flush()
    pr_repo.save_settings(session, "CAL-01", {"refill_time": time(9, 30)}, by="t")
    pr_repo.set_flags(session, "CAL-01", date(2026, 11, 24), rest=False, forced=False,
                      no_delivery=True)
    pr_repo.set_flags(session, "CAL-01", date(2026, 11, 25), rest=True, forced=False)
    pr_repo.set_flags(session, "CAL-01", date(2026, 11, 26), rest=False, forced=True)
    cal = pr_repo.delivery_calendar(session, "CAL-01", tz=VN, today=NOW)
    assert cal.refill_time == time(9, 30)
    assert cal.blocked == frozenset({date(2026, 11, 24), date(2026, 11, 25)}), (
        "ngày nghỉ chặn giao; nạp chỉ định thì không"
    )
