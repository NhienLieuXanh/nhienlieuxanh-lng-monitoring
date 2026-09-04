"""Test lớp dự báo. Hàm thuần nên không cần DB, không cần mock clock.

Mọi con số ở đây được chọn để **tính nhẩm ra được**: nếu một assert fail thì biết
ngay là công thức sai chứ không phải fixture sai.

Bối cảnh dữ liệu thật (để các hằng số dưới đây không phải số ngẫu nhiên):
dung tích 10 425 L, cadence vendor ~30 phút, mức dùng Excel của người vận hành
7.4 m³/ngày = 7400 L/ngày.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.domain.forecast import (
    DEFAULT_MAX_FILL_PERCENT,
    Z_BY_SERVICE_LEVEL,
    Forecast,
    Sample,
    build_forecast,
    detect_refills,
    estimate_consumption,
    estimate_idle_trend,
    hold_time,
    noise_floor_l,
    plan_trips,
    refill_floor_l,
    runout,
    suggest_order,
)

UTC = ZoneInfo("UTC")
VN = ZoneInfo("Asia/Ho_Chi_Minh")

CAP = 10425.0
T0 = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
STEP = timedelta(minutes=30)


def _series(
    *,
    start_l: float,
    per_day_l: float,
    hours: float,
    step: timedelta = STEP,
    pressure0: float | None = None,
    pressure_per_day: float = 0.0,
    t0: datetime = T0,
) -> list[Sample]:
    """Chuỗi tuyến tính: mức giảm ``per_day_l`` L/ngày, áp tăng đều nếu được cấp."""
    n = round(hours * 3600 / step.total_seconds())
    out: list[Sample] = []
    for i in range(n + 1):
        d = i * step.total_seconds() / 86400.0
        out.append(
            Sample(
                at=t0 + i * step,
                volume_l=start_l - per_day_l * d,
                pressure_mpa=(
                    None if pressure0 is None else pressure0 + pressure_per_day * d
                ),
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Ngưỡng
# --------------------------------------------------------------------------- #


def test_thresholds_scale_with_capacity() -> None:
    # 0.1% và 2% dung tích, có sàn tuyệt đối cho bồn rất nhỏ.
    assert noise_floor_l(CAP) == 10.425
    assert refill_floor_l(CAP) == 208.5
    assert noise_floor_l(None) == 5.0
    assert refill_floor_l(0) == 200.0
    assert noise_floor_l(100.0) == 3.0  # sàn 3 L thắng 0.1 L


# --------------------------------------------------------------------------- #
# Tiêu thụ
# --------------------------------------------------------------------------- #


def test_consumption_constant_drawdown() -> None:
    """24 giờ dữ liệu liên tục, giảm đúng 7400 L/ngày -> đo lại đúng 7400."""
    s = _series(start_l=9000.0, per_day_l=7400.0, hours=24)
    est = estimate_consumption(s, capacity_l=CAP, tz=VN)
    assert est.samples == 49
    assert est.active_days == 1.0
    assert est.coverage == 1.0
    assert est.daily_use_l is not None
    assert abs(est.daily_use_l - 7400.0) < 1e-6
    assert est.refills == 0


def test_consumption_ignores_refill_jump() -> None:
    """Một lần nạp giữa cửa sổ không được tính thành 'tiêu thụ âm'.

    Đây chính là lý do không hồi quy trên cả cửa sổ: hệ số góc hồi quy của chuỗi
    này là DƯƠNG (mức cuối cao hơn mức đầu) nên hồi quy sẽ ra 'mức dùng âm'.
    """
    a = _series(start_l=3000.0, per_day_l=7400.0, hours=6)  # 3000 -> 1150
    last = a[-1]
    assert last.volume_l is not None
    b = _series(start_l=9000.0, per_day_l=7400.0, hours=6, t0=last.at + STEP)
    est = estimate_consumption(a + b, capacity_l=CAP, tz=VN)

    assert est.refills == 1
    assert est.refill_l > 7000  # ~9000 - 1150 trừ một bước rút
    assert est.daily_use_l is not None
    # Hai nhánh đều rút 7400 L/ngày; bước nạp bị loại nên kết quả vẫn là 7400.
    assert abs(est.daily_use_l - 7400.0) < 5.0

    refills = detect_refills(a + b, capacity_l=CAP)
    assert len(refills) == 1
    assert refills[0].after_l == 9000.0
    assert refills[0].amount_l > 7000


def test_refill_steps_merge_into_one_event() -> None:
    """Một chuyến xe bồn = MỘT bản ghi, dù nó sinh nhiều lần đọc tăng liên tiếp.

    Lỗi thật trên production: nhật ký nạp hiện hai bản ghi cách nhau 64 giây
    (0.009 -> 2.758 -> 3.258 m³) cho cùng một lần nạp, nên đếm sai số chuyến và chia
    vụn lượng nạp. Ở đây mô phỏng đúng hình dạng đó: ba lần đọc tăng dần, cách nhau
    một phút.
    """
    base = _series(start_l=3000.0, per_day_l=7400.0, hours=6)
    t = base[-1].at
    minute = timedelta(minutes=1)
    fill = [
        Sample(at=t + minute, volume_l=5000.0, pressure_mpa=0.1),
        Sample(at=t + 2 * minute, volume_l=8000.0, pressure_mpa=0.1),
        Sample(at=t + 3 * minute, volume_l=9000.0, pressure_mpa=0.1),
    ]
    after = _series(start_l=9000.0, per_day_l=7400.0, hours=6, t0=t + 4 * minute)

    events = detect_refills(base + fill + after, capacity_l=CAP)
    assert len(events) == 1, f"phải gộp thành một đợt, đang có {len(events)}"
    ev = events[0]
    # Trước = mức ngay trước bước tăng đầu; sau = mức ở đỉnh đợt.
    assert ev.before_l == base[-1].volume_l
    assert ev.after_l == 9000.0
    assert abs(ev.amount_l - (9000.0 - (base[-1].volume_l or 0.0))) < 1e-6
    assert ev.at == t + 3 * minute


def test_two_deliveries_far_apart_stay_separate() -> None:
    """Ngưỡng gộp không được rộng tới mức nuốt hai chuyến khác nhau trong ngày."""
    a = _series(start_l=3000.0, per_day_l=7400.0, hours=2)
    t1 = a[-1].at
    first = [Sample(at=t1 + timedelta(minutes=1), volume_l=6000.0, pressure_mpa=0.1)]
    # Rút tiếp 5 giờ — xa hơn REFILL_MERGE_HOURS — rồi nạp lần hai.
    mid = _series(
        start_l=6000.0, per_day_l=7400.0, hours=5, t0=t1 + timedelta(minutes=31)
    )
    t2 = mid[-1].at
    second = [Sample(at=t2 + timedelta(minutes=1), volume_l=9000.0, pressure_mpa=0.1)]

    events = detect_refills(a + first + mid + second, capacity_l=CAP)
    assert len(events) == 2, f"phải là hai đợt riêng, đang có {len(events)}"
    assert events[0].after_l == 6000.0
    assert events[1].after_l == 9000.0


def test_consumption_excludes_offline_gap() -> None:
    """Một tuần offline KHÔNG được kéo mức dùng/ngày xuống gần 0.

    Chia theo bề rộng cửa sổ (8 ngày) sẽ ra ~925 L/ngày — sai 8 lần. Chia theo
    active_days (1 ngày) ra đúng 7400.
    """
    a = _series(start_l=9000.0, per_day_l=7400.0, hours=12)
    b = _series(start_l=1000.0, per_day_l=7400.0, hours=12, t0=T0 + timedelta(days=7))
    est = estimate_consumption(a + b, capacity_l=CAP, tz=VN)

    assert est.window_days > 7.0
    assert abs(est.active_days - 1.0) < 1e-6  # 12h + 12h
    assert est.coverage < 0.2
    assert est.daily_use_l is not None
    assert abs(est.daily_use_l - 7400.0) < 1e-6
    # Khoảng trống không bị đếm thành lần nạp, cũng không thành cú rút khổng lồ.
    assert est.refills == 0


def test_minute_cadence_daily_use_is_not_zero() -> None:
    """Nhịp 1 phút, 5 m³/ngày trên bồn 60 000 L — phải đo được, không bị deadband nuốt."""
    cap = 60_000.0
    s = _series(
        start_l=50_000.0,
        per_day_l=5_000.0,
        hours=48,
        step=timedelta(minutes=1),
    )
    est = estimate_consumption(s, capacity_l=cap, tz=VN)
    assert est.daily_use_l is not None
    assert abs(est.daily_use_l - 5_000.0) / 5_000.0 < 0.10


def test_consumption_deadband_rejects_sensor_noise() -> None:
    """Dao động +/-5 L quanh 5000 (dưới deadband 10.425) -> không có tiêu thụ."""
    s = [
        Sample(at=T0 + i * STEP, volume_l=5000.0 + (5.0 if i % 2 else -5.0))
        for i in range(49)
    ]
    est = estimate_consumption(s, capacity_l=CAP, tz=VN)
    assert est.drawdown_l == 0.0
    assert est.daily_use_l is None
    assert est.confidence == "none"


def test_consumption_confidence_and_sigma() -> None:
    """>= 7 ngày liên tục -> độ tin cậy cao và sigma đo được (không phải giả định)."""
    s = _series(start_l=80_000.0, per_day_l=7400.0, hours=24 * 8)
    est = estimate_consumption(s, capacity_l=CAP, tz=VN)
    assert est.confidence == "high"
    assert est.full_days >= 5
    assert est.daily_use_sd_l is not None
    # Tiêu thụ hoàn toàn đều -> biến động theo ngày xấp xỉ 0.
    assert est.daily_use_sd_l < 1.0


def test_consumption_empty_and_single() -> None:
    assert estimate_consumption([], capacity_l=CAP).daily_use_l is None
    one = [Sample(at=T0, volume_l=100.0)]
    assert estimate_consumption(one, capacity_l=CAP).daily_use_l is None
    # Chỉ có volume None -> vẫn không nổ.
    nones = [Sample(at=T0 + i * STEP) for i in range(10)]
    assert estimate_consumption(nones, capacity_l=CAP).samples == 0


# --------------------------------------------------------------------------- #
# Boil-off & hold time
# --------------------------------------------------------------------------- #


def test_idle_trend_measures_boil_off_and_pressure() -> None:
    """Bồn nghỉ 24 giờ: hao 5 L/ngày, áp tăng 0.02 MPa/ngày.

    5 L/ngày = 0.104 L mỗi 30 phút — nằm SÂU dưới deadband 10.425 L, nên đây đúng
    là ca mà hiệu số từng cặp không thấy gì và chỉ hồi quy mới đo được.
    """
    s = _series(
        start_l=5000.0,
        per_day_l=5.0,
        hours=24,
        pressure0=0.05,
        pressure_per_day=0.02,
    )
    idle = estimate_idle_trend(s, capacity_l=CAP)

    assert idle.method == "measured"
    assert idle.idle_windows == 1
    assert abs(idle.idle_hours - 24.0) < 1e-6
    assert idle.boil_off_l_per_day is not None
    assert abs(idle.boil_off_l_per_day - 5.0) < 1e-6
    assert idle.boil_off_percent_per_day is not None
    assert abs(idle.boil_off_percent_per_day - 5.0 / CAP * 100) < 1e-9
    assert idle.pressure_rise_mpa_per_day is not None
    assert abs(idle.pressure_rise_mpa_per_day - 0.02) < 1e-9


def test_idle_trend_falls_back_to_reference_when_busy() -> None:
    """Rút liên tục -> không có cửa sổ nghỉ -> boil-off tham chiếu, nhãn rõ ràng."""
    s = _series(start_l=9000.0, per_day_l=7400.0, hours=24)
    idle = estimate_idle_trend(s, capacity_l=CAP)
    assert idle.idle_windows == 0
    assert idle.method == "reference"
    assert idle.boil_off_l_per_day is not None
    assert abs(idle.boil_off_l_per_day - CAP * 0.0005) < 1e-9
    assert idle.pressure_rise_mpa_per_day is None


def test_idle_window_needs_six_hours() -> None:
    """Nghỉ 3 giờ chưa đủ để hồi quy — quá ngắn thì nhiễu áp đảo hệ số góc."""
    s = _series(start_l=5000.0, per_day_l=5.0, hours=3)
    assert estimate_idle_trend(s, capacity_l=CAP).idle_windows == 0


def test_hold_time_math() -> None:
    h = hold_time(current_mpa=0.07, rise_mpa_per_day=0.02, relief_mpa=0.8)
    assert h.method == "measured"
    assert h.headroom_mpa is not None
    assert abs(h.headroom_mpa - 0.73) < 1e-9
    assert h.days is not None
    assert abs(h.days - 36.5) < 1e-9


def test_hold_time_undefined_not_infinite() -> None:
    """Áp không tăng -> None ('chưa đủ dữ liệu'), TUYỆT ĐỐI không phải vô cực."""
    assert hold_time(current_mpa=0.07, rise_mpa_per_day=None).days is None
    assert hold_time(current_mpa=0.07, rise_mpa_per_day=0.0).days is None
    assert hold_time(current_mpa=None, rise_mpa_per_day=0.02).days is None
    # Đã vượt van an toàn -> 0 ngày, phải xả ngay.
    over = hold_time(current_mpa=0.9, rise_mpa_per_day=0.02, relief_mpa=0.8)
    assert over.days == 0.0


# --------------------------------------------------------------------------- #
# Ngày tới cạn
# --------------------------------------------------------------------------- #


def test_runout_includes_boil_off() -> None:
    """Thất thoát = rút + bay hơi. Bỏ bay hơi ra là dự báo lạc quan hơn thực tế."""
    r = runout(
        volume_l=8000.0,
        reserve_l=1000.0,
        daily_use_l=1000.0,
        boil_off_l_per_day=0.0,
        now=T0,
    )
    assert r.days_to_reserve == 7.0
    assert r.days_to_empty == 8.0
    assert r.reserve_at == T0 + timedelta(days=7)

    r2 = runout(
        volume_l=8000.0,
        reserve_l=1000.0,
        daily_use_l=1000.0,
        boil_off_l_per_day=1000.0,
        now=T0,
    )
    assert r2.daily_loss_l == 2000.0
    assert r2.days_to_reserve == 3.5  # bay hơi làm cạn nhanh gấp đôi


def test_runout_below_reserve_clamps_to_zero() -> None:
    r = runout(
        volume_l=500.0,
        reserve_l=1000.0,
        daily_use_l=1000.0,
        boil_off_l_per_day=0.0,
        now=T0,
    )
    assert r.days_to_reserve == 0.0
    assert r.days_to_empty == 0.5


def test_runout_unknown_when_no_usage() -> None:
    r = runout(
        volume_l=8000.0,
        reserve_l=1000.0,
        daily_use_l=None,
        boil_off_l_per_day=None,
        now=T0,
    )
    assert r.days_to_empty is None and r.empty_at is None


# --------------------------------------------------------------------------- #
# Đề xuất đặt hàng
# --------------------------------------------------------------------------- #


def test_suggestion_reorder_point_formula() -> None:
    """Kiểm từng số hạng của ROP = thất thoát x lead time + z x sigma x sqrt(LT)."""
    s = _series(start_l=9000.0, per_day_l=1000.0, hours=24 * 8)
    cons = estimate_consumption(s, capacity_l=CAP, tz=VN)
    idle = estimate_idle_trend(s, capacity_l=CAP)  # busy -> reference BOR
    assert cons.daily_use_l is not None

    sug = suggest_order(
        volume_l=9000.0,
        capacity_l=CAP,
        consumption=cons,
        idle=idle,
        now=T0,
        lead_time_days=2.0,
        service_level=95,
        reserve_l=0.0,  # để mô hình quyết định, không bị sàn người vận hành đè
    )
    bor = CAP * 0.0005
    loss = cons.daily_use_l + bor
    sd = cons.daily_use_sd_l
    assert sd is not None
    expect_safety = Z_BY_SERVICE_LEVEL[95] * sd * (2.0**0.5)
    assert abs(sug.safety_stock_l - expect_safety) < 1e-6
    assert abs(sug.reorder_point_l - (loss * 2.0 + expect_safety)) < 1e-6

    # Mức đích chừa ullage, không nạp tới 100%.
    assert abs(sug.target_l - CAP * DEFAULT_MAX_FILL_PERCENT / 100.0) < 1e-9
    assert sug.target_l < CAP
    # Lượng đặt tính theo mức LÚC GIAO, nên phải > (đích - mức hiện tại).
    assert sug.order_l is not None
    assert sug.order_l > sug.target_l - 9000.0
    assert sug.reasons  # luôn giải thích được


def test_suggestion_operator_reserve_is_a_floor() -> None:
    """Dự trữ người vận hành đặt cao hơn thì thắng — mô hình không hạ chính sách."""
    s = _series(start_l=9000.0, per_day_l=1000.0, hours=24 * 8)
    cons = estimate_consumption(s, capacity_l=CAP, tz=VN)
    idle = estimate_idle_trend(s, capacity_l=CAP)
    sug = suggest_order(
        volume_l=9000.0,
        capacity_l=CAP,
        consumption=cons,
        idle=idle,
        now=T0,
        lead_time_days=1.0,
        reserve_l=6000.0,
    )
    assert sug.reorder_point_l == 6000.0
    assert any("người vận hành" in r for r in sug.reasons)


def test_suggestion_urgency_now_when_below_reorder_point() -> None:
    s = _series(start_l=9000.0, per_day_l=1000.0, hours=24 * 8)
    cons = estimate_consumption(s, capacity_l=CAP, tz=VN)
    idle = estimate_idle_trend(s, capacity_l=CAP)
    sug = suggest_order(
        volume_l=500.0,
        capacity_l=CAP,
        consumption=cons,
        idle=idle,
        now=T0,
        lead_time_days=1.0,
        reserve_l=2000.0,
    )
    assert sug.urgency == "now"
    assert sug.order_at == T0  # đặt ngay, không lùi về quá khứ


def test_suggestion_honest_when_no_data() -> None:
    """Không đo được mức dùng -> KHÔNG bịa ra một con số."""
    cons = estimate_consumption([], capacity_l=CAP)
    idle = estimate_idle_trend([], capacity_l=CAP)
    sug = suggest_order(
        volume_l=60.0, capacity_l=CAP, consumption=cons, idle=idle, now=T0
    )
    assert sug.order_l is None
    assert sug.urgency == "unknown"
    assert any("Chưa đo được" in r for r in sug.reasons)

    # Không biết dung tích -> cũng không bịa.
    sug2 = suggest_order(
        volume_l=60.0, capacity_l=None, consumption=cons, idle=idle, now=T0
    )
    assert sug2.order_l is None and sug2.urgency == "unknown"


# --------------------------------------------------------------------------- #
# Gộp + điều phối chuyến
# --------------------------------------------------------------------------- #


def test_build_forecast_end_to_end() -> None:
    s = _series(
        start_l=9000.0,
        per_day_l=1000.0,
        hours=24 * 8,
        pressure0=0.07,
        pressure_per_day=0.0,
    )
    f = build_forecast(
        s,
        psn="TEST0001",
        volume_l=9000.0,
        capacity_l=CAP,
        pressure_mpa=0.07,
        now=T0,
        tz=VN,
        reserve_percent=15.0,
        lead_time_days=1.0,
    )
    assert f.psn == "TEST0001"
    assert f.fill_percent is not None
    assert abs(f.fill_percent - 9000.0 / CAP * 100) < 1e-9
    assert abs(f.reserve_l - CAP * 0.15) < 1e-9
    assert f.consumption.confidence == "high"
    assert f.runout.days_to_empty is not None
    assert f.suggestion.order_l is not None
    # Áp phẳng trong lúc rút liên tục -> hold time không xác định, không phải vô cực.
    assert f.hold.days is None


def test_build_forecast_survives_dead_device() -> None:
    """Ca THẬT hôm nay: bồn gần cạn, offline hàng tháng, chỉ một điểm dữ liệu."""
    f = build_forecast(
        [Sample(at=T0, volume_l=61.0, pressure_mpa=0.071)],
        psn="2604200016",
        volume_l=61.0,
        capacity_l=CAP,
        pressure_mpa=0.071,
        now=T0 + timedelta(days=30),
        tz=VN,
    )
    assert f.consumption.daily_use_l is None
    assert f.consumption.confidence == "none"
    # Vẫn cạn dần vì bay hơi tham chiếu, dù không đo được lượng rút.
    assert f.runout.days_to_empty is not None
    # Lần đọc cách đây 30 ngày -> stale -> KHÔNG phát cảnh báo hướng tới tương lai.
    assert f.stale is True
    assert f.alerts == []
    # Nhưng vẫn nói được BAO NHIÊU (mức đã dưới dự trữ), chỉ không nói được KHI NÀO.
    assert f.suggestion.urgency == "now"
    assert f.suggestion.order_l is not None
    assert any("không dự báo được thời điểm" in r for r in f.suggestion.reasons)


def test_stale_reading_suppresses_forward_alerts() -> None:
    """Số liệu cũ -> KHÔNG phát RUNOUT/HOLD_TIME.

    Ca thật phát hiện khi test e2e: hai bồn offline hàng tháng, mức đo cuối 61 L.
    Chiếu "còn 0 ngày tới mức dự trữ" từ con số đó rồi gửi email là sai — bồn có
    thể đã được nạp tay từ lâu. Cảnh báo đúng ở đây là OFFLINE, và nó do
    ``domain/alerts.py`` phát, không phải module này.
    """
    s = _series(start_l=9000.0, per_day_l=1000.0, hours=24 * 8)
    old = build_forecast(
        s,
        psn="2604200016",
        volume_l=61.0,
        capacity_l=CAP,
        pressure_mpa=0.071,
        now=T0 + timedelta(days=40),
        tz=VN,
        reading_at=T0 + timedelta(days=8),  # đọc cách đây 32 ngày
    )
    assert old.stale is True
    assert old.reading_age_days is not None and old.reading_age_days > 31
    assert [a.code for a in old.alerts] == []
    # Con số vẫn được tính và vẫn phát ra — UI hiện kèm nhãn "dữ liệu cũ" thay vì
    # ẩn đi, để người xem biết vì sao không có dự báo dùng được.
    assert old.runout.days_to_reserve is not None

    fresh = build_forecast(
        s,
        psn="2604200016",
        volume_l=61.0,
        capacity_l=CAP,
        pressure_mpa=0.071,
        now=T0 + timedelta(days=8),
        tz=VN,
        reading_at=T0 + timedelta(days=8),  # vừa đọc xong
    )
    assert fresh.stale is False
    assert "RUNOUT" in [a.code for a in fresh.alerts]


def test_no_reading_at_is_treated_as_stale() -> None:
    """Không biết lần đọc lúc nào -> im lặng, không cảnh báo. Mặc định an toàn."""
    f = build_forecast(
        _series(start_l=9000.0, per_day_l=1000.0, hours=24 * 8),
        psn="X", volume_l=61.0, capacity_l=CAP, pressure_mpa=0.07,
        now=T0 + timedelta(days=8), tz=VN,
    )
    assert f.stale is True and f.reading_age_days is None
    assert f.alerts == []


def _stub(psn: str, days: float, order: float) -> Forecast:
    """Forecast thật, rồi thay hai con số để test riêng phần chia chuyến."""
    f = build_forecast(
        _series(start_l=9000.0, per_day_l=1000.0, hours=24 * 8),
        psn=psn,
        volume_l=9000.0,
        capacity_l=CAP,
        pressure_mpa=0.07,
        now=T0,
        tz=VN,
        reading_at=T0,  # dữ liệu tươi, nếu không sẽ bị loại vì stale
    )
    return replace(
        f,
        runout=replace(f.runout, days_to_reserve=days),
        suggestion=replace(f.suggestion, order_l=order),
    )


def test_plan_trips_splits_by_truck_capacity() -> None:
    trips = plan_trips(
        [_stub("A", 5.0, 6000.0), _stub("B", 1.0, 5000.0), _stub("C", 20.0, 5000.0)],
        truck_capacity_l=10_000.0,
        horizon_days=7.0,
        names={"A": "Bồn A", "B": "Bồn B"},
    )
    # C ngoài tầm 7 ngày -> bị loại. B gấp hơn A -> đi trước.
    stops = [s.psn for t in trips for s in t.stops]
    assert stops == ["B", "A"]
    # 5000 + 6000 > 10000 -> phải tách thành 2 chuyến.
    assert len(trips) == 2
    assert trips[0].total_l == 5000.0
    assert trips[1].total_l == 6000.0
    assert all(t.total_l <= t.truck_capacity_l for t in trips)
    assert trips[0].stops[0].name == "Bồn B"


def test_plan_trips_excludes_stale_tanks() -> None:
    """Không điều xe theo mức đo đã chết — bồn đó cần người kiểm tra thiết bị."""
    fresh = _stub("FRESH", 1.0, 5000.0)
    stale = replace(_stub("STALE", 1.0, 5000.0), stale=True)
    trips = plan_trips([fresh, stale], truck_capacity_l=20_000.0, horizon_days=7.0)
    assert [s.psn for t in trips for s in t.stops] == ["FRESH"]


def test_plan_trips_empty_inputs() -> None:
    assert plan_trips([], truck_capacity_l=10_000.0) == []
    f = build_forecast(
        [], psn="X", volume_l=None, capacity_l=None, pressure_mpa=None, now=T0
    )
    assert plan_trips([f], truck_capacity_l=10_000.0) == []
    assert plan_trips([f], truck_capacity_l=0.0) == []


def test_suggestion_still_gives_quantity_when_below_reserve_without_usage() -> None:
    """Không đo được mức dùng nhưng mức ĐÃ dưới dự trữ -> vẫn phải nói BAO NHIÊU.

    Ca thật phát hiện khi test e2e: bồn còn 0.061 / 10.425 m³, mức dùng chưa đo
    được (thiết bị chết), và lịch giao báo "không cần chuyến nào". Không biết
    KHI NÀO là đúng; nhưng "bao nhiêu" thì biết chắc — mức đích trừ mức hiện tại.
    """
    cons = estimate_consumption([], capacity_l=CAP)
    idle = estimate_idle_trend([], capacity_l=CAP)
    reserve = CAP * 0.15

    sug = suggest_order(
        volume_l=61.0, capacity_l=CAP, consumption=cons, idle=idle, now=T0,
        lead_time_days=1.0, reserve_l=reserve,
    )
    assert sug.urgency == "now"
    assert sug.order_l is not None
    assert abs(sug.order_l - (CAP * 0.9 - 61.0)) < 1e-9
    assert sug.order_at == T0  # kết luận từ MỨC, không phải mốc dự báo
    assert sug.safety_stock_l == 0.0  # chưa đo được biến động thì không bịa đệm
    assert any("không dự báo được thời điểm" in r for r in sug.reasons)
    assert any("đã dưới mức dự trữ" in r for r in sug.reasons)


def test_suggestion_stays_silent_when_level_is_fine_without_usage() -> None:
    """Mức còn trên dự trữ và chưa đo được mức dùng -> KHÔNG bịa lượng đặt."""
    cons = estimate_consumption([], capacity_l=CAP)
    idle = estimate_idle_trend([], capacity_l=CAP)
    sug = suggest_order(
        volume_l=CAP * 0.6, capacity_l=CAP, consumption=cons, idle=idle, now=T0,
        reserve_l=CAP * 0.15,
    )
    assert sug.order_l is None
    assert sug.urgency == "unknown"
    assert any("chưa cần đặt" in r for r in sug.reasons)


def test_plan_trips_includes_below_reserve_tank_without_usage_data() -> None:
    """Bồn gần cạn phải xuất hiện trong lịch giao dù chưa đo được mức dùng."""
    f = build_forecast(
        [Sample(at=T0, volume_l=61.0, pressure_mpa=0.071)],
        psn="2604200016", volume_l=61.0, capacity_l=CAP, pressure_mpa=0.071,
        now=T0, tz=VN, reading_at=T0,  # dữ liệu tươi
    )
    assert f.stale is False
    assert f.suggestion.urgency == "now"
    trips = plan_trips([f], truck_capacity_l=20_000.0, horizon_days=30.0)
    assert len(trips) == 1
    assert trips[0].stops[0].psn == "2604200016"
    assert trips[0].total_l > 9_000  # gần một xe đầy


# --------------------------------------------------------------------------- #
# Tổng CÓ DẤU: cảm biến lượng tử thô không được biến thành tiêu thụ ảo
# --------------------------------------------------------------------------- #

YKH_CAP = 60000.0


def _dither(hours: float, *, base: float, quantum: float, step_min: int = 1):
    """Cảm biến đứng yên nhưng dao động quanh MỘT lượng tử.

    Dựng lại đúng chuỗi đo được trên nhà máy ngày 2026-09-04: 2160 dòng nhịp 1
    phút, thể tích 53 290 L đầu và cuối, 139 bước -100 L và 139 bước +100 L.
    """
    out, t, up = [], T0, False
    n = int(hours * 60 / step_min)
    for i in range(n):
        # đứng yên phần lớn thời gian, thỉnh thoảng nhảy một lượng tử rồi về
        v = base - quantum if up else base
        if i % 7 == 0:
            up = not up
        out.append(Sample(at=t, volume_l=v, pressure_mpa=0.374))
        t += timedelta(minutes=step_min)
    # kết thúc ở đúng giá trị đầu -> ròng bằng 0
    out.append(Sample(at=t, volume_l=base, pressure_mpa=0.374))
    return out


def test_cam_bien_dao_dong_mot_luong_tu_khong_phai_tieu_thu() -> None:
    """Ca thật: bồn tiêu thụ 0, cách cũ báo 0,466 m³/ngày.

    Bằng chứng từ nguồn: đồng hồ khí giao 0 Nm³, lưu lượng 0 ở cả 2160 dòng, thể
    tích đầu = cuối. Tổng giảm 14 080 L đúng bằng tổng tăng 14 080 L.
    """
    s = _dither(48.0, base=53290.0, quantum=100.0)
    est = estimate_consumption(s, capacity_l=YKH_CAP)

    assert est.daily_use_l is None, "không đo được tiêu thụ thì phải nói vậy"
    assert est.drawdown_l == 0.0, "ròng bằng 0"
    assert est.rise_l > 1000.0, (
        "mức DÂNG không-phải-nạp lớn — đó là dấu hiệu cảm biến dao động, và là "
        "lý do cách tính cũ ra một con số tiêu thụ ảo"
    )
    assert est.confidence == "none"


def test_bon_rut_that_van_ra_dung_muc_dung() -> None:
    """Ranh giới: chuỗi giảm đơn điệu phải cho CÙNG kết quả như trước.

    Đây là guard chống việc bản sửa làm mất tiêu thụ thật.
    """
    s = _series(start_l=9000.0, per_day_l=500.0, hours=24 * 4)
    est = estimate_consumption(s, capacity_l=CAP)
    assert est.daily_use_l is not None
    assert abs(est.daily_use_l - 500.0) < 25.0
    # giảm đơn điệu -> KHÔNG có bước dâng nào
    assert est.rise_l == 0.0


def test_nap_giua_cua_so_khong_lam_am_muc_dung() -> None:
    """Lần nạp bị loại khỏi CẢ tử số lẫn mẫu số, nên ròng không bị nó kéo âm."""
    a = _series(start_l=9000.0, per_day_l=500.0, hours=24 * 2)
    jump = a[-1].volume_l + 6000.0
    b = _series(start_l=jump, per_day_l=500.0, hours=24 * 2)
    b = [Sample(at=x.at + timedelta(hours=48), volume_l=x.volume_l,
                pressure_mpa=x.pressure_mpa) for x in b]
    est = estimate_consumption(a + b, capacity_l=CAP)
    assert est.refills == 1
    assert est.daily_use_l is not None and est.daily_use_l > 0
    assert abs(est.daily_use_l - 500.0) < 40.0


def test_bu_nho_duoi_nguong_nap_lam_rong_thap_hon_va_no_HIEN_RA() -> None:
    """Cái giá của tổng có dấu, và lý do ``gross_drop_l`` tồn tại.

    Một lần bù nhỏ hơn refill_floor (208 L trên bồn 10 425 L) không bị nhận là
    nạp, nên nó trừ vào số ròng. Chênh lệch giữa hai con số là dấu hiệu duy nhất
    để người xem biết điều đó đã xảy ra.
    """
    s = list(_series(start_l=9000.0, per_day_l=500.0, hours=24 * 3))
    # chèn một bước dâng 150 L (dưới refill_floor 208 L) vào giữa
    mid = len(s) // 2
    s = s[:mid] + [
        Sample(at=s[mid].at, volume_l=s[mid].volume_l + 150.0, pressure_mpa=0.374)
    ] + [
        Sample(at=x.at, volume_l=x.volume_l + 150.0, pressure_mpa=x.pressure_mpa)
        for x in s[mid:]
    ]
    est = estimate_consumption(s, capacity_l=CAP)
    assert est.refills == 0, "150 L không phải một lần nạp"
    # 139,6 chứ không phải 150: bucket 30 phút gộp bước dâng với phần rút bình
    # thường của chính bucket đó (150 - 10,4). Đúng hành vi, không phải sai số.
    assert est.rise_l >= 130.0, "bước dâng phải hiện ra ở rise_l"
    # và nó ĐÃ trừ vào số ròng: đó là cái giá của tổng có dấu, hiện ra chứ không ẩn
    khong_bu = estimate_consumption(
        _series(start_l=9000.0, per_day_l=500.0, hours=24 * 3), capacity_l=CAP)
    assert khong_bu.drawdown_l - est.drawdown_l >= 140.0
