"""CLI vận hành. Dùng CHUNG IngestionService với scheduler — không nhân bản write path.

    .venv\\Scripts\\python.exe -m app.cli check-db
    .venv\\Scripts\\python.exe -m app.cli run-once
    .venv\\Scripts\\python.exe -m app.cli probe
    .venv\\Scripts\\python.exe -m app.cli backfill --psn 2604200016 --from 2026-07-01 --to 2026-07-31
    .venv\\Scripts\\python.exe -m app.cli serve
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import typer
from sqlalchemy import text

from app.config import assert_venv, get_settings
from app.db.session import make_engine, make_session_factory
from app.factory import build_adapter
from app.logging_config import setup_logging
from app.repositories import ingest_runs as runs_repo
from app.repositories import telemetry as tel_repo
from app.repositories import terminals as term_repo
from app.services.ingestion import IngestionService, IngestStats

app = typer.Typer(add_completion=False, help="LNG monitoring — công cụ vận hành")
log = logging.getLogger(__name__)
UTC = ZoneInfo("UTC")


def _wire(*, adapter_override: Any = None) -> tuple[Any, ...]:
    """Dựng engine + service. Dùng ở mọi command."""
    assert_venv()
    settings = get_settings()
    setup_logging(settings.log_level)
    engine = make_engine(settings, application_name="lng-cli")
    sf = make_session_factory(engine)
    if adapter_override is not None:
        adapter, fatal, psns = adapter_override, (), None
    else:
        adapter, fatal, psns = build_adapter(settings)
    svc = IngestionService(adapter, sf, settings, fatal_exc_types=fatal, psns=psns)
    return settings, engine, sf, adapter, svc


def _print_stats(stats: IngestStats) -> None:
    typer.echo(f"  {stats.summary()}")
    if stats.psns_no_data:
        # In riêng, KHÔNG lẫn với errors: cả hai thiết bị thật đang offline hàng
        # tháng nên 0 dòng là kết quả bình thường, không phải sự cố.
        typer.echo(f"  không có dữ liệu: {', '.join(stats.psns_no_data)}")
    for e in stats.errors[:10]:
        typer.echo(f"  LỖI: {e}", err=True)
    unmapped = stats.mapping.get("unmapped_keys") or []
    if unmapped:
        typer.echo(f"  field vendor CHƯA MAP: {unmapped}", err=True)


@app.command("check-db")
def check_db() -> None:
    """Kiểm kết nối DB, in version và trạng thái migration."""
    _, engine, _sf, adapter, _svc = _wire()
    try:
        with engine.connect() as conn:
            ver = conn.execute(text("SELECT version()")).scalar_one()
            typer.echo(f"OK  {str(ver).split(',')[0]}")
            try:
                rev = conn.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one_or_none()
                typer.echo(f"    alembic_version = {rev}")
            except Exception:
                typer.echo(
                    "    alembic_version: CHƯA CÓ — chạy `alembic upgrade head`"
                )
    except Exception as exc:
        typer.echo(f"THẤT BẠI: {exc}", err=True)
        raise typer.Exit(1) from exc
    finally:
        adapter.close()
        engine.dispose()


@app.command("probe")
def probe() -> None:
    """Kiểm auth + mapping với vendor thật. Chỉ đọc, một PSN, một ngày."""
    settings = get_settings()
    if settings.xingke_adapter != "live":
        typer.echo(
            "XINGKE_ADAPTER=fake — probe cần adapter live. Set XINGKE_ADAPTER=live "
            "trong .env kèm credential.",
            err=True,
        )
        raise typer.Exit(1)
    _, engine, _sf, adapter, _svc = _wire()
    try:
        typer.echo(json.dumps(adapter.probe(), indent=2, ensure_ascii=False))
    finally:
        adapter.close()
        engine.dispose()


@app.command("discover")
def discover() -> None:
    """Làm mới metadata thiết bị từ vendor (không lấy telemetry)."""
    _, engine, sf, adapter, svc = _wire()
    stats = IngestStats()
    try:
        with sf() as session:
            known = term_repo.all_psns(session)
        _, _, cfg_psns = build_adapter(get_settings())
        svc.sync_terminals(stats, cfg_psns or known)
        _print_stats(stats)
    finally:
        adapter.close()
        engine.dispose()


@app.command("run-once")
def run_once(
    repair: Annotated[
        bool, typer.Option(help="cập nhật field NULL của dòng đã có")
    ] = False,
) -> None:
    """Một vòng ingest, đúng như scheduler chạy."""
    _, engine, _sf, adapter, svc = _wire()
    try:
        stats = svc.run_cycle(trigger="cli", repair=repair)
        _print_stats(stats)
        if stats.errors:
            raise typer.Exit(1)
    finally:
        adapter.close()
        engine.dispose()


@app.command("backfill")
def backfill(
    psn: Annotated[list[str] | None, typer.Option(help="lặp lại được")] = None,
    all_terminals: Annotated[
        bool, typer.Option("--all", help="mọi PSN trong DB")
    ] = False,
    from_: Annotated[str | None, typer.Option("--from", help="YYYY-MM-DD")] = None,
    to: Annotated[str | None, typer.Option("--to", help="YYYY-MM-DD")] = None,
    days: Annotated[int | None, typer.Option(help="N ngày gần nhất")] = None,
    dry_run: Annotated[bool, typer.Option(help="fetch nhưng không ghi")] = False,
    repair: Annotated[bool, typer.Option()] = False,
) -> None:
    """Backfill lịch sử, walk từng ngày.

    Endpoint vendor chỉ nhận MỘT ngày nên không có cách nào lấy cả range trong một
    request. Chạy tuần tự có throttle: bắn song song vào một cloud chưa biết
    rate-limit là cách nhanh nhất để bị block.

    Ngắt giữa đường thì chỉ cần chạy lại đúng command — upsert là idempotent nên
    resume miễn phí, không cần checkpoint table.
    """
    _, engine, sf, adapter, svc = _wire()
    try:
        if all_terminals or not psn:
            with sf() as session:
                psns = term_repo.all_psns(session)
        else:
            psns = list(psn)
        if not psns:
            typer.echo(
                "không có PSN nào. Chạy `discover` hoặc truyền --psn.", err=True
            )
            raise typer.Exit(1)

        today = datetime.now(tz=UTC).date()
        if days is not None:
            start, end = today - timedelta(days=days - 1), today
        elif from_ and to:
            start, end = date.fromisoformat(from_), date.fromisoformat(to)
        else:
            typer.echo("cần --days, hoặc cả --from và --to", err=True)
            raise typer.Exit(1)
        if start > end:
            typer.echo("--from phải <= --to", err=True)
            raise typer.Exit(1)

        typer.echo(
            f"backfill {len(psns)} PSN x {(end - start).days + 1} ngày "
            f"({start} .. {end}){' [DRY RUN]' if dry_run else ''}"
        )

        def progress(p: str, d: date, s: IngestStats) -> None:
            typer.echo(f"  {p} {d}  inserted={s.inserted} dup={s.duplicates}")

        stats = svc.backfill(
            psns, start, end, repair=repair, dry_run=dry_run, on_day=progress
        )
        _print_stats(stats)
        if stats.errors:
            raise typer.Exit(1)
    finally:
        adapter.close()
        engine.dispose()


@app.command("set-terminal")
def set_terminal(
    psn: Annotated[str, typer.Argument(help="PSN của bồn")],
    name: Annotated[str | None, typer.Option(help="tên do người vận hành đặt")] = None,
    capacity_l: Annotated[
        float | None, typer.Option(help="dung tích danh nghĩa (L)")
    ] = None,
) -> None:
    """Sửa tên hoặc dung tích. Ingest không ghi đè các field này."""
    from decimal import Decimal

    if name is None and capacity_l is None:
        typer.echo("cần --name và/hoặc --capacity-l", err=True)
        raise typer.Exit(1)
    _, engine, sf, adapter, _svc = _wire()
    try:
        with sf() as session, session.begin():
            term = term_repo.update_operator(
                session,
                psn,
                name=name,
                capacity_l=Decimal(str(capacity_l)) if capacity_l is not None else None,
            )
            if term is None:
                typer.echo(f"không có terminal {psn}", err=True)
                raise typer.Exit(1)
            typer.echo(
                f"OK  {term.psn}  name={term.name!r}  capacity_l={term.capacity_l}"
            )
    finally:
        adapter.close()
        engine.dispose()


@app.command("serve")
def serve(
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="mở dashboard sau khi server lên")
    ] = True,
) -> None:
    """Chạy API + dashboard. Một lệnh thay cho nhớ uvicorn + host + port."""
    import threading
    import time
    import webbrowser

    import uvicorn

    assert_venv()
    settings = get_settings()
    url = f"http://{settings.api_host}:{settings.api_port}/ui/"
    if open_browser:
        def _open() -> None:
            time.sleep(1.5)
            webbrowser.open(url)

        threading.Thread(target=_open, daemon=True).start()
    typer.echo(f"dashboard: {url}")
    uvicorn.run(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
    )


@app.command("status")
def status_cmd() -> None:
    """In nhanh trạng thái các terminal và lần ingest gần nhất."""
    settings, engine, sf, adapter, _svc = _wire()
    try:
        with sf() as session:
            terms = term_repo.list_all(session)
            latest = tel_repo.latest_many(session, [t.psn for t in terms])
            typer.echo(
                f"{'PSN':<14}{'status':<10}{'volume_l':>10}{'fill%':>9}  last_seen"
            )
            for t in terms:
                r = latest.get(t.psn)
                fill = ""
                if r and r.volume_l is not None and t.capacity_l:
                    fill = f"{(r.volume_l / t.capacity_l * 100):.2f}"
                vol = f"{r.volume_l}" if r and r.volume_l is not None else "-"
                seen = (
                    t.last_seen_at.astimezone(settings.tzinfo).strftime(
                        "%Y-%m-%d %H:%M"
                    )
                    if t.last_seen_at
                    else "chưa bao giờ"
                )
                typer.echo(f"{t.psn:<14}{t.status:<10}{vol:>10}{fill:>9}  {seen}")
            run = runs_repo.last_run(session)
            if run:
                typer.echo(
                    f"\nlần ingest cuối: #{run.id} {run.status} "
                    f"inserted={run.inserted} dup={run.duplicates} "
                    f"errors={run.error_count}"
                )
            else:
                typer.echo("\nchưa có lần ingest nào")
    finally:
        adapter.close()
        engine.dispose()


if __name__ == "__main__":
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)
