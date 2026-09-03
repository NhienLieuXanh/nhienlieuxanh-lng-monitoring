"""IngestionService qua FakeAdapter. Cần PostgreSQL thật."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from stub_adapter import DEMO_GPS, DEMO_PSNS, FakeAdapter

from app.domain.alerts import fill_percent
from app.repositories import telemetry as tel_repo
from app.repositories import terminals as term_repo
from app.services.ingestion import IngestionService, IngestStats

UTC = ZoneInfo("UTC")


def _newest_demo_day(fake: FakeAdapter, psn: str) -> date:
    """Ngày MỚI NHẤT của một PSN — ngày duy nhất có toạ độ.

    ``_synthesize`` của fake cố ý chỉ gắn toạ độ vào bản đọc mới nhất của CẢ
    chuỗi, còn ``fetch_telemetry`` lọc theo ngày lịch giờ vendor. Nên một ngày cũ
    hơn vẫn hợp lệ mà KHÔNG có toạ độ nào. Test nào nói về GPS mà lấy
    ``demo_days()[0]`` là đang chọn đúng ngày không có GPS: hoặc đỏ oan, hoặc —
    tệ hơn — xanh rỗng vì chẳng có gì để ghi đè.
    """
    return max(d for p, d in fake.demo_days() if p == psn)


@pytest.mark.db
def test_ingest_is_idempotent(session, session_factory, settings):
    now = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    fake = FakeAdapter(days=1, fresh=True, now=now)
    svc = IngestionService(fake, session_factory, settings, psns=list(DEMO_PSNS))
    psn, day = fake.demo_days()[0]

    first = IngestStats()
    svc.ingest_psn_day(psn, day, first)
    assert first.inserted > 0
    assert first.errors == []

    second = IngestStats()
    svc.ingest_psn_day(psn, day, second)
    assert second.inserted == 0
    assert second.duplicates == first.inserted


@pytest.mark.db
def test_zero_rows_is_not_an_error(session, session_factory, settings):
    fake = FakeAdapter(return_empty=True)
    svc = IngestionService(fake, session_factory, settings, psns=list(DEMO_PSNS))
    stats = IngestStats()
    svc.ingest_psn_day(DEMO_PSNS[0], datetime(2026, 7, 23, tzinfo=UTC).date(), stats)
    assert stats.errors == []
    assert DEMO_PSNS[0] in stats.psns_no_data
    assert stats.inserted == 0


@pytest.mark.db
def test_operator_name_survives_resync(session, session_factory, settings):
    fake = FakeAdapter()
    svc = IngestionService(fake, session_factory, settings, psns=list(DEMO_PSNS))
    stats = IngestStats()
    svc.sync_terminals(stats, list(DEMO_PSNS))
    psn = DEMO_PSNS[0]
    # Fixture session đã join outer transaction: flush trần để lại implicit
    # transaction, lần begin() sau của service sẽ fail. Bọc begin() cho khớp
    # đường ghi của IngestionService.
    with session.begin():
        term_repo.update_operator(session, psn, name="Bồn A - Kho Long An")

    svc.sync_terminals(IngestStats(), list(DEMO_PSNS))
    term = term_repo.get_by_psn(session, psn)
    assert term is not None
    assert term.name == "Bồn A - Kho Long An"


@pytest.mark.db
def test_vendor_gps_dien_vao_o_dang_trong(session, session_factory, settings):
    """Bồn chưa ghim vị trí thì GPS của module tự điền vào — không phải nhập tay.

    Đây là lý do đường tự động tồn tại: vendor CÓ trả toạ độ (đã xác minh trên
    2604200016 ngày 2026-07-23), chỉ là không phải ngày nào cũng có.
    """
    now = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    fake = FakeAdapter(days=1, fresh=True, now=now)
    svc = IngestionService(fake, session_factory, settings, psns=list(DEMO_PSNS))
    psn = DEMO_PSNS[0]
    svc.ingest_psn_day(psn, _newest_demo_day(fake, psn), IngestStats())

    term = term_repo.get_by_psn(session, psn)
    assert term is not None
    assert term.latitude == DEMO_GPS[0]
    assert term.longitude == DEMO_GPS[1]


@pytest.mark.db
def test_ghim_tay_song_sot_qua_ingest(session, session_factory, settings):
    """Người vận hành ghim đúng nhà kho thì vòng ingest sau KHÔNG được kéo về.

    Cùng luật với ``name`` và ``capacity_l``: ingest chỉ COALESCE vào chỗ NULL.
    Nếu ghi đè vô điều kiện thì (a) mất vị trí chính xác người ta tự đo, và (b) một
    ngày module mất định vị sẽ XOÁ luôn toạ độ đang đúng.
    """
    now = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    fake = FakeAdapter(days=1, fresh=True, now=now)
    svc = IngestionService(fake, session_factory, settings, psns=list(DEMO_PSNS))
    # Ngày MỚI NHẤT, không phải ngày cũ nhất: vòng ingest thứ hai phải THẬT SỰ
    # mang toạ độ về, nếu không thì luật COALESCE chẳng bị thử và bài này xanh
    # rỗng — nó sẽ pass y nguyên kể cả khi ai đó bỏ COALESCE và ghi đè vô điều kiện.
    psn = DEMO_PSNS[0]
    day = _newest_demo_day(fake, psn)
    svc.ingest_psn_day(psn, day, IngestStats())

    pinned = (Decimal("10.800000"), Decimal("106.600000"))
    with session.begin():
        term_repo.update_operator(
            session,
            psn,
            location_sent=True,
            latitude=pinned[0],
            longitude=pinned[1],
        )

    svc.ingest_psn_day(psn, day, IngestStats())
    term = term_repo.get_by_psn(session, psn)
    assert term is not None
    assert (term.latitude, term.longitude) == pinned


@pytest.mark.db
def test_fill_percent_matches_ingested_row(session, session_factory, settings):
    now = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    fake = FakeAdapter(days=1, fresh=True, now=now)
    svc = IngestionService(fake, session_factory, settings, psns=[DEMO_PSNS[0]])
    psn, day = next(p for p in fake.demo_days() if p[0] == DEMO_PSNS[0])
    svc.ingest_psn_day(psn, day, IngestStats())
    term = term_repo.get_by_psn(session, psn)
    latest = tel_repo.latest_for(session, psn)
    assert term is not None and latest is not None
    derived = fill_percent(latest.volume_l, term.capacity_l)
    assert derived is not None
    assert latest.volume_percent is not None
    assert abs(derived - latest.volume_percent) < 0.02
