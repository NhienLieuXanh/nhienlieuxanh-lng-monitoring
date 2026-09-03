"""Dự báo từ telemetry THẬT: mức dùng/ngày, ngày tới cạn, boil-off, hold time.

Toàn bộ là **hàm thuần** trên một list ``Sample`` — không ORM, không Pydantic,
không đọc clock. ``now`` luôn là tham số. Nhờ vậy test chạy trên dữ liệu dựng tay
mà không cần DB và không cần mock thời gian, giống ``domain/alerts.py``.

Lý do tồn tại: bản kế hoạch nạp đầu tiên bắt người vận hành **gõ tay** "mức dùng
7.4 m³/ngày". Các nền tảng giám sát bồn ngoài thị trường (VMI / remote tank
monitoring) đều **tự suy** con số đó từ lịch sử và hiển thị "còn N ngày tới cạn".
Module này là phần suy đó.

Ba thang thời gian rất khác nhau, nên KHÔNG dùng chung một thuật toán:

  * **Tiêu thụ** lớn (7.4 m³/ngày ≈ 154 L mỗi 30 phút) → đo được bằng hiệu số
    giữa hai lần đọc liền nhau, chỉ cần deadband chống nhiễu cảm biến.
  * **Boil-off** rất nhỏ (~0.05 %/ngày ≈ 5 L/ngày ≈ 0.1 L mỗi 30 phút) → NẰM
    DƯỚI nhiễu cảm biến, nên hiệu số từng cặp là vô nghĩa. Phải hồi quy tuyến
    tính trên các **cửa sổ nghỉ dài** (≥ 6 giờ không rút).
  * **Áp suất tăng** cũng chậm → cùng cách như boil-off.

Thiết bị thật mất upload thường xuyên, nên mọi phép chia đều theo **thời gian
thực sự có dữ liệu** (``active_days``), không theo bề rộng cửa sổ. Nếu chia theo
bề rộng, một tuần offline sẽ kéo "mức dùng/ngày" xuống gần 0 và dự báo cạn thành
vô cực — im lặng và sai.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, tzinfo
from itertools import pairwise
from typing import Literal

# --------------------------------------------------------------------------- #
# Hằng số & ngưỡng
# --------------------------------------------------------------------------- #

#: Boil-off tham chiếu khi dữ liệu không đủ để đo. Bồn LNG lạnh sâu chân không
#: tiêu chuẩn nằm khoảng 0.05-0.3 %/ngày; lấy đầu dưới để dự báo không quá bi quan,
#: và LUÔN nhãn ``method="reference"`` để không ai tưởng đây là số đo được.
REFERENCE_BOR_PERCENT_PER_DAY = 0.05

#: Trần rót LNG. Bồn lạnh sâu không nạp đầy 100%: phải để khoảng hơi (ullage) cho
#: giãn nở nhiệt và cho áp suất làm việc. 90% là mức vận hành phổ biến và cũng
#: đúng con số công thức Excel của người dùng đang dùng (B5 = 90%).
DEFAULT_MAX_FILL_PERCENT = 90.0

#: Áp suất van an toàn mặc định (MPa). Hold time đo tới ngưỡng này.
DEFAULT_RELIEF_PRESSURE_MPA = 0.8

#: Khoảng cách tối đa giữa hai lần đọc còn được coi là liên tục. Cadence vendor
#: ~30 phút; 3 giờ = mất 6 sample. Xa hơn nữa thì không biết giữa đó có nạp hay
#: rút gì không, nên khoảng đó bị loại khỏi ``active_days`` thay vì đoán.
MAX_GAP = timedelta(hours=3)

#: Cửa sổ "nghỉ" tối thiểu để hồi quy boil-off / áp suất.
MIN_IDLE_WINDOW = timedelta(hours=6)

#: Các bước tăng liên tiếp cách nhau dưới mức này thuộc CÙNG một đợt nạp. Một chuyến
#: xe bồn bơm 20-60 phút và sinh nhiều lần đọc tăng; 2 giờ đủ rộng để phủ cả chuyến
#: kể cả khi thiết bị báo dày (đã thấy hai lần đọc cách nhau 64 giây trong lúc nạp),
#: và đủ hẹp để hai chuyến khác nhau trong ngày không bị gộp thành một.
REFILL_MERGE_HOURS = 2.0

#: z-score theo mức phục vụ (service level) cho dự trữ an toàn. Đây là bảng
#: tra một phía của phân phối chuẩn — cùng công thức reorder point mà kho vận
#: dùng: ROP = nhu cầu trong lead time + z·σ·√lead_time.
Z_BY_SERVICE_LEVEL: dict[int, float] = {
    50: 0.0,
    80: 0.842,
    90: 1.282,
    95: 1.645,
    99: 2.326,
}

Confidence = Literal["high", "medium", "low", "none"]
Method = Literal["measured", "reference", "insufficient"]
Urgency = Literal["now", "soon", "ok", "unknown"]


# --------------------------------------------------------------------------- #
# Dữ liệu vào
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Sample:
    """Một lần đọc, đã rút gọn về đúng những gì dự báo cần.

    ``at`` phải tz-aware (telemetry luôn lưu UTC). Không validate ở đây vì tầng
    repository lấy thẳng từ cột ``timestamptz``.
    """

    at: datetime
    volume_l: float | None = None
    pressure_mpa: float | None = None


def noise_floor_l(capacity_l: float | None) -> float:
    """Deadband chống nhiễu cảm biến mức, theo lít.

    Vendor trả ``currentVolume`` làm tròn tới lít trên bồn 10 425 L, và mức đo qua
    cột áp nên dao động theo sóng chất lỏng. 0.1% dung tích (~10 L) đủ để bỏ
    nhiễu mà vẫn nhỏ hơn nhiều so với một bước tiêu thụ 30 phút (~154 L).
    """
    if not capacity_l or capacity_l <= 0:
        return 5.0
    return max(capacity_l * 0.001, 3.0)


def refill_floor_l(capacity_l: float | None) -> float:
    """Ngưỡng coi một bước tăng là **lần nạp thật**, không phải nhiễu.

    Một chuyến xe LNG bơm vào hàng nghìn lít. 2% dung tích (~208 L) nằm rất xa
    trên nhiễu và rất xa dưới một lần nạp thật, nên không có vùng nhập nhằng.
    """
    if not capacity_l or capacity_l <= 0:
        return 200.0
    return max(capacity_l * 0.02, 50.0)


# --------------------------------------------------------------------------- #
# Nạp: phát hiện từ chính telemetry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RefillEvent:
    """Một lần nạp suy ra từ bước nhảy tăng của mức.

    Đây là "nhật ký nạp" mà không cần ai nhập tay và không cần bảng riêng: nó là
    hệ quả trực tiếp của dữ liệu, nên không bao giờ lệch với thực tế đo được.
    """

    at: datetime
    before_l: float
    after_l: float
    amount_l: float


def detect_refills(
    samples: list[Sample], *, capacity_l: float | None = None
) -> list[RefillEvent]:
    """Các ĐỢT nạp, không phải từng bước nhảy tăng.

    Không đòi hai lần đọc phải gần nhau: nếu thiết bị offline suốt lúc nạp thì bước
    nhảy vẫn hiện ra ở cặp bao quanh khoảng trống, và thời điểm ghi nhận là lần đọc
    SAU — muộn hơn thực tế nhưng không bỏ sót lần nạp.

    GỘP các bước tăng liên tiếp thành một đợt. Một chuyến xe bồn bơm 20-60 phút và
    sinh NHIỀU lần đọc tăng liên tiếp, mỗi lần đều có thể vượt ``refill_floor_l``
    (2% dung tích). Bản trước phát một sự kiện cho MỖI CẶP, nên trên dữ liệu thật một
    lần nạp hiện thành hai bản ghi cách nhau 64 giây (0.009 -> 2.758 -> 3.258 m³):
    nhật ký nạp đếm sai số chuyến và lượng nạp bị chia vụn.

    Đợt kết thúc khi mức thôi tăng (bồn bắt đầu vơi thì đợt tự chấm dứt) hoặc khi
    khoảng cách giữa hai lần đọc vượt ``REFILL_MERGE_HOURS``. Ngưỡng thời gian là cần
    thiết: hai chuyến xe cách nhau nửa ngày KHÔNG được gộp thành một.
    """
    floor = refill_floor_l(capacity_l)
    noise = noise_floor_l(capacity_l)
    pts = _volume_points(samples)
    out: list[RefillEvent] = []
    merge_gap = timedelta(hours=REFILL_MERGE_HOURS)

    i = 0
    while i < len(pts) - 1:
        v0 = pts[i][1]
        if pts[i + 1][1] - v0 < floor:
            i += 1
            continue
        # Mở một đợt. Kéo dài khi lần đọc kế tiếp VẪN tăng — chỉ cần trên nhiễu, không
        # cần lại vượt ngưỡng nạp, vì nhịp cuối của một chuyến bơm thường nhỏ — và vẫn
        # liền mạch về thời gian.
        j = i + 1
        while j < len(pts) - 1:
            t_cur, v_cur = pts[j]
            t_next, v_next = pts[j + 1]
            if t_next - t_cur > merge_gap or v_next - v_cur <= noise:
                break
            j += 1
        t_end, v_end = pts[j]
        out.append(
            RefillEvent(at=t_end, before_l=v0, after_l=v_end, amount_l=v_end - v0)
        )
        i = j
    return out


# --------------------------------------------------------------------------- #
# Tiêu thụ
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ConsumptionEstimate:
    """Mức dùng/ngày đo từ lịch sử, kèm mọi thứ cần để đánh giá độ tin cậy."""

    daily_use_l: float | None
    #: Độ lệch chuẩn của lượng dùng theo ngày. Đây là đầu vào của dự trữ an toàn:
    #: dùng đều thì cần đệm ít, dùng thất thường thì cần đệm nhiều.
    daily_use_sd_l: float | None
    samples: int
    #: Bề rộng cửa sổ dữ liệu (lần đọc đầu -> cuối).
    window_days: float
    #: Thời gian THỰC SỰ có dữ liệu liên tục. Mọi phép chia dùng số này.
    active_days: float
    #: active_days / window_days. Thấp = thiết bị mất upload nhiều.
    coverage: float
    drawdown_l: float
    refills: int
    refill_l: float
    #: Số ngày lịch có đủ dữ liệu để tính tổng dùng của ngày đó.
    full_days: int
    confidence: Confidence

    @property
    def ok(self) -> bool:
        return self.daily_use_l is not None and self.daily_use_l > 0


def _downsample_if_dense(
    pts: list[tuple[datetime, float]],
    *,
    bucket: timedelta = timedelta(minutes=30),
) -> list[tuple[datetime, float]]:
    """Gộp chuỗi dày hơn 30 phút về lưới 30 phút (giữ điểm cuối mỗi bucket).

    Deadband dung tích được hiệu chỉnh cho nhịp 30 phút. Ở nhịp 1 phút mọi bước
    tiêu thụ thật đều nằm dưới ngưỡng nên bị bỏ — downsample trước pairwise.
    Chuỗi đã thưa (>= 15 phút) giữ nguyên.
    """
    if len(pts) < 3:
        return pts
    gaps = sorted((b[0] - a[0] for a, b in pairwise(pts)))
    median = gaps[len(gaps) // 2]
    if median >= bucket / 2:
        return pts
    out: list[tuple[datetime, float]] = []
    bucket_end = pts[0][0] + bucket
    last = pts[0]
    for p in pts[1:]:
        if p[0] < bucket_end:
            last = p
            continue
        out.append(last)
        while p[0] >= bucket_end:
            bucket_end += bucket
        last = p
    out.append(last)
    return out


def estimate_consumption(
    samples: list[Sample],
    *,
    capacity_l: float | None = None,
    tz: tzinfo | None = None,
) -> ConsumptionEstimate:
    """Mức dùng/ngày = tổng sụt giảm / thời gian có dữ liệu.

    Đây là cách các nền tảng giám sát bồn tính "usage": cộng toàn bộ phần mức
    ĐI XUỐNG và bỏ qua phần đi lên (nạp). Không hồi quy trên cả cửa sổ, vì một
    lần nạp giữa cửa sổ sẽ làm hệ số góc hồi quy dương và ra "mức dùng âm".

    Ba thứ bị loại khỏi tổng, mỗi thứ vì một lý do khác nhau:

    * bước tăng >= ``refill_floor_l``  -> lần nạp, không phải tiêu thụ ngược;
    * ``|delta| < noise_floor_l``      -> nhiễu cảm biến, cộng vào sẽ phóng đại;
    * cặp cách nhau > ``MAX_GAP``      -> không biết giữa đó xảy ra gì, nên khoảng
      đó không được tính vào ``active_days`` (và phần sụt giảm qua khoảng trống
      cũng không được cộng — nó có thể chứa cả một lần nạp lẫn nhiều ngày rút).
    """
    raw_pts = _volume_points(samples)
    n_samples = len(raw_pts)
    pts = _downsample_if_dense(raw_pts)
    if len(pts) < 2:
        return ConsumptionEstimate(
            daily_use_l=None,
            daily_use_sd_l=None,
            samples=n_samples,
            window_days=0.0,
            active_days=0.0,
            coverage=0.0,
            drawdown_l=0.0,
            refills=0,
            refill_l=0.0,
            full_days=0,
            confidence="none",
        )

    noise = noise_floor_l(capacity_l)
    refill = refill_floor_l(capacity_l)
    window_days = (pts[-1][0] - pts[0][0]).total_seconds() / 86400.0

    drawdown = 0.0
    refill_total = 0.0
    refills = 0
    active_s = 0.0
    # Sụt giảm theo ngày lịch (giờ địa phương) để tính độ lệch chuẩn.
    per_day: dict[str, float] = {}
    covered_s: dict[str, float] = {}

    for (t0, v0), (t1, v1) in pairwise(pts):
        gap = t1 - t0
        delta = v1 - v0
        if delta >= refill:
            refills += 1
            refill_total += delta
            # Khoảng chứa lần nạp bị loại khỏi CẢ tử số lẫn mẫu số, cùng lý do
            # như khoảng trống: trong lúc bơm, mức dâng lên che mất phần đang
            # rút, nên tiêu thụ ở đó là KHÔNG QUAN SÁT ĐƯỢC. Tính nó vào
            # active_days như một khoảng "0 tiêu thụ" sẽ kéo mức dùng/ngày xuống
            # thấp giả tạo — sai lệch nhỏ nhưng luôn theo một chiều, và chiều đó
            # là chiều nguy hiểm (dự báo cạn lâu hơn thực tế).
            continue
        if gap > MAX_GAP:
            # Khoảng trống: không cộng tiêu thụ, không cộng thời gian.
            continue
        active_s += gap.total_seconds()
        key = _day_key(t1, tz)
        _bump(covered_s, key, gap.total_seconds())
        if delta <= -noise:
            drawdown += -delta
            _bump(per_day, key, -delta)

    active_days = active_s / 86400.0
    if active_days <= 0 or drawdown <= 0:
        return ConsumptionEstimate(
            daily_use_l=None,
            daily_use_sd_l=None,
            samples=len(pts),
            window_days=window_days,
            active_days=active_days,
            coverage=(active_days / window_days) if window_days > 0 else 0.0,
            drawdown_l=drawdown,
            refills=refills,
            refill_l=refill_total,
            full_days=0,
            confidence="none",
        )

    daily = drawdown / active_days

    # Độ lệch chuẩn CHỈ tính trên những ngày lịch phủ >= 80% (19.2 giờ) dữ liệu.
    # Ngày phủ một nửa có tổng dùng thấp giả tạo; trộn vào sẽ thổi phồng sigma và
    # làm dự trữ an toàn to vô lý.
    full = [per_day.get(k, 0.0) for k, s in covered_s.items() if s >= 0.8 * 86400.0]
    sd = _stdev(full) if len(full) >= 3 else None

    coverage = (active_days / window_days) if window_days > 0 else 0.0
    conf = _confidence(active_days=active_days, coverage=coverage, full_days=len(full))

    return ConsumptionEstimate(
        daily_use_l=daily,
        daily_use_sd_l=sd,
        samples=len(pts),
        window_days=window_days,
        active_days=active_days,
        coverage=coverage,
        drawdown_l=drawdown,
        refills=refills,
        refill_l=refill_total,
        full_days=len(full),
        confidence=conf,
    )


def _confidence(*, active_days: float, coverage: float, full_days: int) -> Confidence:
    """Độ tin cậy công khai, để UI không trình bày một con số mỏng như sự thật."""
    if active_days >= 7 and coverage >= 0.6 and full_days >= 5:
        return "high"
    if active_days >= 3 and coverage >= 0.4:
        return "medium"
    if active_days >= 0.5:
        return "low"
    return "none"


# --------------------------------------------------------------------------- #
# Boil-off & áp suất: hồi quy trên cửa sổ nghỉ
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class IdleTrend:
    """Xu hướng trong lúc KHÔNG rút — nơi duy nhất đo được boil-off."""

    boil_off_l_per_day: float | None
    boil_off_percent_per_day: float | None
    pressure_rise_mpa_per_day: float | None
    idle_windows: int
    idle_hours: float
    method: Method


def estimate_idle_trend(
    samples: list[Sample],
    *,
    capacity_l: float | None = None,
    reference_bor_percent_per_day: float = REFERENCE_BOR_PERCENT_PER_DAY,
) -> IdleTrend:
    """Boil-off (L/ngày) và tốc độ tăng áp (MPa/ngày) từ các cửa sổ nghỉ.

    "Nghỉ" = chuỗi lần đọc liên tục (không gap > ``MAX_GAP``) trong đó không có
    bước nào vượt deadband theo cả hai chiều — tức không rút, không nạp. Trong
    cửa sổ đó phần mức hao đi là bay hơi tự nhiên, và áp suất tăng vì hơi sinh ra
    bị giữ trong bồn kín.

    Lấy **trung vị** hệ số góc của các cửa sổ, không lấy trung bình: một cửa sổ
    dính nhiễu hoặc dính một lần rút nhỏ không bị phát hiện sẽ kéo trung bình đi
    rất xa, còn trung vị thì không.

    Không đủ cửa sổ nghỉ -> trả boil-off **tham chiếu** với ``method="reference"``.
    Đây là lựa chọn có ý thức: 0.05 %/ngày đúng hơn nhiều so với 0, và nhãn
    ``method`` giữ cho người đọc biết mình đang xem số đo hay số sách.
    """
    noise = noise_floor_l(capacity_l)
    windows = _idle_windows(samples, noise=noise)

    vol_slopes: list[float] = []
    pres_slopes: list[float] = []
    idle_s = 0.0
    for w in windows:
        idle_s += (w[-1].at - w[0].at).total_seconds()
        sv = _slope_per_day([(s.at, s.volume_l) for s in w])
        if sv is not None:
            vol_slopes.append(sv)
        sp = _slope_per_day([(s.at, s.pressure_mpa) for s in w])
        if sp is not None:
            pres_slopes.append(sp)

    bor_l: float | None = None
    if vol_slopes:
        med = _median(vol_slopes)
        # Chỉ nhận hệ số góc ÂM: bay hơi làm mức giảm. Dương nghĩa là cửa sổ đó
        # nhiễu áp đảo tín hiệu, không phải bồn tự sinh thêm LNG.
        if med is not None and med < 0:
            bor_l = -med

    pres = _median(pres_slopes) if pres_slopes else None

    method: Method
    if bor_l is None:
        if capacity_l and capacity_l > 0:
            bor_l = capacity_l * reference_bor_percent_per_day / 100.0
            method = "reference"
        else:
            method = "insufficient"
    else:
        method = "measured"

    bor_pct = None
    if bor_l is not None and capacity_l and capacity_l > 0:
        bor_pct = bor_l / capacity_l * 100.0

    return IdleTrend(
        boil_off_l_per_day=bor_l,
        boil_off_percent_per_day=bor_pct,
        pressure_rise_mpa_per_day=pres,
        idle_windows=len(windows),
        idle_hours=idle_s / 3600.0,
        method=method,
    )


def _idle_windows(samples: list[Sample], *, noise: float) -> list[list[Sample]]:
    """Các chuỗi liên tục không có biến động mức vượt deadband, dài >= 6 giờ."""
    pts = [s for s in samples if s.volume_l is not None]
    pts.sort(key=lambda s: s.at)
    out: list[list[Sample]] = []
    cur: list[Sample] = []

    def flush() -> None:
        if len(cur) >= 3 and (cur[-1].at - cur[0].at) >= MIN_IDLE_WINDOW:
            out.append(list(cur))

    for prev, nxt in pairwise(pts):
        pv = prev.volume_l
        nv = nxt.volume_l
        assert pv is not None and nv is not None
        broken = (nxt.at - prev.at) > MAX_GAP or abs(nv - pv) >= noise
        if broken:
            flush()
            cur = []
            continue
        if not cur:
            cur = [prev]
        cur.append(nxt)
    flush()
    return out


@dataclass(frozen=True, slots=True)
class HoldTime:
    """Số ngày trước khi áp suất chạm van an toàn và bồn buộc phải xả.

    Chỉ số an toàn đặc thù LNG lạnh sâu, không có ở giám sát bồn thường. Bồn
    càng vơi thì khoảng hơi càng lớn và áp tăng càng nhanh, nên hold time ngắn
    lại đúng lúc mức thấp — hai rủi ro cộng dồn.
    """

    days: float | None
    current_mpa: float | None
    relief_mpa: float
    rise_mpa_per_day: float | None
    headroom_mpa: float | None
    method: Method


def hold_time(
    *,
    current_mpa: float | None,
    rise_mpa_per_day: float | None,
    relief_mpa: float = DEFAULT_RELIEF_PRESSURE_MPA,
) -> HoldTime:
    headroom = None if current_mpa is None else relief_mpa - current_mpa
    if current_mpa is None or rise_mpa_per_day is None or rise_mpa_per_day <= 0:
        # Áp không tăng (hoặc chưa đo được) thì hold time không xác định — KHÔNG
        # phải vô cực. Trả None để UI nói "chưa đủ dữ liệu" thay vì "vô hạn ngày".
        return HoldTime(
            days=None,
            current_mpa=current_mpa,
            relief_mpa=relief_mpa,
            rise_mpa_per_day=rise_mpa_per_day,
            headroom_mpa=headroom,
            method="insufficient",
        )
    assert headroom is not None
    if headroom <= 0:
        return HoldTime(
            days=0.0,
            current_mpa=current_mpa,
            relief_mpa=relief_mpa,
            rise_mpa_per_day=rise_mpa_per_day,
            headroom_mpa=headroom,
            method="measured",
        )
    return HoldTime(
        days=headroom / rise_mpa_per_day,
        current_mpa=current_mpa,
        relief_mpa=relief_mpa,
        rise_mpa_per_day=rise_mpa_per_day,
        headroom_mpa=headroom,
        method="measured",
    )


# --------------------------------------------------------------------------- #
# Ngày tới cạn
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Runout:
    """Bao lâu nữa tới mức dự trữ, và tới cạn hẳn."""

    daily_loss_l: float | None
    days_to_reserve: float | None
    days_to_empty: float | None
    reserve_at: datetime | None
    empty_at: datetime | None


def runout(
    *,
    volume_l: float | None,
    reserve_l: float,
    daily_use_l: float | None,
    boil_off_l_per_day: float | None,
    now: datetime,
) -> Runout:
    """Cạn theo **tổng thất thoát** = rút + bay hơi.

    Cộng boil-off vào là chi tiết riêng của LNG: bồn không rút gì vẫn vơi dần, nên
    dự báo chỉ dựa trên lượng rút sẽ luôn lạc quan hơn thực tế.
    """
    loss = (daily_use_l or 0.0) + (boil_off_l_per_day or 0.0)
    if volume_l is None or loss <= 0:
        return Runout(
            daily_loss_l=loss if loss > 0 else None,
            days_to_reserve=None,
            days_to_empty=None,
            reserve_at=None,
            empty_at=None,
        )
    d_res = max(0.0, (volume_l - reserve_l) / loss)
    d_emp = max(0.0, volume_l / loss)
    return Runout(
        daily_loss_l=loss,
        days_to_reserve=d_res,
        days_to_empty=d_emp,
        reserve_at=now + timedelta(days=d_res),
        empty_at=now + timedelta(days=d_emp),
    )


# --------------------------------------------------------------------------- #
# Đề xuất đặt hàng
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class OrderSuggestion:
    """Đề xuất "đặt bao nhiêu, đặt khi nào" kèm lý do bằng chữ.

    Đây là mô hình **điểm đặt hàng lại có dự trữ an toàn thống kê** — đúng thứ
    ngành kho vận và VMI dùng — chứ không phải một model ngôn ngữ. ``reasons``
    tồn tại để mỗi con số đều truy được về đầu vào; một đề xuất không giải thích
    được thì người vận hành sẽ không dám dùng.
    """

    order_l: float | None
    order_at: datetime | None
    deliver_at: datetime | None
    target_l: float
    reorder_point_l: float
    safety_stock_l: float
    lead_time_days: float
    service_level: int
    urgency: Urgency
    reasons: list[str] = field(default_factory=list)


def suggest_order(
    *,
    volume_l: float | None,
    capacity_l: float | None,
    consumption: ConsumptionEstimate,
    idle: IdleTrend,
    now: datetime,
    lead_time_days: float = 1.0,
    service_level: int = 95,
    max_fill_percent: float = DEFAULT_MAX_FILL_PERCENT,
    reserve_l: float | None = None,
) -> OrderSuggestion:
    """Tính điểm đặt hàng lại, thời điểm đặt và lượng đặt.

    Chuỗi suy luận, mỗi bước đều vào ``reasons``:

    1. Thất thoát/ngày = mức dùng đo được + boil-off.
    2. Dự trữ an toàn = z(service level) · sigma · sqrt(lead_time). sigma chưa đo
       được thì lấy 25% mức dùng làm đại diện — biến động thật của phụ tải công
       nghiệp thường quanh mức đó, và nói rõ trong ``reasons`` rằng đây là giả định.
    3. Điểm đặt hàng lại = thất thoát trong lead time + dự trữ an toàn. Nếu người
       vận hành đã đặt mức dự trữ riêng thì lấy giá trị LỚN HƠN — chính sách của
       người vận hành là sàn, không bị mô hình hạ xuống.
    4. Mức đích = ``max_fill_percent`` x dung tích (chừa ullage cho giãn nở nhiệt).
    5. Lượng đặt = mức đích - mức dự kiến LÚC XE TỚI, không phải mức hiện tại:
       giữa lúc đặt và lúc giao bồn vẫn vơi tiếp.
    """
    reasons: list[str] = []
    daily_use = consumption.daily_use_l
    bor = idle.boil_off_l_per_day or 0.0

    if not capacity_l or capacity_l <= 0:
        return OrderSuggestion(
            order_l=None,
            order_at=None,
            deliver_at=None,
            target_l=0.0,
            reorder_point_l=0.0,
            safety_stock_l=0.0,
            lead_time_days=lead_time_days,
            service_level=service_level,
            urgency="unknown",
            reasons=["Chưa khai báo dung tích bồn nên không tính được lượng đặt."],
        )
    if daily_use is None or daily_use <= 0 or volume_l is None:
        # Không đo được mức dùng thì KHÔNG biết "khi nào" — nhưng nếu mức đã dưới
        # dự trữ thì vẫn biết chắc "cần đặt ngay" và biết "bao nhiêu" (mức đích trừ
        # mức hiện tại). Trả None cho cả hai câu là bỏ mất một kết luận chắc chắn:
        # phát hiện khi test e2e, bồn còn 0.06/10.43 m³ mà lịch giao báo "không cần
        # chuyến nào".
        target = capacity_l * max_fill_percent / 100.0
        floor = reserve_l if reserve_l is not None else 0.0
        no_use = (
            "Chưa đo được mức tiêu thụ/ngày từ dữ liệu lịch sử (thiết bị ngoại tuyến hoặc "
            "chưa đủ dữ liệu) nên không dự báo được thời điểm cần đặt hàng."
        )
        if volume_l is not None and volume_l < floor:
            return OrderSuggestion(
                order_l=max(0.0, target - volume_l),
                # Không có mức dùng thì không suy được thời điểm; "ngay" là kết luận
                # từ MỨC HIỆN TẠI, nên order_at = now chứ không phải một mốc dự báo.
                order_at=now,
                deliver_at=now + timedelta(days=lead_time_days),
                target_l=target,
                reorder_point_l=floor,
                safety_stock_l=0.0,
                lead_time_days=lead_time_days,
                service_level=service_level,
                urgency="now",
                reasons=[
                    no_use,
                    f"Tuy nhiên thể tích hiện tại {volume_l / 1000:.2f} m³ đã dưới mức dự trữ "
                    f"{floor / 1000:.2f} m³ — cần đặt hàng ngay.",
                    f"Lượng đặt = thể tích đích {target / 1000:.2f} m³ "
                    f"({max_fill_percent:g}% dung tích, chừa khoảng hơi) trừ thể tích hiện tại.",
                    "Chưa tính được dự trữ an toàn — cần đo được biến động tiêu thụ.",
                ],
            )
        return OrderSuggestion(
            order_l=None,
            order_at=None,
            deliver_at=None,
            target_l=target,
            reorder_point_l=floor,
            safety_stock_l=0.0,
            lead_time_days=lead_time_days,
            service_level=service_level,
            urgency="unknown",
            reasons=[
                no_use,
                f"Thể tích hiện tại còn trên mức dự trữ {floor / 1000:.2f} m³ nên chưa cần "
                "đặt hàng; hệ thống tiếp tục theo dõi khi thiết bị có dữ liệu.",
            ],
        )

    loss = daily_use + bor
    reasons.append(
        f"Thất thoát {loss / 1000:.2f} m³/ngày = tiêu thụ {daily_use / 1000:.2f} "
        f"+ bay hơi tự nhiên {bor / 1000:.2f} m³/ngày"
        + (" (bay hơi lấy theo giá trị tham chiếu 0.05%/ngày)" if idle.method == "reference" else "")
    )

    z = Z_BY_SERVICE_LEVEL.get(service_level, 1.645)
    sd = consumption.daily_use_sd_l
    if sd is None:
        sd = daily_use * 0.25
        reasons.append(
            f"Chưa đủ số ngày dữ liệu đầy đủ để đo biến động; giả định độ lệch chuẩn "
            f"bằng 25% mức tiêu thụ = {sd / 1000:.2f} m³/ngày"
        )
    else:
        reasons.append(
            f"Biến động tiêu thụ đo được: độ lệch chuẩn {sd / 1000:.2f} m³/ngày "
            f"({consumption.full_days} ngày dữ liệu đầy đủ)"
        )

    safety = z * sd * math.sqrt(max(lead_time_days, 0.0))
    reasons.append(
        f"Dự trữ an toàn = {z:.3f} (mức phục vụ {service_level}%) × độ lệch chuẩn × "
        f"căn bậc hai của {lead_time_days:g} ngày = {safety / 1000:.2f} m³"
    )

    rop = loss * lead_time_days + safety
    if reserve_l is not None and reserve_l > rop:
        reasons.append(
            f"Mức dự trữ do người vận hành đặt ({reserve_l / 1000:.2f} m³) cao hơn "
            f"điểm đặt hàng tính được ({rop / 1000:.2f} m³) — áp dụng theo người vận hành"
        )
        rop = reserve_l
    else:
        reasons.append(
            f"Điểm đặt hàng lại = thất thoát trong {lead_time_days:g} ngày giao hàng "
            f"+ dự trữ an toàn = {rop / 1000:.2f} m³"
        )

    days_to_rop = (volume_l - rop) / loss
    order_at = now + timedelta(days=max(0.0, days_to_rop))
    deliver_at = order_at + timedelta(days=lead_time_days)

    target = capacity_l * max_fill_percent / 100.0
    level_at_delivery = max(
        0.0, volume_l - loss * max(0.0, days_to_rop + lead_time_days)
    )
    order = max(0.0, target - level_at_delivery)

    urgency: Urgency
    if days_to_rop <= 0:
        urgency = "now"
        reasons.append(
            f"Thể tích hiện tại {volume_l / 1000:.2f} m³ đã dưới điểm đặt hàng — cần đặt ngay"
        )
    elif days_to_rop <= 2:
        urgency = "soon"
        reasons.append(f"Còn {days_to_rop:.1f} ngày nữa tới điểm đặt hàng lại")
    else:
        urgency = "ok"
        reasons.append(f"Còn {days_to_rop:.1f} ngày nữa tới điểm đặt hàng lại")

    reasons.append(
        f"Lượng đặt = thể tích đích {target / 1000:.2f} m³ ({max_fill_percent:g}% dung tích, "
        f"chừa khoảng hơi) trừ thể tích dự kiến lúc giao {level_at_delivery / 1000:.2f} m³"
    )
    if consumption.confidence in ("low", "none"):
        reasons.append(
            "Lưu ý: độ tin cậy thấp do dữ liệu lịch sử còn ít — cần đối chiếu với thực tế "
            "trước khi chốt đơn hàng"
        )

    return OrderSuggestion(
        order_l=order,
        order_at=order_at,
        deliver_at=deliver_at,
        target_l=target,
        reorder_point_l=rop,
        safety_stock_l=safety,
        lead_time_days=lead_time_days,
        service_level=service_level,
        urgency=urgency,
        reasons=reasons,
    )


# --------------------------------------------------------------------------- #
# Gộp
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ForecastAlert:
    """Cảnh báo suy từ dự báo, KHÁC với cảnh báo thiết bị ở ``domain/alerts.py``.

    ``alerts.py`` trả lời "thiết bị/bồn đang thế nào NGAY BÂY GIỜ" (offline, mức
    thấp, pin yếu). Ở đây là "sắp xảy ra chuyện gì" — chỉ tính được khi có chuỗi
    thời gian. Tách ra vì chúng có thời gian sống khác nhau, nhưng cùng đi qua
    một hàm để **notifier gửi email và dashboard hiển thị không bao giờ lệch nhau**.
    """

    psn: str
    code: str
    severity: str
    message: str
    value: float | None = None
    threshold: float | None = None


#: Lần đọc cũ hơn ngưỡng này thì mọi dự báo hướng tới tương lai bị đánh dấu
#: ``stale`` và KHÔNG phát cảnh báo. Phát hiện khi test e2e trên thiết bị thật:
#: hai bồn đã offline hàng tháng, và chiếu "còn 11.7 ngày tới cạn" từ một con số
#: đo từ tháng trước là bịa — bồn có thể đã được nạp đầy bằng tay từ lâu. Trạng
#: thái thật ở đây là "mất liên lạc", và đã có cảnh báo OFFLINE lo việc đó.
#: 24 giờ (không phải 90 phút như ngưỡng online/offline): một lần đọc cách đây
#: vài giờ vẫn đủ để dự báo nhiều ngày, chỉ mất liên lạc cả ngày mới thành vô nghĩa.
MAX_READING_AGE_DAYS = 1.0

#: Ngưỡng cảnh báo cạn: dưới 3 ngày là hết thời gian xoay xe trong thực tế.
ALERT_RUNOUT_DAYS = 3.0
#: Hold time tối thiểu. 5 ngày là mức sàn mà quy chuẩn bồn lạnh sâu ở Mỹ/Canada
#: dùng, nên lấy đúng con số đó thay vì tự đặt.
ALERT_HOLD_DAYS = 5.0
#: Boil-off đo được vượt ngưỡng này là dấu hiệu chân không lớp cách nhiệt suy
#: giảm — đây là cảnh báo BẢO TRÌ, không phải cảnh báo tồn kho.
ALERT_BOR_PERCENT_MAX = 0.30


def forecast_alerts(
    f: Forecast,
    *,
    runout_days: float = ALERT_RUNOUT_DAYS,
    hold_days: float = ALERT_HOLD_DAYS,
    bor_percent_max: float = ALERT_BOR_PERCENT_MAX,
) -> list[ForecastAlert]:
    """Ba cảnh báo mà giám sát bồn thường KHÔNG có, và LNG thì bắt buộc phải có.

    Số liệu cũ (``f.stale``) thì RUNOUT và HOLD_TIME **không phát**: cả hai là suy
    diễn từ MỨC và ÁP hiện tại, mà "hiện tại" ở đây có thể là số của tháng trước.
    Gửi email "còn 0 ngày tới cạn" dựa trên số liệu chết là cách nhanh nhất để
    người nhận mất tin vào toàn bộ hệ thống cảnh báo. Việc mất liên lạc đã có
    cảnh báo OFFLINE riêng, đúng bản chất vấn đề hơn.

    BOIL_OFF_HIGH thì KHÔNG bị chặn: nó suy từ các cửa sổ nghỉ trong lịch sử, là
    một kết luận về tình trạng cách nhiệt của bồn, không phải về mức hiện tại.
    """
    out: list[ForecastAlert] = []

    d = f.runout.days_to_reserve
    if f.stale:
        d = None

    if d is not None and d <= runout_days:
        sev = "critical" if d <= 1.0 else "warning"
        out.append(
            ForecastAlert(
                f.psn, "RUNOUT", sev,
                f"Còn {d:.1f} ngày tới mức dự trữ "
                f"({f.reserve_l / 1000:.2f} m³) — cần đặt hàng",
                d, runout_days,
            )
        )

    h = None if f.stale else f.hold.days
    if h is not None and h <= hold_days:
        sev = "critical" if h <= 1.0 else "warning"
        out.append(
            ForecastAlert(
                f.psn, "HOLD_TIME", sev,
                f"Thời gian giữ áp còn {h:.1f} ngày — áp suất sẽ chạm van an toàn "
                f"{f.hold.relief_mpa:g} MPa và bồn phải xả",
                h, hold_days,
            )
        )

    # CHỈ cảnh báo khi boil-off là số ĐO ĐƯỢC. Con số tham chiếu 0.05%/ngày là
    # hằng số của chúng ta, báo động trên nó thì mãi mãi không bao giờ fire hoặc
    # mãi mãi fire — cả hai đều vô nghĩa.
    if f.idle.method == "measured" and f.idle.boil_off_percent_per_day is not None:
        bor = f.idle.boil_off_percent_per_day
        if bor > bor_percent_max:
            out.append(
                ForecastAlert(
                    f.psn, "BOIL_OFF_HIGH", "warning",
                    f"Bay hơi tự nhiên {bor:.3f}%/ngày vượt ngưỡng {bor_percent_max:g}% "
                    "— nghi chân không lớp cách nhiệt suy giảm, cần kiểm tra bồn",
                    bor, bor_percent_max,
                )
            )
    return out


@dataclass(frozen=True, slots=True)
class Forecast:
    psn: str
    volume_l: float | None
    capacity_l: float | None
    fill_percent: float | None
    reserve_l: float
    consumption: ConsumptionEstimate
    idle: IdleTrend
    runout: Runout
    hold: HoldTime
    suggestion: OrderSuggestion
    refills: list[RefillEvent]
    generated_at: datetime
    #: Thời điểm của lần đọc mà mọi con số "hiện tại" dựa vào.
    reading_at: datetime | None = None
    reading_age_days: float | None = None
    #: True = lần đọc quá cũ để chiếu về tương lai. Không phải lỗi, là sự thật cần
    #: nói ra: mọi con số runout/hold time bên dưới chỉ là "nếu bồn vẫn đang chạy
    #: như lúc đó". Cảnh báo bị chặn khi cờ này bật (xem forecast_alerts).
    stale: bool = False
    alerts: list[ForecastAlert] = field(default_factory=list)


def build_forecast(
    samples: list[Sample],
    *,
    psn: str,
    volume_l: float | None,
    capacity_l: float | None,
    pressure_mpa: float | None,
    now: datetime,
    tz: tzinfo | None = None,
    reserve_percent: float = 15.0,
    reserve_l: float | None = None,
    lead_time_days: float = 1.0,
    service_level: int = 95,
    relief_mpa: float = DEFAULT_RELIEF_PRESSURE_MPA,
    max_fill_percent: float = DEFAULT_MAX_FILL_PERCENT,
    reading_at: datetime | None = None,
    max_reading_age_days: float = MAX_READING_AGE_DAYS,
) -> Forecast:
    """Chạy toàn bộ chuỗi dự báo cho một bồn.

    ``reading_at`` là thời điểm của lần đọc cấp ``volume_l``/``pressure_mpa``. Cũ
    hơn ``max_reading_age_days`` thì kết quả bị đánh dấu ``stale`` và cảnh báo bị
    chặn — xem ``MAX_READING_AGE_DAYS``.
    """
    cons = estimate_consumption(samples, capacity_l=capacity_l, tz=tz)
    idle = estimate_idle_trend(samples, capacity_l=capacity_l)
    res = reserve_l
    if res is None:
        res = (capacity_l or 0.0) * reserve_percent / 100.0
    ro = runout(
        volume_l=volume_l,
        reserve_l=res,
        daily_use_l=cons.daily_use_l,
        boil_off_l_per_day=idle.boil_off_l_per_day,
        now=now,
    )
    hold = hold_time(
        current_mpa=pressure_mpa,
        rise_mpa_per_day=idle.pressure_rise_mpa_per_day,
        relief_mpa=relief_mpa,
    )
    sug = suggest_order(
        volume_l=volume_l,
        capacity_l=capacity_l,
        consumption=cons,
        idle=idle,
        now=now,
        lead_time_days=lead_time_days,
        service_level=service_level,
        max_fill_percent=max_fill_percent,
        reserve_l=res,
    )
    fill = None
    if volume_l is not None and capacity_l and capacity_l > 0:
        fill = volume_l / capacity_l * 100.0
    age = None if reading_at is None else (now - reading_at).total_seconds() / 86400.0
    # reading_at là None -> coi là stale: không có lần đọc nào thì không có cơ sở
    # nào để nói về tương lai. Mặc định an toàn là im lặng, không phải cảnh báo.
    stale = age is None or age > max_reading_age_days
    f = Forecast(
        psn=psn,
        volume_l=volume_l,
        capacity_l=capacity_l,
        fill_percent=fill,
        reserve_l=res,
        consumption=cons,
        idle=idle,
        runout=ro,
        hold=hold,
        suggestion=sug,
        refills=detect_refills(samples, capacity_l=capacity_l),
        generated_at=now,
        reading_at=reading_at,
        reading_age_days=age,
        stale=stale,
    )
    # Cảnh báo phải nằm TRONG payload dự báo, không tính lại ở tầng trên: nếu
    # dashboard và notifier mỗi bên tự suy thì chúng sẽ lệch nhau đúng vào lúc
    # có sự cố. Một nguồn sự thật.
    return replace(f, alerts=forecast_alerts(f))


# --------------------------------------------------------------------------- #
# Điều phối chuyến giao (nhiều bồn / một xe)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DeliveryStop:
    psn: str
    name: str | None
    order_l: float
    days_to_reserve: float | None
    urgency: Urgency


@dataclass(frozen=True, slots=True)
class DeliveryTrip:
    seq: int
    stops: list[DeliveryStop]
    total_l: float
    truck_capacity_l: float


def plan_trips(
    forecasts: list[Forecast],
    *,
    truck_capacity_l: float,
    horizon_days: float = 7.0,
    names: dict[str, str | None] | None = None,
) -> list[DeliveryTrip]:
    """Gom các bồn cần nạp trong ``horizon_days`` thành các chuyến theo tải xe.

    Greedy theo độ gấp (ngày tới mức dự trữ tăng dần) — bồn nguy cấp nhất luôn
    nằm ở chuyến đầu. Cố ý KHÔNG tối ưu tuyến đường: khoảng cách giữa các kho
    chưa có trong dữ liệu, nên một "tối ưu" theo thứ tự PSN sẽ chỉ là số đẹp mà
    vô nghĩa. Khi nào có toạ độ kho thì thêm bước sắp tuyến ở đây.

    Bồn có ``stale`` bị LOẠI: điều một chuyến xe với lượng hàng tính từ mức đo
    tháng trước là một hành động vận hành thật dựa trên số liệu đã chết. Bồn đó
    cần người gọi kiểm tra thiết bị, không cần một dòng trong lịch giao.
    """
    if truck_capacity_l <= 0:
        return []
    cand = [
        f
        for f in forecasts
        if not f.stale
        and f.suggestion.order_l
        and f.suggestion.order_l > 0
        and f.runout.days_to_reserve is not None
        and f.runout.days_to_reserve <= horizon_days
    ]
    cand.sort(key=lambda f: (f.runout.days_to_reserve or 0.0))

    trips: list[DeliveryTrip] = []
    cur: list[DeliveryStop] = []
    load = 0.0
    for f in cand:
        qty = min(f.suggestion.order_l or 0.0, truck_capacity_l)
        if cur and load + qty > truck_capacity_l:
            trips.append(DeliveryTrip(len(trips) + 1, cur, load, truck_capacity_l))
            cur, load = [], 0.0
        cur.append(
            DeliveryStop(
                psn=f.psn,
                name=(names or {}).get(f.psn),
                order_l=qty,
                days_to_reserve=f.runout.days_to_reserve,
                urgency=f.suggestion.urgency,
            )
        )
        load += qty
    if cur:
        trips.append(DeliveryTrip(len(trips) + 1, cur, load, truck_capacity_l))
    return trips


# --------------------------------------------------------------------------- #
# Tiện ích số học
# --------------------------------------------------------------------------- #


def _volume_points(samples: list[Sample]) -> list[tuple[datetime, float]]:
    pts = [(s.at, float(s.volume_l)) for s in samples if s.volume_l is not None]
    pts.sort(key=lambda p: p[0])
    return pts


def _day_key(at: datetime, tz: tzinfo | None) -> str:
    local = at.astimezone(tz) if tz is not None else at
    return local.date().isoformat()


def _bump(d: dict[str, float], key: str, amount: float) -> None:
    d[key] = d.get(key, 0.0) + amount


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _stdev(xs: list[float]) -> float | None:
    """Độ lệch chuẩn mẫu (n-1). Cần >= 2 điểm."""
    if len(xs) < 2:
        return None
    mean = sum(xs) / len(xs)
    var = sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(var)


def _slope_per_day(pts: list[tuple[datetime, float | None]]) -> float | None:
    """Hệ số góc hồi quy bình phương tối thiểu, đơn vị /ngày.

    Dùng ngày làm đơn vị x ngay từ đầu (không phải giây) để không phải nhân
    86400 ở chỗ khác và không ai nhầm đơn vị.
    """
    clean = [(t, v) for t, v in pts if v is not None]
    if len(clean) < 3:
        return None
    t0 = clean[0][0]
    xs = [(t - t0).total_seconds() / 86400.0 for t, _ in clean]
    ys = [float(v) for _, v in clean]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return sxy / sxx
