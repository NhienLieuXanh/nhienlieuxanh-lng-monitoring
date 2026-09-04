"""Endpoint chuỗi cho biểu đồ, trên PostgreSQL thật.

Bài quan trọng nhất là ``test_giu_phan_moi_nhat_khong_phai_cu_nhat``: dashboard
trước đây vẽ bằng ``/api/telemetry/{psn}?limit=500&order=asc`` — endpoint PHÂN
TRANG — nên nó nhận 500 dòng CŨ NHẤT. Với nguồn 30 phút, 500 điểm là 10 ngày nên
lỗi không lộ; với nguồn 1 phút (nhà máy Yokohama), 500 điểm là 8 giờ 20 và biểu
đồ dừng giữa ngày, không bao giờ chạm giá trị hiện tại. Đo được trên production:
trục x đi 00:00 -> 07:42 trong khi thể tích hiện tại là 16,19 m³ và biểu đồ dừng
ở 19,0.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from app.api.deps import HistoryQuery
from app.api.routers.telemetry import series
from app.db.models import Telemetry, Terminal

UTC = ZoneInfo("UTC")
PSN = "SERIES-TEST-01"
NOW = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)

pytestmark = pytest.mark.db


def _seed_minute_data(session: Session, minutes: int) -> None:
    """Nguồn nhịp 1 PHÚT — chính là nhịp làm lộ ra lỗi."""
    term = Terminal(psn=PSN, name="bồn test", capacity_l=Decimal("60000"))
    session.add(term)
    session.flush()
    for i in range(minutes):
        at = NOW - timedelta(minutes=minutes - 1 - i)
        session.add(
            Telemetry(
                terminal_id=term.id,
                psn=PSN,
                sampled_at=at,
                # Thể tích GIẢM đều: giá trị cuối là nhỏ nhất, nên "giữ phần mới
                # nhất" và "giữ phần cũ nhất" cho hai kết quả không thể lẫn.
                volume_l=Decimal(str(20000 - i)),
                pressure_mpa=Decimal("0.374"),
                source="tst",
                raw_payload={},
            )
        )
    session.flush()


def _q(*, hours: float, limit: int = 1000) -> HistoryQuery:
    return HistoryQuery(
        from_=NOW - timedelta(hours=hours), to=NOW, page=1, limit=limit, order="asc"
    )


def test_giu_phan_moi_nhat_khong_phai_cu_nhat(session: Session) -> None:
    _seed_minute_data(session, 1000)
    # limit nhỏ hơn số dòng -> phải cắt phần CŨ, giữ phần MỚI.
    out = series(PSN, session, _q(hours=24, limit=100), None, None)  # type: ignore[arg-type]
    assert len(out) == 100
    assert out[0].at < out[-1].at, "trả về tăng dần theo thời gian"
    assert out[-1].at == NOW, "điểm cuối phải là bản đo MỚI NHẤT"
    # Với dữ liệu giảm đều, phần mới nhất là phần có thể tích NHỎ nhất.
    assert out[-1].volume_l == pytest.approx(20000 - 999)


def test_bucket_phu_het_cua_so_voi_nguon_1_phut(session: Session) -> None:
    """1440 điểm/ngày không vẽ được trong 480 nhãn — bucket là thứ giải quyết."""
    _seed_minute_data(session, 1440)
    no_bucket = series(PSN, session, _q(hours=24, limit=480), None, None)  # type: ignore[arg-type]
    bucketed = series(PSN, session, _q(hours=24, limit=480), None, 3)  # type: ignore[arg-type]

    span = lambda rows: (rows[-1].at - rows[0].at).total_seconds() / 3600.0  # noqa: E731
    # Không bucket: 480 điểm cuối = 8 giờ. Có bucket 3 phút: 480 điểm = 24 giờ.
    assert span(no_bucket) == pytest.approx(8.0, abs=0.1)
    assert span(bucketed) >= 23.0, "bucket phải phủ hết cửa sổ đã chọn"
    assert len(bucketed) <= 480


def test_bucket_lay_ban_doc_moi_nhat_trong_moi_bucket(session: Session) -> None:
    _seed_minute_data(session, 60)
    out = series(PSN, session, _q(hours=2), None, 10)  # type: ignore[arg-type]
    assert out[-1].at == NOW
    assert out[-1].volume_l == pytest.approx(20000 - 59)


def test_psn_la_dieu_kien_404(session: Session) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        series("KHONG-CO", session, _q(hours=24), None, None)  # type: ignore[arg-type]
    assert ei.value.status_code == 404


def test_khong_phat_raw_payload(session: Session) -> None:
    """Schema mỏng CỐ Ý: biểu đồ cần ba cột, không phải raw_payload của vendor."""
    _seed_minute_data(session, 5)
    out = series(PSN, session, _q(hours=1), None, None)  # type: ignore[arg-type]
    keys = set(out[0].model_dump())
    assert keys == {"at", "volume_l", "pressure_mpa"}
