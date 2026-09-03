"""IngestionService — đường ghi DUY NHẤT vào telemetry.

Scheduler, CLI backfill và endpoint admin đều đi qua đây. Không nhân bản
logic ghi: nhờ vậy semantics của --repair, dedupe và thống kê giống nhau theo cấu
trúc, không phải theo kỷ luật.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.domain.contracts import MappingReport, TelemetryPort, VendorAlarmPort
from app.repositories import ingest_runs as runs_repo
from app.repositories import telemetry as tel_repo
from app.repositories import terminals as term_repo
from app.repositories import vendor_alarms as alarm_repo

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
    # PSN đã đưa về ít nhất một dòng trong cycle này. Cần thiết vì cửa sổ fetch
    # gồm NHIỀU ngày: nguồn phút chỉ có bản ghi của hôm nay, nên lần gọi cho hôm
    # qua trả rỗng và PSN bị ghi vào psns_no_data, rồi không bao giờ rút ra — dù
    # cùng cycle đó nó đưa về 1038 dòng (đo được, run 215). Một tín hiệu giám sát
    # nói "không có dữ liệu" về nguồn đang chạy tốt nhất là tín hiệu không dùng
    # được, và đó đúng là lớp lỗi cả phiên này đi dọn.
    psns_with_data: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    days_processed: int = 0
    mapping: dict[str, Any] = field(default_factory=dict)
    alarms_inserted: int = 0
    alarms_duplicates: int = 0

    def no_data_psns(self) -> list[str]:
        """PSN KHÔNG có dữ liệu nào trong CẢ cycle, không phải trong một ngày."""
        return [p for p in self.psns_no_data if p not in self.psns_with_data]

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
            f"alarms_inserted={self.alarms_inserted} "
            f"alarms_duplicates={self.alarms_duplicates} "
            f"no_data={len(self.no_data_psns())} errors={len(self.errors)}"
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
        ports_by_psn: dict[str, TelemetryPort] | None = None,
        alarm_port: VendorAlarmPort | None = None,
    ) -> None:
        self._adapter = adapter
        self._sf = session_factory
        self._s = settings
        # Inject thay vì import: services/ không được biết adapters/* tồn tại.
        self._fatal: tuple[type[BaseException], ...] = fatal_exc_types
        self._configured_psns = psns
        self._ports_by_psn = ports_by_psn
        self._alarm_port = alarm_port

    # ---------------------------------------------------------------- helpers

    @property
    def stale_after(self) -> timedelta:
        return timedelta(minutes=self._s.online_stale_minutes)

    def _target_psns(self, session: Session) -> list[str]:
        """PSN cần ingest: cấu hình tường minh, nếu không thì những gì DB đã biết."""
        if self._configured_psns:
            return list(self._configured_psns)
        return term_repo.all_psns(session)

    def _begin_cycle(self) -> None:
        seen: set[int] = set()
        ports: list[TelemetryPort] = [self._adapter]
        if self._ports_by_psn:
            ports.extend(self._ports_by_psn.values())
        for port in ports:
            i = id(port)
            if i in seen:
                continue
            seen.add(i)
            port.begin_cycle()

    def _port(self, psn: str) -> TelemetryPort | None:
        if self._ports_by_psn is None:
            return self._adapter
        return self._ports_by_psn.get(psn)

    def _vendor_days_for(self, tz: ZoneInfo) -> list[date]:
        """Các ngày lịch cần fetch, tính theo giờ VENDOR của ĐÚNG port."""
        today_vendor = datetime.now(tz=UTC).astimezone(tz).date()
        # Cũ trước: nguồn stream newest-first, lần fetch ngày cũ nhất lấp cache
        # cho cả những ngày mới hơn — một stream/cycle thay vì N.
        return [
            today_vendor - timedelta(days=i)
            for i in range(self._s.ingest_days_back, -1, -1)
        ]

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
        port = self._port(psn)
        if port is None:
            stats.errors.append(f"{psn}: không có cổng telemetry")
            return
        try:
            result = port.fetch_telemetry(psn, day)
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
        _merge_mapping(stats, result.report, source=port.source)

        if not result.readings:
            if psn not in stats.psns_no_data:
                stats.psns_no_data.append(psn)
            return
        if psn not in stats.psns_with_data:
            stats.psns_with_data.append(psn)

        if dry_run:
            return

        with self._sf() as session, session.begin():
            # capacity_l đến kèm mỗi lần đọc nhưng thuộc terminals — lấy từ bản
            # đọc mới nhất có giá trị.
            capacity = next(
                (r.capacity_l for r in reversed(result.readings) if r.capacity_l),
                None,
            )
            # Toạ độ cũng vậy, nhưng nó là dữ liệu THỈNH THOẢNG CÓ: cùng một thiết
            # bị có ngày trả toạ độ thật, có ngày trả 0,0 — xác minh trên dữ liệu
            # thật (2604200016 ngày 2026-07-23 có toạ độ, ngày 2026-06-02 thì cả 17
            # dòng đều 0,0). Adapter đã loại 0,0, nên ở đây chỉ lấy cặp gần nhất
            # còn sót. Không có cặp nào thì để None và upsert() không ghi gì —
            # KHÔNG BAO GIỜ xoá toạ độ đang có chỉ vì hôm nay module mất định vị.
            lat, lon = next(
                (
                    (r.latitude, r.longitude)
                    for r in reversed(result.readings)
                    if r.latitude is not None and r.longitude is not None
                ),
                (None, None),
            )
            tid, created = term_repo.upsert(
                session,
                psn,
                default_capacity_l=capacity
                or Decimal(str(self._s.default_tank_capacity_l)),
                default_latitude=lat,
                default_longitude=lon,
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
        groups: dict[int, tuple[TelemetryPort, list[str]]] = {}
        skipped: list[str] = []
        for psn in psns:
            port = self._port(psn)
            if port is None:
                skipped.append(psn)
                continue
            rec = groups.setdefault(id(port), (port, []))
            rec[1].append(psn)
        if skipped:
            stats.errors.append(
                "sync_terminals: không có cổng telemetry cho "
                + ", ".join(skipped)
            )
        for port, group in groups.values():
            try:
                metas = port.fetch_devices(group)
            except self._fatal as exc:
                raise FatalIngestError(
                    f"adapter báo lỗi không phục hồi được khi fetch_devices: {exc}",
                    remediation=str(getattr(exc, "remediation", "")),
                ) from exc
            except Exception as exc:
                stats.errors.append(f"sync_terminals: {type(exc).__name__}: {exc}")
                log.warning("ingest: sync_terminals thất bại: %s", exc)
                continue

            with self._sf() as session, session.begin():
                for meta in metas:
                    default_cap = (
                        meta.capacity_l
                        if meta.capacity_l is not None
                        else Decimal(str(self._s.default_tank_capacity_l))
                    )
                    _, created = term_repo.upsert(
                        session,
                        meta.psn,
                        meta=meta,
                        default_capacity_l=default_cap,
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
        self._begin_cycle()

        with self._sf() as session:
            psns = self._target_psns(session)

        if not psns:
            log.warning(
                "ingest: không có PSN nào để lấy. Set XINGKE_ALLOWED_PSNS hoặc chạy "
                "`python -m app.cli discover` để tạo terminal trước."
            )

        fatal: FatalIngestError | None = None
        try:
            if sync_meta:
                self.sync_terminals(stats, psns)
            for psn in psns:
                port = self._port(psn)
                tz = port.vendor_tz if port is not None else UTC
                for day in self._vendor_days_for(tz):
                    self.ingest_psn_day(psn, day, stats, repair=repair)
            if self._alarm_port is not None:
                self._ingest_alarms(stats)
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
                    "repair": repair,
                    "alarms_inserted": stats.alarms_inserted,
                    "alarms_duplicates": stats.alarms_duplicates,
                },
            )

        if fatal is not None:
            raise fatal
        log.info("ingest cycle (%s): %s", trigger, stats.summary())
        return stats

    def _ingest_alarms(self, stats: IngestStats) -> None:
        port = self._alarm_port
        if port is None:
            return
        try:
            days = self._vendor_days_for(port.vendor_tz)
            alarms = []
            for day in days:
                alarms.extend(port.fetch_alarms(day))
        except self._fatal as exc:
            raise FatalIngestError(
                f"adapter báo lỗi không phục hồi được khi fetch_alarms: {exc}",
                remediation=str(getattr(exc, "remediation", "")),
            ) from exc
        except Exception as exc:
            stats.errors.append(f"alarms: {type(exc).__name__}: {exc}")
            log.warning("ingest: fetch_alarms thất bại: %s", exc)
            return
        if not alarms:
            return
        with self._sf() as session, session.begin():
            inserted, dupes = alarm_repo.bulk_insert(session, alarms)
        stats.alarms_inserted += inserted
        stats.alarms_duplicates += dupes

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
        self._begin_cycle()
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


def _merge_mapping(
    stats: IngestStats, report: MappingReport, *, source: str
) -> None:
    """Gộp MappingReport. Top-level giữ cho CLI; by_source tránh ghi đè provenance."""
    acc: dict[str, Any] = stats.mapping
    acc["n_rows"] = int(acc.get("n_rows", 0)) + report.n_rows
    acc["source_rows"] = int(acc.get("source_rows", 0)) + report.source_rows
    present = cast(dict[str, int], acc.setdefault("present", {}))
    for name, n in report.present.items():
        present[name] = present.get(name, 0) + n
    rf = cast(dict[str, str], acc.setdefault("resolved_from", {}))
    for name, alias in report.resolved_from.items():
        rf.setdefault(name, alias)
    unmapped = set(acc.get("unmapped_keys", [])) | report.unmapped_keys
    acc["unmapped_keys"] = sorted(unmapped)
    acc["rejected_rows"] = acc.get("rejected_rows", 0) + report.rejected_rows
    acc["dropped_foreign_psn"] = (
        acc.get("dropped_foreign_psn", 0) + report.dropped_foreign_psn
    )
    acc["zero_as_missing"] = acc.get("zero_as_missing", 0) + report.zero_as_missing
    if report.errors:
        errs = acc.setdefault("errors", [])
        errs.extend({"field": f, "error": e} for f, e in report.errors)
    by = acc.setdefault("by_source", {})
    bucket: dict[str, Any] = by.setdefault(source, {})
    bucket["n_rows"] = bucket.get("n_rows", 0) + report.n_rows
    bucket["source_rows"] = bucket.get("source_rows", 0) + report.source_rows
    # GIỮ mốc mới nhất, không cộng: chuỗi ISO ở UTC nên max() ra đúng thứ tự.
    if report.newest_source_at is not None:
        prev = bucket.get("newest_source_at")
        bucket["newest_source_at"] = (
            report.newest_source_at
            if prev is None
            else max(prev, report.newest_source_at)
        )
    bucket.setdefault("resolved_from", {}).update(report.resolved_from)
