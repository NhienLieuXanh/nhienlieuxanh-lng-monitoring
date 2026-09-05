"""Suy trạng thái online/offline. Hàm thuần, không đọc clock.

Nhận `now` làm tham số thay vì gọi datetime.now() bên trong: test kiểm được
đúng biên (ngưỡng -1s / +1s / chính xác / None) mà không cần mock clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.domain.contracts import TerminalStatus

# Bao lâu một lần quan sát còn được coi là bằng chứng về thiết bị. Quá mốc này
# thì ta không biết gì về hiện tại nữa và phải quay về đo bằng `now`.
#
# 720 phút = đúng ngưỡng ``ingest_stale_after_minutes``, và nó được chọn từ số
# đo chứ không phải đoán: khoảng cách thật giữa hai lần poller chạy có max 689
# phút (106 lần chạy, 24/08–05/09/2026). Trên mốc đó thì bản thân poller mới là
# thứ hỏng, và /api/health đã báo degraded — hai tín hiệu phải nói cùng một điều.
DEFAULT_TRUST_WINDOW = timedelta(minutes=720)


def status_cutoff(
    now: datetime,
    stale_after: timedelta,
    observed_at: datetime | None,
    trust_window: timedelta = DEFAULT_TRUST_WINDOW,
) -> datetime:
    """Mốc thời gian mà ``last_seen_at`` phải mới hơn thì thiết bị mới là online.

    Rút ra thành hàm riêng vì có HAI đường tính trạng thái: Python (``derive_status``,
    dùng ở tầng API) và SQL (``counts_by_status``, ``refresh_status_cache``). Hai
    đường tính cùng một thứ theo hai cách là đúng cái đã đẻ ra bug health khai
    ``terminals_online: 1`` trong khi dashboard khai offline. Cả hai giờ gọi hàm
    này và chỉ so ``last_seen_at >= cutoff``, nên không có chỗ nào để lệch.
    """
    tin_duoc = observed_at is not None and (now - observed_at) <= trust_window
    goc = observed_at if (tin_duoc and observed_at is not None) else now
    return goc - stale_after


def derive_status(
    last_seen_at: datetime | None,
    now: datetime,
    stale_after: timedelta,
    *,
    observed_at: datetime | None,
    trust_window: timedelta = DEFAULT_TRUST_WINDOW,
) -> TerminalStatus:
    """Thiết bị có đang báo hay không — đo bằng LẦN TA NHÌN, không bằng `now`.

    ``observed_at`` là thời điểm lần nạp thành công gần nhất, tức lần cuối cùng ta
    thực sự hỏi nguồn. Nó là tham số BẮT BUỘC (keyword-only, không có mặc định) để
    không call site nào lặng lẽ quên nó rồi rơi về hành vi cũ.

    Vì sao không so với ``now``: trên serverless không có scheduler, nhịp lấy dữ
    liệu do một poller bên ngoài quyết định và nó trễ rất nặng — đo được min 26 /
    trung vị 89 / max 689 phút (106 lần chạy theo lịch, 24/08–05/09/2026). Nhà máy
    phát mỗi PHÚT, nhưng ta chỉ *lấy về* vài lần một ngày. So ``last_seen_at`` với
    ``now`` là đo độ trễ của CHÍNH TA rồi ghi tội cho thiết bị: với ngưỡng 90 phút,
    bồn hiện "Ngoại tuyến" 55% thời gian trong khi nó chưa ngừng báo phút nào.

    Điều ta thật sự biết: tại ``observed_at`` ta đã hỏi nguồn, và bản ghi mới nhất
    nó đưa ra là ``last_seen_at``. Hai mốc cách nhau quá ngưỡng thì thiết bị đã im
    **vào lúc ta nhìn** — đó mới là ngoại tuyến thật.

    Nhưng bằng chứng cũng hết hạn. Nếu chính poller chết, ``observed_at`` đứng yên
    và bồn sẽ khai "Trực tuyến" vĩnh viễn dựa trên một lần nhìn từ hôm kia — đúng
    loại tín hiệu dối mà dự án này đang dọn. Nên quá ``trust_window`` thì bỏ bằng
    chứng cũ và quay về ``now``: mọi bồn chuyển offline, vì lúc đó ta thật sự
    không biết gì, và "không biết" phải hiện ra chứ không được im lặng.
    """
    if last_seen_at is None:
        return TerminalStatus.OFFLINE
    cutoff = status_cutoff(now, stale_after, observed_at, trust_window)
    return (
        TerminalStatus.ONLINE
        if last_seen_at >= cutoff
        else TerminalStatus.OFFLINE
    )
