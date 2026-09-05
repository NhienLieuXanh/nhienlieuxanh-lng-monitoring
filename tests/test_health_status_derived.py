"""/api/health phải SUY trạng thái, không đọc cột cache.

Bug thật, đo trên production 05/09/2026 13:38 ICT: ``/api/health`` khai
``terminals_online: 1`` trong khi ``last_ingest_at`` là 10:00 ICT — bồn đó đã im
3,6 giờ, quá ngưỡng 90 phút, và mọi endpoint khác (đều suy lại lúc đọc) khai nó
offline. Cột ``terminals.status`` chỉ được đồng bộ bên trong cycle ingest, nên
giữa hai lần poll nó luôn kể chuyện cũ — và trên serverless khoảng cách giữa hai
lần poll có trung vị ~90 phút, max ~11 giờ.

Chính docstring của ``refresh_status_cache`` đã đặt bất biến "API thì suy lại lúc
đọc nên không bị". Health là chỗ duy nhất phá bất biến đó.

Các test dựng đúng cái kẽ: cache nói 'online', dữ liệu nói đã im quá lâu.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.db.models import Terminal

pytestmark = pytest.mark.db

NGUONG = timedelta(minutes=90)  # = Settings.online_stale_minutes mặc định


def _bon_cache_noi_online(session: Session, psn: str, im_bao_lau: timedelta) -> None:
    """Terminal có cột status='online' nhưng last_seen_at đã cũ ``im_bao_lau``.

    Đây KHÔNG phải trạng thái nhân tạo: nó là trạng thái bình thường của mọi bồn
    trong khoảng giữa hai lần ingest, vì không có gì hạ cột status xuống ngoài
    cycle ingest kế tiếp.
    """
    session.add(
        Terminal(
            psn=psn,
            name=psn,
            status="online",
            last_seen_at=datetime.now(tz=UTC) - im_bao_lau,
        )
    )
    session.flush()


def _health(session: Session) -> Any:
    from app.api.routers.ops import health
    from app.config import get_settings

    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    resp = SimpleNamespace(status_code=200)
    return health(req, resp, session, get_settings())  # type: ignore[arg-type]


def test_health_khong_khai_online_cho_bon_da_im_qua_nguong(session: Session) -> None:
    _bon_cache_noi_online(session, "IM-LANG-01", NGUONG + timedelta(minutes=30))

    out = _health(session)
    assert out.terminals_offline == 1
    assert out.terminals_online == 0, (
        "health đang đọc cột status (cache) thay vì suy từ last_seen_at — "
        "nó khai online trong khi /api/terminals khai offline cùng lúc"
    )


def test_health_va_summary_khong_duoc_noi_nguoc_nhau(session: Session) -> None:
    """Cùng thiết bị, cùng thời điểm, hai endpoint phải cho cùng câu trả lời."""
    from app.api.routers.ops import summary
    from app.config import get_settings

    _bon_cache_noi_online(session, "IM-LANG-02", NGUONG + timedelta(hours=3))

    h = _health(session)
    s = summary(session, get_settings(), None)  # type: ignore[arg-type]
    assert (h.terminals_online, h.terminals_offline) == (s.online, s.offline), (
        f"health nói {h.terminals_online}/{h.terminals_offline} "
        f"nhưng summary nói {s.online}/{s.offline}"
    )


def test_bon_vua_bao_van_la_online(session: Session) -> None:
    """Phía kia của biên: suy lại không được biến bồn đang sống thành offline."""
    _bon_cache_noi_online(session, "CON-SONG-01", timedelta(minutes=1))

    out = _health(session)
    assert out.terminals_online == 1
    assert out.terminals_offline == 0


def test_bon_chua_bao_lan_nao_la_offline(session: Session) -> None:
    session.add(Terminal(psn="CHUA-BAO-01", name="CHUA-BAO-01", status="online"))
    session.flush()

    out = _health(session)
    assert out.terminals_online == 0
    assert out.terminals_offline == 1


# ------------------------------------------------- SQL và Python phải cùng luật


def test_duong_SQL_va_duong_PYTHON_cho_cung_ket_qua(session: Session) -> None:
    """health (SQL) và /api/terminals (Python) phải không thể lệch nhau.

    Hai đường tính cùng một thứ theo hai cách chính là hình dạng của bug đã sửa
    ở trên. Giờ cả hai gọi chung ``status_cutoff``; test này khoá điều đó lại,
    quét qua nhiều độ trễ khác nhau quanh ngưỡng.
    """
    from app.api.deps import to_terminal_out
    from app.repositories import terminals as term_repo

    now = datetime.now(tz=UTC)
    nap_luc = now - timedelta(hours=3)

    for i, im in enumerate(
        [
            timedelta(minutes=1),
            NGUONG - timedelta(minutes=1),
            NGUONG + timedelta(minutes=1),
            timedelta(hours=5),
            timedelta(days=40),
        ]
    ):
        psn = f"SS-{i:02d}"
        session.add(Terminal(psn=psn, name=psn, status="online", last_seen_at=now - im))
    session.flush()

    sql = term_repo.counts_by_status(session, now, NGUONG, observed_at=nap_luc)
    terms = term_repo.list_all(session)
    py = {"online": 0, "offline": 0}
    for t in terms:
        out = to_terminal_out(
            t, None, now=now, stale_after=NGUONG, observed_at=nap_luc
        )
        py[out.status] += 1

    assert sql.get("online", 0) == py["online"], f"SQL={sql} Python={py}"
    assert sql.get("offline", 0) == py["offline"], f"SQL={sql} Python={py}"


def test_health_khong_bao_ngoai_tuyen_khi_chi_la_poller_tre(session: Session) -> None:
    """Bồn báo tới 09:59, ta nạp lúc 10:00, bây giờ 13:38 — thiết bị vẫn sống."""
    from app.db.models import IngestRun

    now = datetime.now(tz=UTC)
    nap_luc = now - timedelta(hours=3, minutes=38)
    session.add(
        Terminal(
            psn="TRE-POLLER",
            name="TRE-POLLER",
            status="offline",
            last_seen_at=nap_luc - timedelta(minutes=1),
        )
    )
    session.add(
        IngestRun(trigger="cli", status="success", started_at=nap_luc, finished_at=nap_luc)
    )
    session.flush()

    out = _health(session)
    assert out.terminals_online == 1, (
        "health vẫn quy độ trễ của poller thành thiết bị chết"
    )
    assert out.terminals_offline == 0
