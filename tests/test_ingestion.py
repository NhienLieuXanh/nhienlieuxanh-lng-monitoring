"""IngestionService qua FakeAdapter. Cần PostgreSQL thật."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from stub_adapter import DEMO_PSNS, FakeAdapter

from app.domain.alerts import fill_percent
from app.repositories import telemetry as tel_repo
from app.repositories import terminals as term_repo
from app.services.ingestion import IngestionService, IngestStats

UTC = ZoneInfo("UTC")


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
