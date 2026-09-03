"""Định tuyến PSN → port. Không cần DB: fetch rỗng + dry_run."""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

from app.config import Settings
from app.domain.contracts import MEASURE_FIELDS, FetchResult, MappingReport
from app.services.ingestion import IngestionService, IngestStats

UTC = ZoneInfo("UTC")


class _Rec:
    def __init__(self, source: str) -> None:
        self.source = source
        self.vendor_tz = UTC
        self.measure_fields = MEASURE_FIELDS
        self.calls: list[tuple[str, date]] = []

    def fetch_telemetry(self, psn: str, day: date) -> FetchResult:
        self.calls.append((psn, day))
        return FetchResult(report=MappingReport())

    def fetch_devices(self, psns: list[str]) -> list:
        return []

    def close(self) -> None:
        return None

    def begin_cycle(self) -> None:
        return None


def _settings() -> Settings:
    return Settings(app_env="test", db_password="x", scheduler_enabled=False)


def test_each_psn_hits_only_its_port() -> None:
    a = _Rec("a")
    b = _Rec("b")
    svc = IngestionService(
        a,
        session_factory=lambda: None,  # type: ignore[arg-type]
        settings=_settings(),
        psns=["P1", "P2"],
        ports_by_psn={"P1": a, "P2": b},
    )
    day = date(2026, 8, 27)
    svc.ingest_psn_day("P1", day, IngestStats(), dry_run=True)
    svc.ingest_psn_day("P2", day, IngestStats(), dry_run=True)
    assert a.calls == [("P1", day)]
    assert b.calls == [("P2", day)]


def test_unknown_psn_is_error_not_no_data() -> None:
    a = _Rec("a")
    svc = IngestionService(
        a,
        session_factory=lambda: None,  # type: ignore[arg-type]
        settings=_settings(),
        psns=["P1"],
        ports_by_psn={"P1": a},
    )
    stats = IngestStats()
    svc.ingest_psn_day("P-UNKNOWN", date(2026, 8, 27), stats)
    assert stats.psns_no_data == []
    assert any("không có cổng telemetry" in e for e in stats.errors)
    assert a.calls == []
