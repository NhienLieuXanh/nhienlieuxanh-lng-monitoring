"""Báo động của NGUỒN phải hiện ở header, báo cáo trình ký, và email.

Ba chỗ đó cùng im lặng trước khi có file này. Soát ngày 2026-09-04 trên dữ liệu
nhà máy thật:

    stats.summary.alert = 0     /api/alerts = []
    báo cáo mục "Cảnh báo đang mở": "Không có cảnh báo nào đang mở."
    email: cùng hàm với mục trên, nên cũng không nói gì

trong khi nguồn có 6 việc mở trên 5 thiết bị (PS1, PS2, SV2, SV3, SV4), 289 dòng
thô. Báo cáo trình ký là tài liệu đem đi ký, nên một khoảng trống ở đó là một lời
nói sai có chữ ký bên dưới.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from app.domain.contracts import NormalizedAlarm
from app.repositories import vendor_alarms as repo

UTC = ZoneInfo("UTC")
SITE = "YKH"
NOW = datetime.now(tz=UTC)

pytestmark = pytest.mark.db


def _alarm(device: str, message: str, at: datetime) -> NormalizedAlarm:
    return NormalizedAlarm(
        source="ykh",
        site_code=SITE,
        device_id=device,
        raised_at=at,
        vendor_ts_raw=at.strftime("%d/%m/%Y %H:%M:%S"),
        message=message,
        symbol="danger",
    )


def _seed(session: Session, plan: list[tuple[str, str, int]]) -> int:
    rows: list[NormalizedAlarm] = []
    for device, msg, n in plan:
        for i in range(n):
            rows.append(_alarm(device, msg, NOW - timedelta(minutes=i + 1)))
    inserted, _ = repo.bulk_insert(session, rows)
    session.flush()
    return inserted


REAL = [
    ("SV4", "Van SV4: Lỗi đóng van SV4", 70),
    ("SV3", "Van SV3: Lỗi đóng van SV3", 70),
    ("SV2", "Van SV2: Lỗi đóng van SV2", 70),
    ("PS1", "Áp suất PS1: Áp suất dưới ngưỡng cài đặt L: 0.0 bar", 35),
    ("PS2", "Áp suất PS2: Áp suất dưới ngưỡng cài đặt L: 0.0 bar", 43),
]


# ---------------------------------------------------------------- header


def test_summary_dem_viec_bao_dong_cua_nguon(session: Session) -> None:
    from app.api.routers.ops import summary
    from app.config import get_settings

    raw = _seed(session, REAL)
    out = summary(session, get_settings(), None)  # type: ignore[arg-type]
    assert raw == 288
    assert out.vendor_alarms == 5, "5 việc, không phải 288 dòng"


def test_bao_dong_nguon_KHONG_cong_vao_canh_bao_platform(session: Session) -> None:
    """Hai con số phải TÁCH nhau: hai việc khác nhau, hai người phải gọi.

    ``alert`` là cảnh báo hệ thống tự suy (ngoại tuyến / sắp cạn / pin / sóng) —
    việc của người quản lý tài sản. Báo động nguồn là sự cố thiết bị tại nhà máy —
    việc của người vận hành hiện trường.
    """
    from app.api.routers.ops import summary
    from app.config import get_settings

    _seed(session, REAL)
    out = summary(session, get_settings(), None)  # type: ignore[arg-type]
    assert out.vendor_alarms == 5
    assert out.alert == 0, "không có bồn nào nên không có cảnh báo platform"


def test_khong_co_bao_dong_thi_dem_bang_0(session: Session) -> None:
    from app.api.routers.ops import summary
    from app.config import get_settings

    out = summary(session, get_settings(), None)  # type: ignore[arg-type]
    assert out.vendor_alarms == 0


# ---------------------------------------------------------------- email


def _cfg():
    from app.config import get_settings
    from app.services.appconfig import EffectiveConfig

    s = get_settings()
    return EffectiveConfig.from_settings(s) if hasattr(
        EffectiveConfig, "from_settings"
    ) else s


def test_email_co_notice_cho_tung_viec(session: Session) -> None:
    from app.services.notifier import VENDOR_CODE_PREFIX, collect_notices

    _seed(session, REAL)
    notices = collect_notices(session, _cfg(), NOW)
    ven = [n for n in notices if n.code.startswith(VENDOR_CODE_PREFIX)]
    assert len(ven) == 5, "một notice cho MỖI việc, không phải một cho tất cả"
    assert len({n.code for n in ven}) == 5, (
        "mã phải KHÁC nhau: cửa chặn gửi lại khoá theo (psn, code), nên mã dùng "
        "chung sẽ làm việc thứ hai bị chặn im lặng sau việc thứ nhất"
    )
    assert all(len(n.code) <= 32 for n in ven), "trần cột notifications.code"
    assert all(n.severity == "warning" for n in ven), (
        "nguồn gắn cùng một mức cho mọi dòng nên xếp nặng nhẹ ở đây là bịa"
    )
    assert any("SV4" in n.message and "70 lần" in n.message for n in ven)


def test_email_co_tran_so_viec(session: Session) -> None:
    """Một nhà máy nhiều thiết bị chập chờn không được biến email thành bức tường."""
    from app.services.notifier import (
        VENDOR_CODE_PREFIX,
        VENDOR_MAX_NOTICES,
        collect_notices,
    )

    plan = [(f"SV{i}", f"Van SV{i}: loi {i}", 3) for i in range(VENDOR_MAX_NOTICES + 4)]
    _seed(session, plan)
    notices = collect_notices(session, _cfg(), NOW)
    ven = [n for n in notices if n.code.startswith(VENDOR_CODE_PREFIX)]
    assert len(ven) == VENDOR_MAX_NOTICES + 1, "trần cộng MỘT dòng nói còn bao nhiêu"
    overflow = [n for n in ven if n.code.endswith("OVERFLOW")]
    assert len(overflow) == 1
    assert "4 việc báo động nữa" in overflow[0].message


# ---------------------------------------------------------------- báo cáo


def test_bao_cao_trinh_ky_co_muc_bao_dong_nha_may(session: Session) -> None:
    from app.api.routers import report as rp
    from app.config import get_settings

    _seed(session, REAL)
    body = rp.export_report(
        session=session,
        settings=get_settings(),
        user="audit",
        psn=None,
        window_days=7,
    ).body.decode("utf-8")

    assert "Báo động của nhà máy" in body
    assert "Không có cảnh báo nào đang mở" in body, (
        "mục 6 (cảnh báo platform) vẫn đúng là rỗng — mục mới KHÔNG thay nó"
    )
    for dev in ("SV4", "SV3", "SV2", "PS1", "PS2"):
        assert dev in body, f"thiếu thiết bị {dev}"
    assert "288 dòng thô gộp thành 5 việc" in body
    # thứ tự mục phải liền mạch
    assert "7.</span>Báo động của nhà máy" in body
    assert "8.</span>Ghi chú phương pháp" in body
