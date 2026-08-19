"""IngestionService — đường ghi DUY NHẤT vào telemetry.

Scheduler, CLI backfill, seed-demo và endpoint admin đều đi qua đây. Không nhân bản
logic ghi: nhờ vậy semantics của --repair, dedupe và thống kê giống nhau theo cấu
trúc, không phải theo kỷ luật.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.domain.contracts import MappingReport, TelemetryPort
from app.repositories import ingest_runs as runs_repo
from app.repositories import telemetry as tel_repo
from app.repositories import terminals as term_repo

log = logging.getLogger(__name__)

UTC = ZoneInfo("UTC")


@dataclass(slots=True)
class IngestStats:
    fetched: int = 0
    inserted: int = 0
    duplicates: int = 0
    intra_batch_dupes: int = 0
    terminals_created: int = 0
    rejected_rows: int = 0
    dropped_foreign_psn: int = 0
    # Zero row KHÔNG phải lỗi — nó có ô riêng, không đi vào `errors`. Cả hai thiết
    # bị thật đang offline hàng tháng nên MỌI cycle sẽ hợp lệ trả 0 row; nếu tính
    # đó là lỗi thì health đỏ vĩnh viễn và circuit breaker trip ngay cycle đầu.
    psns_no_data: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    days_processed: int = 0
    mapping: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if not self.errors:
            return "success"
        if self.inserted or self.duplicates or self.days_processed > len(self.errors):
            return "partial"
        return "failed"

    def summary(self) -> str:
        return (
            f"fetched={self.fetched} inserted={self.inserted} "
            f"duplicates={self.duplicates} terminals_created={self.terminals_created} "
            f"no_data={len(self.psns_no_data)} errors={len(self.errors)}"
        )


class FatalIngestError(RuntimeError):
    """Wrapper cho lỗi khiến ingest không thể tiếp tục (auth chết).

    Service KHÔNG import exception của vendor — đó là cả điểm của biên adapter. Nó
    nhận biết lỗi fatal qua ``fatal_exc_types`` được inject từ tầng lắp ráp.
    """

    def __init__(self, detail: str, remediation: str = "") -> None:
        super().__init__(detail)
        self.detail = detail
        self.remediation = remediation


class IngestionService:
    def __init__(
        self,
        adapter: TelemetryPort,
        session_factory: sessionmaker[Session],
        settings: Settings,
        *,
        fatal_exc_types: tuple[type[BaseException], ...] = (),
        psns: list[str] | None = None,
    ) -> None:
        self._adapter = adapter
        self._sf = session_factory
        self._s = settings
        # Inject thay vì import: services/ không được biết adapters/xingke tồn tại.
        self._fatal: tuple[type[BaseException], ...] = fatal_exc_types
        self._configured_psns = psns

    # ---------------------------------------------------------------- helpers

    @property
    def stale_after(self) -> timedelta:
        return timedelta(minutes=self._s.online_stale_minutes)

    def _target_psns(self, session: Session) -> list[str]:
        """PSN cần ingest: cấu hình tường minh, nếu không thì những gì DB đã biết."""
        if self._configured_psns:
            return list(self._configured_psns)
        return term_repo.all_psns(session)

    def _vendor_tz(self) -> ZoneInfo:
        # Service không biết TZ vendor; adapter phơi ra nếu có, mặc định UTC.
        tz = getattr(self._adapter, "vendor_tz", None)
        return tz if isinstance(tz, ZoneInfo) else UTC

    def _vendor_days(self, days_back: int) -> list[date]:
        """Các ngày lịch cần fetch, tính theo giờ VENDOR.

        Fetch hôm nay + N ngày trước. Không phải hoang tưởng: vendor ở UTC+8, công
        ty ở UTC+7, lưu UTC — "hôm nay" nhập nhằng qua ba múi giờ, và 23:30 ICT thì
        Thượng Hải đã sang ngày mai. Cửa sổ 2 ngày phủ mọi cách hiểu mà service này
        không cần biết TZ của vendor (giữ nguyên biên adapter). Giá: 2 HTTP call
        cho mỗi thiết bị mỗi cycle.
        """
        today_vendor = datetime.now(tz=UTC).astimezone(self._vendor_tz()).date()
        return [today_vendor - timedelta(days=i) for i in range(days_back + 1)]

    # ---------------------------------------------------------------- ingest

    def ingest_psn_day(
        self,
        psn: str,
        day: date,
        stats: IngestStats,
        *,
        repair: bool = False,
        dry_run: bool = False,
    ) -> None:
        """Một PSN, một ngày. Lỗi ở đây không được làm chết các PSN khác."""
        try:
            result = self._adapter.fetch_telemetry(psn, day)
        except self._fatal as exc:
            raise FatalIngestError(
                f"adapter báo lỗi không phục hồi được khi fetch psn={psn} "
                f"day={day}: {exc}",
                remediation=str(getattr(exc, "remediation", "")),
            ) from exc
        except Exception as exc:
            stats.errors.append(f"{psn} {day}: {type(exc).__name__}: {exc}")
            log.warning("ingest: lỗi psn=%s day=%s: %s", psn, day, exc)
            return

        stats.days_processed += 1
        stats.fetched += len(result.readings)
        stats.rejected_rows += result.report.rejected_rows
        stats.dropped_foreign_psn += result.report.dropped_foreign_psn
        _merge_mapping(stats, result.report)

        if not result.readings:
            if psn not in stats.psns_no_data:
                stats.psns_no_data.append(psn)
            return

        if dry_run:
            return

        with self._sf() as session, session.begin():
            # capacity_l đến kèm mỗi lần đọc nhưng thuộc terminals — lấy từ bản
            # đọc mới nhất có giá trị.
            capacity = next(
                (r.capacity_l for r in reversed(result.readings) if r.capacity_l),
                None,
            )
            tid, created = term_repo.upsert(
                session,
                psn,
                default_capacity_l=capacity
                or Decimal(str(self._s.default_tank_capacity_l)),
            )
            if created:
                stats.terminals_created += 1

            rows = [tel_repo.to_row(r, tid) for r in result.readings]
            inserted, dupes = tel_repo.bulk_upsert(session, rows, repair=repair)
            stats.inserted += inserted
            stats.duplicates += dupes

    def sync_terminals(self, stats: IngestStats, psns: list[str]) -> None:
        """Làm mới metadata thiết bị. Không fatal nếu thất bại."""
        if not psns:
            return
        try:
            metas = self._adapter.fetch_devices(psns)
        except self._fatal as exc:
            raise FatalIngestError(
                f"adapter báo lỗi không phục hồi được khi fetch_devices: {exc}",
                remediation=str(getattr(exc, "remediation", "")),
            ) from exc
        except Exception as exc:
            # Metadata thiếu không ngăn được việc ingest telemetry — đừng để nó
            # chặn phần có giá trị hơn.
            stats.errors.append(f"sync_terminals: {type(exc).__name__}: {exc}")
            log.warning("ingest: sync_terminals thất bại: %s", exc)
            return

        with self._sf() as session, session.begin():
            for meta in metas:
                _, created = term_repo.upsert(
                    session,
                    meta.psn,
                    meta=meta,
                    default_capacity_l=Decimal(
                        str(self._s.default_tank_capacity_l)
                    ),
                )
                if created:
                    stats.terminals_created += 1

    def refresh_statuses(self, psns: list[str]) -> None:
        with self._sf() as session, session.begin():
            latest = tel_repo.max_sampled_at(session, psns)
            term_repo.bump_last_seen(session, latest)
            # Chạy cho MỌI row, vô điều kiện: thiết bị ngừng báo thì không có
            # gì trong `latest` chạm tới nó, nên đây là chỗ duy nhất nó chuyển
            # sang offline.
            term_repo.refresh_status_cache(session, self.stale_after)

    # ---------------------------------------------------------------- cycle

    def run_cycle(
        self,
        *,
        trigger: str = "scheduler",
        repair: bool = False,
        sync_meta: bool = True,
    ) -> IngestStats:
        started = datetime.now(tz=UTC)
        stats = IngestStats()

        with self._sf() as session:
            psns = self._target_psns(session)

        if not psns:
            log.warning(
                "ingest: không có PSN nào để lấy. Set XINGKE_ALLOWED_PSNS hoặc chạy "
                "`python -m app.cli discover` để tạo terminal trước."
            )

        days = self._vendor_days(self._s.ingest_days_back)
        fatal: FatalIngestError | None = None
        try:
            if sync_meta:
                self.sync_terminals(stats, psns)
            for psn in psns:
                for day in days:
                    self.ingest_psn_day(psn, day, stats, repair=repair)
        except FatalIngestError as exc:
            fatal = exc
            stats.errors.append(f"FATAL: {exc.detail}")
        finally:
            if psns:
                try:
                    self.refresh_statuses(psns)
                except Exception as exc:
                    stats.errors.append(f"refresh_statuses: {exc}")
            self._record(
                stats,
                trigger,
                started,
                {
                    "psns": psns,
                    "days": [d.isoformat() for d in days],
                    "repair": repair,
                },
            )

        if fatal is not None:
            raise fatal
        log.info("ingest cycle (%s): %s", trigger, stats.summary())
        return stats

    def backfill(
        self,
        psns: list[str],
        start: date,
        end: date,
        *,
        repair: bool = False,
        dry_run: bool = False,
        trigger: str = "cli",
        on_day: Callable[[str, date, IngestStats], None] | None = None,
    ) -> IngestStats:
        """Walk từng ngày. Endpoint vendor chỉ nhận MỘT ngày, không có range.

        Resume miễn phí từ idempotency: upsert là ON CONFLICT DO NOTHING trên
        (psn, sampled_at), nên một backfill bị ngắt chỉ cần chạy lại đúng command.
        Không checkpoint table, không cờ --skip-done để làm sai.
        """
        started = datetime.now(tz=UTC)
        stats = IngestStats()
        fatal: FatalIngestError | None = None
        try:
            for psn in psns:
                day = start
                while day <= end:
                    self.ingest_psn_day(
                        psn, day, stats, repair=repair, dry_run=dry_run
                    )
                    if on_day is not None:
                        on_day(psn, day, stats)
                    day += timedelta(days=1)
        except FatalIngestError as exc:
            fatal = exc
            stats.errors.append(f"FATAL: {exc.detail}")
        finally:
            if not dry_run:
                if psns:
                    try:
                        self.refresh_statuses(psns)
                    except Exception as exc:
                        stats.errors.append(f"refresh_statuses: {exc}")
                self._record(
                    stats,
                    trigger,
                    started,
                    {
                        "psns": psns,
                        "from": start.isoformat(),
                        "to": end.isoformat(),
                        "repair": repair,
                        "backfill": True,
                    },
                )
        if fatal is not None:
            raise fatal
        return stats

    def _record(
        self,
        stats: IngestStats,
        trigger: str,
        started: datetime,
        params: dict[str, Any],
    ) -> None:
        try:
            with self._sf() as session, session.begin():
                runs_repo.record(
                    session,
                    trigger=trigger,
                    status=stats.status,
                    started_at=started,
                    finished_at=datetime.now(tz=UTC),
                    fetched=stats.fetched,
                    inserted=stats.inserted,
                    duplicates=stats.duplicates,
                    terminals_created=stats.terminals_created,
                    errors=stats.errors,
                    params=params,
                    mapping_report=stats.mapping,
                )
        except Exception as exc:
            # Không để việc ghi audit log làm chết một lần ingest đã thành công.
            log.error("ingest: không ghi được ingest_runs: %s", exc)


def _merge_mapping(stats: IngestStats, report: MappingReport) -> None:
    """Gộp MappingReport của nhiều lần fetch vào một dict để persist."""
    acc = stats.mapping
    acc["n_rows"] = acc.get("n_rows", 0) + report.n_rows
    present = acc.setdefault("present", {})
    for k, v in report.present.items():
        present[k] = present.get(k, 0) + v
    acc.setdefault("resolved_from", {}).update(report.resolved_from)
    unmapped = set(acc.get("unmapped_keys", [])) | report.unmapped_keys
    acc["unmapped_keys"] = sorted(unmapped)
    acc["rejected_rows"] = acc.get("rejected_rows", 0) + report.rejected_rows
    acc["dropped_foreign_psn"] = (
        acc.get("dropped_foreign_psn", 0) + report.dropped_foreign_psn
    )
    if report.errors:
        errs = acc.setdefault("errors", [])
        errs.extend({"field": f, "error": e} for f, e in report.errors)
