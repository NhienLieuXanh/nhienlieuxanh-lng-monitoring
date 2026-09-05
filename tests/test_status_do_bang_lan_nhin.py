"""Trạng thái đo bằng LẦN TA NHÌN, không bằng `now`.

Bug thật: bồn YKH-TANK-01 hiện "Ngoại tuyến" 55% thời gian trong khi nhà máy phát
đều mỗi PHÚT. Nguyên nhân không nằm ở thiết bị mà ở nhịp CHÚNG TA đi lấy: poller
là GitHub Actions đặt `*/30`, nhưng runner miễn phí trễ nặng — đo 106 lần chạy
theo lịch (24/08–05/09/2026) được min 26 / trung vị 89 / max 689 phút. Ngưỡng 90
phút nằm ngay trên trung vị, nên đúng nghĩa tung đồng xu.

Các test dưới đây khoá cả hai phía: không được buộc tội thiết bị vì độ trễ của
chính mình, và cũng không được tin một lần nhìn đã quá cũ.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.domain.contracts import TerminalStatus
from app.domain.status import DEFAULT_TRUST_WINDOW, derive_status

NGUONG = timedelta(minutes=90)
NOW = datetime(2026, 9, 5, 13, 38, tzinfo=UTC)


def test_poller_tre_3_gio_KHONG_bien_bon_dang_song_thanh_ngoai_tuyen() -> None:
    """Đúng tình huống trong ảnh người dùng gửi.

    10:00 ta nạp, và bản ghi mới nhất nguồn đưa ra là 09:59 — thiết bị đang sống.
    13:38 poller vẫn chưa chạy lại. Ta không có bằng chứng nào nói nó đã chết.
    """
    nap_luc = NOW - timedelta(hours=3, minutes=38)
    do_luc = nap_luc - timedelta(minutes=1)
    assert (
        derive_status(do_luc, NOW, NGUONG, observed_at=nap_luc)
        is TerminalStatus.ONLINE
    )


def test_cach_cu_do_bang_now_thi_bao_ngoai_tuyen_oan() -> None:
    """Giữ lại đối chứng: bỏ bằng chứng đi thì đúng là ra kết luận sai."""
    nap_luc = NOW - timedelta(hours=3, minutes=38)
    do_luc = nap_luc - timedelta(minutes=1)
    assert (
        derive_status(do_luc, NOW, NGUONG, observed_at=None) is TerminalStatus.OFFLINE
    )


def test_thiet_bi_im_that_van_bi_bat() -> None:
    """Lúc ta nhìn, bản ghi mới nhất đã cũ hơn ngưỡng — đó mới là ngoại tuyến thật."""
    nap_luc = NOW - timedelta(minutes=10)
    do_luc = nap_luc - timedelta(minutes=91)
    assert (
        derive_status(do_luc, NOW, NGUONG, observed_at=nap_luc)
        is TerminalStatus.OFFLINE
    )


def test_bang_chung_qua_han_thi_khong_duoc_tin_nua() -> None:
    """Chốt chặn quan trọng nhất: poller chết không được thành "mọi bồn đều tốt".

    Nếu cứ tin ``observed_at`` mãi thì một poller chết ba ngày vẫn cho bồn khai
    "Trực tuyến" dựa trên một lần nhìn từ hôm kia — đúng loại tín hiệu dối mà dự
    án này đang dọn. Quá trust_window thì quay về đo bằng `now`.
    """
    nap_luc = NOW - DEFAULT_TRUST_WINDOW - timedelta(minutes=1)
    do_luc = nap_luc - timedelta(minutes=1)  # lúc đó thiết bị đang sống
    assert (
        derive_status(do_luc, NOW, NGUONG, observed_at=nap_luc)
        is TerminalStatus.OFFLINE
    ), "bằng chứng đã quá hạn mà vẫn được dùng để khai online"


def test_ngay_truoc_han_tin_thi_van_con_tin() -> None:
    nap_luc = NOW - DEFAULT_TRUST_WINDOW + timedelta(minutes=1)
    do_luc = nap_luc - timedelta(minutes=1)
    assert (
        derive_status(do_luc, NOW, NGUONG, observed_at=nap_luc)
        is TerminalStatus.ONLINE
    )


def test_ban_ghi_moi_hon_ca_luc_nap_khong_lam_am_hieu_thoi_gian() -> None:
    """Đồng hồ nguồn có thể chạy trước: bản ghi mới hơn cả lúc ta nạp xong.

    Nếu tính observed_at - last_seen_at thẳng thì ra số âm; phải là online, không
    được rơi vào nhánh nào lạ.
    """
    nap_luc = NOW - timedelta(minutes=5)
    do_luc = nap_luc + timedelta(minutes=2)
    assert (
        derive_status(do_luc, NOW, NGUONG, observed_at=nap_luc)
        is TerminalStatus.ONLINE
    )


def test_chua_tung_bao_van_la_ngoai_tuyen() -> None:
    assert (
        derive_status(None, NOW, NGUONG, observed_at=NOW) is TerminalStatus.OFFLINE
    )


def test_bien_dung_nguong() -> None:
    nap_luc = NOW - timedelta(minutes=10)
    assert (
        derive_status(nap_luc - NGUONG, NOW, NGUONG, observed_at=nap_luc)
        is TerminalStatus.ONLINE
    ), "đúng bằng ngưỡng thì vẫn online"
    assert (
        derive_status(
            nap_luc - NGUONG - timedelta(seconds=1), NOW, NGUONG, observed_at=nap_luc
        )
        is TerminalStatus.OFFLINE
    )
