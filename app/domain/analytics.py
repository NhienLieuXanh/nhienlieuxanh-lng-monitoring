"""Phân tích: chất lượng dữ liệu, sức khoẻ thiết bị, bất thường.

Hàm thuần, không DB, không tên vendor — cùng khuôn với ``domain/forecast.py`` nên
test được mà không cần Postgres và không cần mock thời gian.

Vì sao ba hàm riêng chứ không một "mô hình": đây là ba câu hỏi khác nhau trên ba
thang thời gian khác nhau, và gộp lại thì mỗi câu đều trả lời tệ hơn.

    chất lượng dữ liệu   phút        "con số phía dưới đáng tin bao nhiêu?"
    bất thường           giờ         "vừa có gì lạ trong mức chứa?"
    sức khoẻ thiết bị    tuần        "thiết bị này còn báo được bao lâu nữa?"

Vì sao THUẦN PYTHON, không numpy/scipy/sklearn
----------------------------------------------
Cỡ dữ liệu thật hiện tại là ~1.600 lần đo cho toàn đội bồn. Theil-Sen O(n²) trên
400 điểm là ~80 nghìn phép chia — dưới một phần mười giây. Đổi lại, numpy thêm hàng
chục MB vào bundle serverless và làm chậm cold start của MỌI request, kể cả request
không dùng tới nó. Khi nào đội bồn lên hàng trăm thiết bị thì tính lại; hiện tại
thêm dependency là trả giá mà không mua được gì.

Vì sao KHÔNG có mô hình học có giám sát
---------------------------------------
Đã đo trên dữ liệu thật: 2 thiết bị, ~1.600 lần đo, độ phủ 23% và 82%, và **không
có chu kỳ nạp sạch nào** (một bồn 0 lần, bồn kia 3 lần nhưng dồn trong 2 ngày nên
gần chắc là một lần nạp bị tách ba). Không nhãn, không chu kỳ, một địa điểm. Gắn
hồi quy có giám sát hay mạng nơ-ron vào đây sẽ cho ra con số trông thuyết phục mà
không đo được gì thật. Những gì dùng ở đây — Theil-Sen, MAD, phân đoạn nhị phân —
đều là thống kê bền / học không giám sát, đúng ở cỡ vài trăm điểm, và nói được khi
nào chúng KHÔNG đủ dữ liệu.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Literal

from app.domain.forecast import Sample, noise_floor_l

# --------------------------------------------------------------------------- #
# Hằng số — mỗi cái kèm lý do, vì đây là chỗ người sau sẽ muốn sửa
# --------------------------------------------------------------------------- #

# Cadence vendor đã đo từ dữ liệu thật (xem tests/test_mapping.py). Dùng làm cadence
# kỳ vọng khi chuỗi quá ngắn để tự suy ra.
DEFAULT_CADENCE_MINUTES = 30.0

# Khoảng trống lớn hơn 3 lần cadence mới tính là mất dữ liệu. Nhỏ hơn thì chỉ là một
# lần upload trượt — thiết bị này báo sóng 15-20%, trượt là chuyện thường.
GAP_FACTOR = 3.0

# 1.4826 đưa MAD về cùng thang với độ lệch chuẩn, nên z=4 xấp xỉ 4 sigma. Cố ý bảo
# thủ: một màn hình cảnh báo kêu sai vài lần là một màn hình không ai còn nhìn.
MAD_TO_SIGMA = 1.4826
ANOMALY_Z = 4.0

# Chuỗi giá trị y hệt nhau dài hơn mức này = cảm biến kẹt, không phải bồn đứng yên.
# 6 giờ = 12 mẫu: đủ dài để không bắt oan một bồn thật sự không dùng gì ban đêm.
FLATLINE_HOURS = 6.0

# Pin lithium primary 3.6 V. 3.40 V khớp ngưỡng cảnh báo phía server; dưới ~3.0 V khi
# có tải là hết tuổi thọ thật. Xem lại khi biết chemistry chính xác.
BATTERY_WARN_V = 3.40
BATTERY_DEAD_V = 3.00

# Dưới mức này coi là sóng quá yếu để upload ổn định.
SIGNAL_FLOOR_PERCENT = 10.0

# Cỡ mẫu tối thiểu để nói bất cứ điều gì về xu hướng. Dưới ngưỡng này trả "chưa đủ
# dữ liệu" — thà im lặng hơn đưa một độ dốc dựng từ 4 điểm.
MIN_TREND_SAMPLES = 12

# Im lâu hơn mức này thì coi là ĐÃ ngừng báo, không còn là dự đoán. Cố ý tính bằng
# NGÀY chứ không theo bội số cadence: 4 lần cadence chỉ là 2 giờ, và một thiết bị
# trượt vài lần upload trong 2 giờ chưa phải lý do để ai lái xe ra hiện trường. Ba
# ngày thì không còn cách giải thích nào khác.
SILENT_DEAD_DAYS = 3.0

# Trần số điểm cho Theil-Sen. 400 điểm = 79.800 cặp; trên mức này lấy mẫu theo bước
# đều (KHÔNG ngẫu nhiên: cùng đầu vào phải cho cùng kết quả).
THEIL_SEN_MAX_POINTS = 400

Grade = Literal["cao", "trung bình", "thấp", "không dùng được"]
Risk = Literal["cao", "trung bình", "thấp", "chưa đủ dữ liệu"]
AnomalyKind = Literal["sụt bất thường", "tăng bất thường", "cảm biến kẹt"]


# --------------------------------------------------------------------------- #
# Thống kê bền
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TrendFit:
    """Một đường thẳng khớp bền, kèm khoảng bất định.

    ``lo``/``hi`` là phân vị 5-95 của PHÂN BỐ ĐỘ DỐC TỪNG CẶP, không phải khoảng tin
    cậy tham số. Nói rõ vì hai thứ này không thay thế nhau: nó là thước đo "dữ liệu
    này nhất quán đến đâu", đủ để quyết định có hiển thị con số hay không.
    """

    slope_per_day: float
    intercept: float
    lo_per_day: float
    hi_per_day: float
    n: int

    @property
    def spread(self) -> float:
        return self.hi_per_day - self.lo_per_day


def mad(xs: list[float], center: float | None = None) -> float:
    """Độ lệch tuyệt đối trung vị. Không bị một điểm rác kéo lệch như stdev."""
    if not xs:
        return 0.0
    c = statistics.median(xs) if center is None else center
    return statistics.median([abs(x - c) for x in xs])


def _thin(items: list[tuple[float, float]], cap: int) -> list[tuple[float, float]]:
    """Lấy mẫu theo bước đều. Xác định, không ngẫu nhiên: cùng vào -> cùng ra."""
    if len(items) <= cap:
        return items
    step = len(items) / cap
    return [items[int(i * step)] for i in range(cap)]


def theil_sen(points: list[tuple[float, float]]) -> TrendFit | None:
    """Hồi quy Theil-Sen: trung vị của độ dốc từng cặp.

    Chọn nó thay bình phương tối thiểu vì dữ liệu này CÓ điểm rác — vendor gửi 0 thay
    cho null, cảm biến nhảy, và một lần nạp là bậc nhảy thật. Bình phương tối thiểu
    để một điểm ngoại lai kéo cả đường; trung vị chịu được tới ~29% dữ liệu bị nhiễm
    trước khi vỡ.

    ``points`` là (ngày, giá trị). Trả None khi không đủ điểm phân biệt.
    """
    pts = _thin(sorted(points), THEIL_SEN_MAX_POINTS)
    if len(pts) < 3:
        return None
    slopes = [
        (y2 - y1) / (x2 - x1)
        for i, (x1, y1) in enumerate(pts)
        for x2, y2 in pts[i + 1 :]
        if x2 != x1
    ]
    if not slopes:
        return None
    slopes.sort()
    slope = statistics.median(slopes)
    # Trung vị của (y - slope*x) là hệ số chắn bền tương ứng của Theil-Sen.
    intercept = statistics.median([y - slope * x for x, y in pts])
    lo = slopes[max(0, int(0.05 * (len(slopes) - 1)))]
    hi = slopes[min(len(slopes) - 1, int(0.95 * (len(slopes) - 1)))]
    return TrendFit(slope, intercept, lo, hi, len(pts))


def _days(base: datetime, at: datetime) -> float:
    return (at - base).total_seconds() / 86400.0


def window_start(now: datetime, days: float) -> datetime:
    return now - timedelta(days=days)


# --------------------------------------------------------------------------- #
# 1. Chất lượng dữ liệu — cửa chặn trước mọi con số khác
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class QualityReport:
    samples: int
    window_days: float
    cadence_minutes: float | None
    cadence_jitter_minutes: float | None
    expected_samples: int
    coverage: float
    gaps: int
    longest_gap_hours: float | None
    flatline_runs: int
    longest_flatline_hours: float | None
    grade: Grade
    reasons: list[str] = field(default_factory=list)


def _flatline_runs(pts: list[Sample]) -> tuple[int, float]:
    """Đếm chuỗi giá trị đứng yên và chuỗi dài nhất (giờ)."""
    runs, longest, i = 0, 0.0, 0
    while i < len(pts) - 1:
        j = i
        while j + 1 < len(pts) and pts[j + 1].volume_l == pts[i].volume_l:
            j += 1
        if j > i:
            hours = (pts[j].at - pts[i].at).total_seconds() / 3600.0
            if hours >= FLATLINE_HOURS:
                runs += 1
                longest = max(longest, hours)
        i = max(j, i + 1)
    return runs, longest


def assess_quality(
    samples: list[Sample],
    *,
    now: datetime,
    window_days: float,
    cadence_minutes: float = DEFAULT_CADENCE_MINUTES,
) -> QualityReport:
    """Chấm điểm chuỗi TRƯỚC khi bất cứ ai suy luận từ nó.

    Hàm này đứng đầu module có chủ ý. Mọi con số phía sau — độ dốc pin, thời gian tới
    cạn, điểm bất thường — chỉ đúng bằng mức đúng của chuỗi sinh ra nó. Hiện một dự
    báo mà không nói độ phủ 23% là nói một nửa sự thật.
    """
    pts = sorted((s for s in samples if s.volume_l is not None), key=lambda s: s.at)
    if len(pts) < 2:
        return QualityReport(
            samples=len(pts),
            window_days=window_days,
            cadence_minutes=None,
            cadence_jitter_minutes=None,
            expected_samples=0,
            coverage=0.0,
            gaps=0,
            longest_gap_hours=None,
            flatline_runs=0,
            longest_flatline_hours=None,
            grade="không dùng được",
            reasons=["Chưa có đủ hai lần đo để đánh giá."],
        )

    deltas = [
        (b.at - a.at).total_seconds() / 60.0 for a, b in pairwise(pts)
    ]
    med_gap = statistics.median(deltas)
    jitter = mad(deltas, med_gap)

    # Cadence kỳ vọng suy TỪ DỮ LIỆU nếu chuỗi đủ dài, không thì lấy hằng số. Suy từ
    # dữ liệu quan trọng vì nếu vendor đổi cadence thì độ phủ tính theo hằng số cũ sẽ
    # sai và không ai biết.
    cadence = med_gap if len(deltas) >= MIN_TREND_SAMPLES else cadence_minutes
    cadence = max(cadence, 1.0)

    span_min = (pts[-1].at - pts[0].at).total_seconds() / 60.0
    # Mẫu số là CỬA SỔ ĐƯỢC YÊU CẦU, không phải khoảng dữ liệu quan sát được. Nếu lấy
    # khoảng quan sát thì một thiết bị chỉ báo 5 ngày trong cửa sổ 30 ngày vẫn hiện
    # "độ phủ 100%" — đúng con số dối mà module này ra đời để chặn. Đo trên cửa sổ thì
    # thiết bị chết hiện ra là dữ liệu thiếu, vì đó chính là nó.
    expected = max(1, round(window_days * 1440.0 / cadence))
    coverage = min(1.0, len(pts) / expected)

    gap_limit = cadence * GAP_FACTOR
    big = [d for d in deltas if d > gap_limit]
    runs, longest_flat = _flatline_runs(pts)

    reasons = [
        f"{len(pts)} lần đo, kỳ vọng {expected} cho cửa sổ {window_days:.0f} ngày "
        f"ở nhịp {cadence:.0f} phút.",
        f"Độ phủ {coverage * 100:.0f}%.",
    ]
    # Nói riêng khoảng dữ liệu thật: độ phủ thấp vì thiết bị chết KHÁC độ phủ thấp vì
    # thiết bị báo chập chờn suốt cửa sổ, và hai ca đó cần hai hành động khác nhau.
    if span_min < window_days * 1440.0 * 0.9:
        reasons.append(
            f"Dữ liệu chỉ trải {span_min / 1440.0:.1f} ngày trong cửa sổ "
            f"{window_days:.0f} ngày."
        )
    if big:
        reasons.append(
            f"{len(big)} khoảng trống dài hơn {gap_limit / 60.0:.1f} giờ, "
            f"dài nhất {max(big) / 60.0:.1f} giờ."
        )
    if runs:
        reasons.append(
            f"{runs} lần giá trị đứng yên trên {FLATLINE_HOURS:.0f} giờ "
            f"(dài nhất {longest_flat:.1f} giờ) — nghi cảm biến kẹt."
        )
    age_h = (now - pts[-1].at).total_seconds() / 3600.0
    if age_h > 24.0:
        reasons.append(f"Lần đo cuối cách đây {age_h / 24.0:.1f} ngày.")

    # Độ phủ là tiêu chí chính; cảm biến kẹt là phủ quyết. Một chuỗi phủ 95% mà nửa
    # số điểm là giá trị kẹt thì tệ hơn chuỗi phủ 60% trung thực.
    if len(pts) < MIN_TREND_SAMPLES or coverage < 0.30:
        grade: Grade = "không dùng được"
    elif runs or coverage < 0.60:
        grade = "thấp"
    elif coverage < 0.85 or jitter > cadence:
        grade = "trung bình"
    else:
        grade = "cao"

    return QualityReport(
        samples=len(pts),
        window_days=window_days,
        cadence_minutes=med_gap,
        cadence_jitter_minutes=jitter,
        expected_samples=expected,
        coverage=coverage,
        gaps=len(big),
        longest_gap_hours=max(deltas) / 60.0,
        flatline_runs=runs,
        longest_flatline_hours=longest_flat or None,
        grade=grade,
        reasons=reasons,
    )


# --------------------------------------------------------------------------- #
# 2. Sức khoẻ thiết bị — thứ đáng tiền nhất ở hiện trạng này
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class HealthSample:
    """Lần đọc phần sức khoẻ. Tách khỏi ``Sample`` vì hai truy vấn khác cột nhau."""

    at: datetime
    battery_v: float | None = None
    signal_percent: float | None = None


@dataclass(frozen=True, slots=True)
class BatteryTrend:
    current_v: float | None
    volts_per_day: float | None
    days_to_warn: float | None
    days_to_dead: float | None
    warn_v: float
    dead_v: float
    confidence: str


@dataclass(frozen=True, slots=True)
class SignalTrend:
    current_percent: float | None
    percent_per_day: float | None
    below_floor_ratio: float
    floor_percent: float


@dataclass(frozen=True, slots=True)
class DeviceHealth:
    psn: str
    samples: int
    battery: BatteryTrend
    signal: SignalTrend
    delivery_ratio: float
    delivery_trend_per_day: float | None
    silent_days: float | None
    risk: Risk
    likely_cause: str | None
    days_to_failure: float | None
    reasons: list[str] = field(default_factory=list)


def _battery_trend(
    bat: list[tuple[datetime, float]], warn_v: float, dead_v: float
) -> tuple[BatteryTrend, list[str]]:
    cur = bat[-1][1] if bat else None
    empty = BatteryTrend(cur, None, None, None, warn_v, dead_v, "chưa đủ dữ liệu")
    if len(bat) < MIN_TREND_SAMPLES:
        if bat:
            return empty, [
                f"Pin {cur:.2f} V nhưng chỉ có {len(bat)} lần đo — cần "
                f"{MIN_TREND_SAMPLES} để nói về xu hướng."
            ]
        return empty, []

    base = bat[0][0]
    fit = theil_sen([(_days(base, at), v) for at, v in bat])
    if fit is None or cur is None:
        return empty, []

    slope = fit.slope_per_day
    # Chỉ ngoại suy khi pin đang GIẢM. Độ dốc dương là nhiễu đo hoặc vừa thay pin —
    # ngoại suy nó thành "còn 900 ngày" là con số vô nghĩa mặc áo chính xác.
    d_warn = (cur - warn_v) / -slope if slope < 0 and cur > warn_v else None
    d_dead = (cur - dead_v) / -slope if slope < 0 and cur > dead_v else None
    # Độ tin cậy đọc từ ĐỘ RỘNG DẢI ĐỘ DỐC, không từ số điểm: 500 điểm mà dải trải
    # rộng thì vẫn không kết luận được gì.
    if slope == 0:
        conf = "thấp"
    elif fit.spread < abs(slope) * 0.5:
        conf = "cao"
    elif fit.spread < abs(slope) * 2.0:
        conf = "trung bình"
    else:
        conf = "thấp"

    reasons = [f"Pin {cur:.2f} V, xu hướng {slope * 1000:+.1f} mV/ngày (tin cậy {conf})."]
    if d_warn is not None:
        reasons.append(f"Tới ngưỡng cảnh báo {warn_v:.2f} V sau khoảng {d_warn:.0f} ngày.")
    return BatteryTrend(cur, slope, d_warn, d_dead, warn_v, dead_v, conf), reasons


def assess_device_health(
    samples: list[HealthSample],
    *,
    psn: str,
    now: datetime,
    cadence_minutes: float = DEFAULT_CADENCE_MINUTES,
    warn_v: float = BATTERY_WARN_V,
    dead_v: float = BATTERY_DEAD_V,
    floor_percent: float = SIGNAL_FLOOR_PERCENT,
) -> DeviceHealth:
    """Thiết bị này còn báo được bao lâu nữa, và vì sao nó sẽ chết.

    Đây là hàm trả lời đúng sự cố đang xảy ra. Hai thiết bị pilot chết dần suốt nhiều
    tuần — pin 3.6 V, sóng 15-20%, mẫu rơi dần — và không ai được cảnh báo cho tới
    khi bồn gần cạn. Không thuật toán dự báo nào cứu được một bồn khi cảm biến đã im
    84 ngày, nên cảnh báo TRƯỚC khi cảm biến chết mới là giá trị thật.

    Ba tín hiệu độc lập, cố ý KHÔNG gộp thành một điểm số duy nhất:

    1. Độ dốc pin (Theil-Sen) -> số ngày tới ngưỡng cảnh báo và tới hết tuổi thọ.
    2. Xu hướng sóng + tỉ lệ mẫu dưới sàn -> khả năng upload.
    3. Tỉ lệ mẫu nhận được so với nhịp kỳ vọng, và xu hướng của nó -> dấu hiệu sớm
       nhất, thường xuất hiện trước khi pin kịp tụt tới ngưỡng.

    Gộp ba thứ thành một con số sẽ che mất NGUYÊN NHÂN, mà nguyên nhân quyết định
    mang theo pin hay mang theo ăng-ten khi ra hiện trường.
    """
    pts = sorted(samples, key=lambda s: s.at)
    silent_days = _days(pts[-1].at, now) if pts else None

    bat = [(s.at, float(s.battery_v)) for s in pts if s.battery_v is not None]
    sig = [(s.at, float(s.signal_percent)) for s in pts if s.signal_percent is not None]

    battery, reasons = _battery_trend(bat, warn_v, dead_v)

    sig_slope = None
    if len(sig) >= MIN_TREND_SAMPLES:
        f = theil_sen([(_days(sig[0][0], at), v) for at, v in sig])
        sig_slope = f.slope_per_day if f else None
    below = sum(1 for _, v in sig if v < floor_percent) / len(sig) if sig else 0.0
    signal = SignalTrend(sig[-1][1] if sig else None, sig_slope, below, floor_percent)
    if sig:
        reasons.append(
            f"Sóng {sig[-1][1]:.0f}%"
            + (f", xu hướng {sig_slope:+.2f} %/ngày" if sig_slope is not None else "")
            + (f", {below * 100:.0f}% mẫu dưới sàn {floor_percent:.0f}%" if below else "")
            + "."
        )

    delivery, deliv_trend = 1.0, None
    if len(pts) >= 2:
        span_min = (pts[-1].at - pts[0].at).total_seconds() / 60.0
        expected = max(1, round(span_min / cadence_minutes) + 1)
        delivery = min(1.0, len(pts) / expected)
        # Mẫu số ở đây là khoảng thiết bị CÒN BÁO, cố ý khác `coverage` của
        # assess_quality (đo trên cả cửa sổ). Hai câu hỏi khác nhau: "đường truyền có
        # tốt không" khác "tôi có dữ liệu cho cửa sổ này không". Một thiết bị chết
        # sạch sẽ có delivery 100% và coverage 15% — cả hai đều đúng, nên nhãn phải
        # nói rõ mỗi con số đo cái gì.
        # Xu hướng: chia chuỗi làm hai nửa theo THỜI GIAN (không theo số điểm) và so
        # tỉ lệ. Thô nhưng đọc được, và không cần thêm giả định nào về phân bố khoảng
        # trống. Chia theo số điểm sẽ sai vì nửa sau vốn đã thưa hơn.
        mid = pts[0].at + (pts[-1].at - pts[0].at) / 2
        first = [s for s in pts if s.at <= mid]
        second = [s for s in pts if s.at > mid]
        if len(first) >= 3 and len(second) >= 3:
            exp_half = max(1, round(span_min / 2.0 / cadence_minutes))
            r1 = min(1.0, len(first) / exp_half)
            r2 = min(1.0, len(second) / exp_half)
            half_days = span_min / 2.0 / 1440.0
            deliv_trend = (r2 - r1) / half_days if half_days > 0 else None
        reasons.append(
            f"Nhận được {delivery * 100:.0f}% số mẫu trong khoảng thiết bị còn báo."
        )

    risk, cause, dtf = _rank_risk(
        battery=battery,
        signal=signal,
        delivery=delivery,
        deliv_trend=deliv_trend,
        silent_days=silent_days,
        n=len(pts),
        reasons=reasons,
    )
    return DeviceHealth(
        psn=psn,
        samples=len(pts),
        battery=battery,
        signal=signal,
        delivery_ratio=delivery,
        delivery_trend_per_day=deliv_trend,
        silent_days=silent_days,
        risk=risk,
        likely_cause=cause,
        days_to_failure=dtf,
        reasons=reasons,
    )


def _rank_risk(
    *,
    battery: BatteryTrend,
    signal: SignalTrend,
    delivery: float,
    deliv_trend: float | None,
    silent_days: float | None,
    n: int,
    reasons: list[str],
) -> tuple[Risk, str | None, float | None]:
    """Xếp mức rủi ro. Thiết bị ĐÃ im thì không còn là dự đoán mà là sự thật."""
    if n < MIN_TREND_SAMPLES:
        return "chưa đủ dữ liệu", None, None

    # Im lâu hơn SILENT_DEAD_DAYS: đây là báo cáo hiện trạng, không phải dự báo.
    if silent_days is not None and silent_days > SILENT_DEAD_DAYS:
        cause = "Thiết bị đã ngừng báo"
        if battery.current_v is not None and battery.current_v <= battery.warn_v:
            cause = "Thiết bị đã ngừng báo, pin ở mức thấp lúc đo cuối"
        elif (
            signal.current_percent is not None
            and signal.current_percent <= signal.floor_percent
        ):
            cause = "Thiết bị đã ngừng báo, sóng ở mức thấp lúc đo cuối"
        reasons.append(f"Đã im {silent_days:.1f} ngày — cần ra hiện trường kiểm tra.")
        return "cao", cause, 0.0

    candidates: list[tuple[float, str]] = []
    if battery.days_to_dead is not None:
        candidates.append((battery.days_to_dead, "Pin cạn"))
    # Tỉ lệ nhận mẫu đang tụt: ngoại suy tới 0. Đây thường là dấu hiệu SỚM NHẤT, xuất
    # hiện trước khi pin kịp tụt tới ngưỡng.
    if deliv_trend is not None and deliv_trend < -0.001 and delivery > 0:
        candidates.append((delivery / -deliv_trend, "Mất dần khả năng truyền"))

    dtf, cause = min(candidates) if candidates else (None, None)

    if dtf is not None and dtf <= 14:
        risk: Risk = "cao"
    elif dtf is not None and dtf <= 45:
        risk = "trung bình"
    elif delivery < 0.60 or signal.below_floor_ratio > 0.5:
        risk, cause = "trung bình", cause or "Truyền nhận không ổn định"
    elif battery.current_v is not None and battery.current_v <= battery.warn_v:
        risk, cause = "trung bình", cause or "Pin dưới ngưỡng cảnh báo"
    else:
        risk = "thấp"

    if dtf is not None:
        reasons.append(f"Ước tính còn {dtf:.0f} ngày trước khi mất tín hiệu ({cause}).")
    return risk, cause, dtf


# --------------------------------------------------------------------------- #
# 3. Bất thường — phân đoạn rồi soi phần dư
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Anomaly:
    at: datetime
    kind: AnomalyKind
    value_l: float | None
    expected_l: float | None
    deviation_l: float | None
    z: float | None
    note: str


def change_points(
    samples: list[Sample], *, min_segment_hours: float = 12.0, max_depth: int = 3
) -> list[int]:
    """Phân đoạn nhị phân: tìm chỗ chuỗi đổi CHẾ ĐỘ tiêu thụ.

    Vì sao cần: một đường thẳng khớp cho cả chuỗi sẽ hoà tan mọi thay đổi thật. Bồn
    được nạp thêm, dây chuyền mới chạy, một chỗ rò xuất hiện — cả ba đều là đổi độ
    dốc, và cả ba bị bình quân hoá thành "mức dùng trung bình" vô nghĩa.

    Cắt tại điểm làm giảm nhiều nhất tổng sai số TUYỆT ĐỐI so với một đường thẳng
    duy nhất. Tuyệt đối chứ không bình phương, để một lần nạp (bậc nhảy lớn) không
    tự mình quyết định chỗ cắt.
    """
    pts = [s for s in samples if s.volume_l is not None]
    if len(pts) < 2 * MIN_TREND_SAMPLES:
        return []
    base = pts[0].at
    xy = [(_days(base, s.at), float(s.volume_l or 0.0)) for s in pts]

    def cost(seg: list[tuple[float, float]]) -> float:
        if len(seg) < 3:
            return 0.0
        f = theil_sen(seg)
        if f is None:
            return 0.0
        return sum(abs(y - (f.intercept + f.slope_per_day * x)) for x, y in seg)

    min_days = min_segment_hours / 24.0
    cuts: list[int] = []

    def split(lo: int, hi: int, depth: int) -> None:
        if depth <= 0 or hi - lo < 2 * MIN_TREND_SAMPLES:
            return
        whole = cost(xy[lo:hi])
        best, best_i = 0.0, -1
        for i in range(lo + MIN_TREND_SAMPLES, hi - MIN_TREND_SAMPLES):
            if xy[i][0] - xy[lo][0] < min_days or xy[hi - 1][0] - xy[i][0] < min_days:
                continue
            gain = whole - (cost(xy[lo:i]) + cost(xy[i:hi]))
            if gain > best:
                best, best_i = gain, i
        # Chỉ nhận nếu cắt giảm được trên 15% sai số. Không có sàn này thì thuật toán
        # luôn tìm ra "một chỗ cắt tốt hơn" và băm chuỗi thành vụn.
        if best_i > 0 and best > whole * 0.15:
            cuts.append(best_i)
            split(lo, best_i, depth - 1)
            split(best_i, hi, depth - 1)

    split(0, len(xy), max_depth)
    return sorted(cuts)


def detect_anomalies(
    samples: list[Sample],
    *,
    capacity_l: float | None = None,
    z_threshold: float = ANOMALY_Z,
) -> list[Anomaly]:
    """Bất thường = phần dư so với chế độ tiêu thụ CỦA CHÍNH ĐOẠN ĐÓ.

    Không so với hằng số và cũng không so với cả chuỗi: bồn tiêu thụ 7.400 L/ngày lúc
    chạy và 5 L/ngày lúc nghỉ, nên một ngưỡng cố định sẽ vừa bỏ sót vừa kêu oan. Phân
    đoạn trước, khớp bền trong từng đoạn, rồi chấm điểm phần dư bằng MAD.
    """
    pts = [s for s in samples if s.volume_l is not None]
    if len(pts) < MIN_TREND_SAMPLES:
        return []

    out: list[Anomaly] = []
    bounds = [0, *change_points(pts), len(pts)]
    base = pts[0].at

    for lo, hi in pairwise(bounds):
        seg = pts[lo:hi]
        if len(seg) < MIN_TREND_SAMPLES:
            continue
        xy = [(_days(base, s.at), float(s.volume_l or 0.0)) for s in seg]
        fit = theil_sen(xy)
        if fit is None:
            continue
        resid = [y - (fit.intercept + fit.slope_per_day * x) for x, y in xy]
        # Sàn thang bằng nhiễu cảm biến. KHÔNG bỏ qua khi MAD = 0: một chuỗi sạch với
        # đúng một điểm rác có MAD phần dư bằng 0 (240 phần dư bằng 0, một cái -900 L
        # -> trung vị của |lệch| vẫn là 0), nên chia cho MAD sẽ bỏ qua cả đoạn. Đó là
        # mù đúng lúc dữ liệu sạch nhất — bug đã bị test chuỗi tuyến tính bắt được.
        # Dùng lại noise_floor_l vì đây là câu hỏi vật lý, không phải thống kê: dưới
        # độ phân giải cảm biến thì không có gì để nói, trên nó thì lệch là thật.
        scale = max(mad(resid) * MAD_TO_SIGMA, noise_floor_l(capacity_l))
        for s, r in zip(seg, resid, strict=False):
            z = r / scale
            if abs(z) < z_threshold:
                continue
            note = f"Lệch {abs(r):.0f} L so với xu hướng của đoạn ({abs(z):.1f} sigma bền)."
            if z < 0 and capacity_l:
                note += " Nếu không phải lần xuất bất thường thì cần kiểm tra rò rỉ."
            out.append(
                Anomaly(
                    at=s.at,
                    kind="tăng bất thường" if z > 0 else "sụt bất thường",
                    value_l=s.volume_l,
                    expected_l=float(s.volume_l or 0.0) - r,
                    deviation_l=r,
                    z=z,
                    note=note,
                )
            )

    # Cảm biến kẹt bắt RIÊNG: phần dư của một chuỗi kẹt lại rất NHỎ — nó khớp đường
    # thẳng gần hoàn hảo. Đây đúng là ca mà phương pháp phần dư mù hoàn toàn.
    i = 0
    while i < len(pts) - 1:
        j = i
        while j + 1 < len(pts) and pts[j + 1].volume_l == pts[i].volume_l:
            j += 1
        if j > i:
            hours = (pts[j].at - pts[i].at).total_seconds() / 3600.0
            if hours >= FLATLINE_HOURS:
                out.append(
                    Anomaly(
                        at=pts[i].at,
                        kind="cảm biến kẹt",
                        value_l=pts[i].volume_l,
                        expected_l=None,
                        deviation_l=None,
                        z=None,
                        note=(
                            f"Giá trị không đổi suốt {hours:.1f} giờ "
                            f"({j - i + 1} lần đo). Bồn thật luôn bay hơi nên mức phải "
                            "nhích xuống — nghi cảm biến kẹt."
                        ),
                    )
                )
        i = max(j, i + 1)

    return sorted(out, key=lambda a: a.at)
