"""Đọc/ghi bảng telemetry."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, literal_column, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import Telemetry
from app.domain.contracts import ALL_MEASURE_FIELDS, NormalizedTelemetry

log = logging.getLogger(__name__)

CHUNK = 500

# Cột được phép cập nhật ở chế độ --repair. `sampled_at`/`psn` là khoá nên không có
# ở đây; `created_at` là thời điểm ta ghi nên cũng không.
# raw_payload CỐ Ý không có: '{}' không NULL nên COALESCE ghi đè payload thật.
_REPAIRABLE = (*ALL_MEASURE_FIELDS, "volume_percent_source", "medium_name",
               "tank_type_name", "vendor_ts_raw")


def to_row(reading: NormalizedTelemetry, terminal_id: UUID) -> dict[str, Any]:
    # capacity_l / latitude / longitude thuộc bảng terminals, không phải telemetry
    # — vendor gửi kèm mỗi lần đọc nhưng chúng là cấu hình tài sản. Quên loại ở
    # đây là một INSERT tham chiếu cột không tồn tại.
    d = reading.model_dump(exclude={"capacity_l", "latitude", "longitude"})
    d["terminal_id"] = terminal_id
    # StrEnum -> str để psycopg khỏi phải đoán.
    src = d.get("volume_percent_source")
    d["volume_percent_source"] = str(src) if src is not None else None
    return d


def dedupe(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Loại trùng (psn, sampled_at) TRONG batch, giữ bản cuối.

    Với ON CONFLICT DO NOTHING việc này về mặt kỹ thuật là optional, nhưng nó (a)
    làm số liệu thống kê trung thực và (b) BẮT BUỘC ngay khi ai đó chuyển sang
    DO UPDATE — Postgres raise "ON CONFLICT DO UPDATE command cannot affect row a
    second time" nếu batch có trùng. Làm luôn để bỏ mìn.
    """
    seen: dict[tuple[str, datetime], dict[str, Any]] = {}
    for r in rows:
        seen[(r["psn"], r["sampled_at"])] = r
    return list(seen.values()), len(rows) - len(seen)


def bulk_upsert(
    session: Session, rows: list[dict[str, Any]], *, repair: bool = False
) -> tuple[int, int]:
    """Upsert idempotent. Trả về (inserted, duplicates).

    Đếm bằng RETURNING chứ không bằng rowcount: DO NOTHING không cho feedback
    per-row và rowcount không đáng tin giữa các driver, còn RETURNING chỉ phát ra
    một dòng cho mỗi dòng THỰC SỰ được ghi.
    """
    if not rows:
        return 0, 0

    deduped, intra = dedupe(rows)
    inserted = 0
    conflicts = 0

    for start in range(0, len(deduped), CHUNK):
        chunk = deduped[start : start + CHUNK]
        stmt = pg_insert(Telemetry).values(chunk)
        if repair:
            # COALESCE(excluded, current): điền chỗ đang NULL nhưng KHÔNG BAO GIỜ
            # ghi NULL lên một giá trị thật. Vendor đôi khi trả null cho field mà
            # lần trước có số; không có COALESCE thì --repair sẽ xoá dữ liệu tốt.
            stmt = stmt.on_conflict_do_update(
                index_elements=["psn", "sampled_at"],
                set_={
                    c: func.coalesce(stmt.excluded[c], getattr(Telemetry, c))
                    for c in _REPAIRABLE
                },
            )
            # DO UPDATE phát RETURNING cho MỌI row (cả insert lẫn update), nên đếm
            # len() sẽ tính update thành insert. `xmax = 0` là idiom Postgres phân
            # biệt: true = vừa insert, false = update một row đã có (một dup).
            rows_out = session.execute(
                stmt.returning(literal_column("xmax = 0").label("inserted"))
            ).all()
            ins = sum(1 for r in rows_out if r.inserted)
            inserted += ins
            conflicts += len(rows_out) - ins
        else:
            # Mặc định: một điểm đo là sự thật lịch sử bất biến. DO NOTHING chỉ phát
            # RETURNING cho row THỰC SỰ được insert; row conflict không xuất hiện.
            stmt = stmt.on_conflict_do_nothing(index_elements=["psn", "sampled_at"])
            n = len(session.execute(stmt.returning(Telemetry.psn)).fetchall())
            inserted += n
            conflicts += len(chunk) - n

    return inserted, conflicts + intra


def latest_for(session: Session, psn: str) -> Telemetry | None:
    """Lần đọc mới nhất. Một backward index-scan trên pk_telemetry."""
    return session.execute(
        select(Telemetry)
        .where(Telemetry.psn == psn)
        .order_by(Telemetry.sampled_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def latest_many(session: Session, psns: list[str]) -> dict[str, Telemetry]:
    """Lần đọc mới nhất cho nhiều PSN, một query.

    DISTINCT ON là idiom Postgres cho latest-per-group; nó dùng đúng index
    (psn, sampled_at) nên không cần window function hay N+1.
    """
    if not psns:
        return {}
    rows = session.execute(
        select(Telemetry)
        .where(Telemetry.psn.in_(psns))
        .order_by(Telemetry.psn, Telemetry.sampled_at.desc())
        .distinct(Telemetry.psn)
    ).scalars()
    return {r.psn: r for r in rows}


def _history_stmt(psn: str, start: datetime, end: datetime) -> Select[Any]:
    return select(Telemetry).where(
        Telemetry.psn == psn,
        Telemetry.sampled_at >= start,
        Telemetry.sampled_at <= end,
    )


def history(
    session: Session,
    psn: str,
    start: datetime,
    end: datetime,
    *,
    limit: int,
    offset: int,
    ascending: bool,
) -> tuple[list[Telemetry], int]:
    total = session.execute(
        select(func.count()).select_from(_history_stmt(psn, start, end).subquery())
    ).scalar_one()
    order = Telemetry.sampled_at.asc() if ascending else Telemetry.sampled_at.desc()
    items = (
        session.execute(
            _history_stmt(psn, start, end).order_by(order).limit(limit).offset(offset)
        )
        .scalars()
        .all()
    )
    return list(items), int(total)


def series(
    session: Session,
    psn: str,
    start: datetime,
    end: datetime,
    *,
    limit: int = 50_000,
    bucket_minutes: int | None = None,
) -> list[tuple[datetime, float | None, float | None]]:
    """Chuỗi (sampled_at, volume_l, pressure_mpa) tăng dần, cho tầng dự báo.

    Cố ý KHÔNG tái dụng ``history()``: dự báo cần hàng nghìn điểm nhưng chỉ ba
    cột, còn ``history()`` hydrate cả ORM object kèm ``raw_payload`` JSONB (~1 KB
    mỗi dòng) và chạy thêm một ``COUNT(*)`` mà ở đây không ai dùng.

    ``bucket_minutes``: lấy bản đọc MỚI NHẤT trong mỗi bucket. Nguồn 1 phút phải
    đi qua đây — pairwise trên nhịp 1 phút nằm dưới deadband dung tích.

    ``limit`` cắt phần CŨ NHẤT (ORDER BY DESC rồi đảo lại).
    """
    rows = _series_rows(
        session,
        psn,
        start,
        end,
        ("volume_l", "pressure_mpa"),
        limit=limit,
        bucket_minutes=bucket_minutes,
    )
    return [(at, v, p) for at, v, p in rows]


def series_with_gas(
    session: Session,
    psn: str,
    start: datetime,
    end: datetime,
    *,
    limit: int = 50_000,
    bucket_minutes: int | None = None,
) -> list[tuple[datetime, float | None, float | None, float | None]]:
    """Như ``series`` nhưng kèm ``gm_totalizer_nm3``.

    Tách thành hàm riêng thay vì nới ``series`` thành 4 cột: 6 chỗ đang gọi
    ``series`` không cần số khí, và đổi độ rộng tuple sẽ bắt cả 6 phải sửa vô ích.
    Cả hai dùng chung ``_series_rows`` nên không có truy vấn nào bị nhân đôi —
    thứ duy nhất khác nhau là tập cột được chiếu ra.

    Bồn không có đồng hồ khí thì cột này NULL ở mọi dòng, và tầng domain
    (``estimate_dual_consumption``) im lặng thay vì phát số bịa.
    """
    return _series_rows(
        session,
        psn,
        start,
        end,
        ("volume_l", "pressure_mpa", "gm_totalizer_nm3"),
        limit=limit,
        bucket_minutes=bucket_minutes,
    )


def _series_rows(
    session: Session,
    psn: str,
    start: datetime,
    end: datetime,
    measure_cols: tuple[str, ...],
    *,
    limit: int,
    bucket_minutes: int | None,
) -> list[tuple[Any, ...]]:
    """Dựng truy vấn chuỗi cho một tập cột bất kỳ. Trả về tăng dần theo thời gian.

    ``Decimal`` được đổi sang ``float`` NGAY ở đây, không để lọt lên domain: tầng
    dự báo chia và nhân các số này với nhau, và trộn ``Decimal`` với ``float``
    trong một biểu thức là ``TypeError`` lúc chạy.
    """
    cols = (Telemetry.sampled_at, *(getattr(Telemetry, c) for c in measure_cols))
    filt = (
        Telemetry.psn == psn,
        Telemetry.sampled_at >= start,
        Telemetry.sampled_at <= end,
    )
    if bucket_minutes and bucket_minutes > 0:
        epoch = func.floor(
            func.extract("epoch", Telemetry.sampled_at) / (bucket_minutes * 60)
        )
        inner = (
            select(*cols, epoch.label("b"))
            .where(*filt)
            .distinct(epoch)
            .order_by(epoch, Telemetry.sampled_at.desc())
            .subquery()
        )
        outer_cols = (inner.c.sampled_at, *(inner.c[c] for c in measure_cols))
        rows = session.execute(
            select(*outer_cols).order_by(inner.c.sampled_at.desc()).limit(limit)
        ).all()
    else:
        rows = session.execute(
            select(*cols)
            .where(*filt)
            .order_by(Telemetry.sampled_at.desc())
            .limit(limit)
        ).all()
    out: list[tuple[Any, ...]] = [
        (r[0], *(None if x is None else float(x) for x in r[1:])) for r in rows
    ]
    out.reverse()
    return out


def health_series(
    session: Session,
    psn: str,
    start: datetime,
    end: datetime,
    *,
    limit: int = 20_000,
) -> list[tuple[datetime, float | None, float | None]]:
    """Chuỗi (sampled_at, battery_v, signal_percent) tăng dần, cho tầng phân tích.

    Truy vấn RIÊNG chứ không mở rộng ``series()``: dự báo mức chứa chạy trên mọi
    request dashboard và không bao giờ cần pin/sóng, nên nhồi hai cột vào đó là bắt
    mọi request đọc thêm để phục vụ một trang mà phần lớn thời gian không ai mở.

    Cắt phần CŨ NHẤT giống ``series()`` và cùng lý do: xu hướng suy pin chỉ có nghĩa
    khi tính trên dữ liệu gần đây.
    """
    rows = session.execute(
        select(Telemetry.sampled_at, Telemetry.battery_v, Telemetry.signal_percent)
        .where(
            Telemetry.psn == psn,
            Telemetry.sampled_at >= start,
            Telemetry.sampled_at <= end,
        )
        .order_by(Telemetry.sampled_at.desc())
        .limit(limit)
    ).all()
    out = [
        (at, None if b is None else float(b), None if s is None else float(s))
        for at, b, s in rows
    ]
    out.reverse()
    return out


#: Cột và THỨ TỰ cột của báo cáo xuất ra. Cố định ở một chỗ để hai lần xuất cách
#: nhau vài tháng vẫn diff được với nhau, và để không ai vô tình thêm
#: ``raw_payload`` vào file gửi ra ngoài.
EXPORT_COLUMNS = (
    "sampled_at",
    "volume_l",
    "volume_percent",
    "pressure_mpa",
    "temperature_c",
    "level_mmwc",
    "diff_pressure_kpa",
    "vacuum_pa",
    "battery_v",
    "signal_percent",
)


def export_rows(
    session: Session,
    psn: str,
    start: datetime,
    end: datetime,
    *,
    limit: int = 200_000,
) -> list[Sequence[Any]]:
    """Dòng thô cho báo cáo CSV, tăng dần theo thời gian.

    Chỉ SELECT các cột trong ``EXPORT_COLUMNS`` — không hydrate ORM object nên
    ``raw_payload`` (JSONB, key tiếng Trung) không thể lọt vào file xuất ra dù ai
    sửa code phía trên thế nào. Đây là biện pháp cùng loại với
    ``api/schemas.py``: chặn ở nơi dữ liệu được lấy, không dựa vào kỷ luật.
    """
    cols = [getattr(Telemetry, c) for c in EXPORT_COLUMNS]
    return list(
        session.execute(
            select(*cols)
            .where(
                Telemetry.psn == psn,
                Telemetry.sampled_at >= start,
                Telemetry.sampled_at <= end,
            )
            .order_by(Telemetry.sampled_at.asc())
            .limit(limit)
        ).all()
    )


def daily_counts(
    session: Session,
    psn: str,
    start: datetime,
    end: datetime,
    *,
    tz_name: str,
) -> list[tuple[date, int]]:
    """Số lần đo theo NGÀY LỊCH giờ địa phương, tăng dần.

    Gom trong SQL chứ không kéo hàng nghìn dòng lên Python để đếm: một cửa sổ 90
    ngày ở nhịp 1 phút là ~130 000 dòng, và tất cả những gì cần là 90 con số.

    Gom theo giờ ĐỊA PHƯƠNG, không phải UTC. "Ngày 3/9 có bao nhiêu lần đo" là câu
    hỏi của người vận hành ở Việt Nam; gom theo UTC thì mỗi cột lệch 7 giờ và hai
    ngày đầu/cuối luôn trông thiếu dữ liệu một cách vô cớ.

    Chỉ trả về NGÀY CÓ dữ liệu. Ngày trống là ngày không xuất hiện — người gọi tự
    điền 0, vì chỉ họ biết cửa sổ mong đợi là bao nhiêu ngày.
    """
    day = func.date(func.timezone(tz_name, Telemetry.sampled_at)).label("d")
    rows = session.execute(
        select(day, func.count().label("n"))
        .where(
            Telemetry.psn == psn,
            Telemetry.sampled_at >= start,
            Telemetry.sampled_at <= end,
        )
        .group_by(day)
        .order_by(day)
    ).all()
    return [(r.d, int(r.n)) for r in rows]


_DAILY_OPEN_SQL = text(
    """
    SELECT d::date AS day, t.sampled_at, t.volume_l
    FROM generate_series(CAST(:start AS date), CAST(:end AS date), interval '1 day') AS d
    CROSS JOIN LATERAL (
        SELECT sampled_at, volume_l
        FROM telemetry
        WHERE psn = :psn
          AND volume_l IS NOT NULL
          AND sampled_at >= (d::date::timestamp AT TIME ZONE :tz)
          AND sampled_at <  ((d::date + 1)::timestamp AT TIME ZONE :tz)
        ORDER BY sampled_at ASC
        LIMIT 1
    ) AS t
    ORDER BY 1
    """
)


def daily_open(
    session: Session,
    psn: str,
    start: date,
    end: date,
    *,
    tz_name: str,
) -> list[tuple[date, datetime, float]]:
    """Lần đo ĐẦU TIÊN của mỗi ngày lịch giờ địa phương: thể tích đầu ngày THẬT.

    Trang Kế hoạch cần nó cho những ngày ĐÃ QUA. Bản trước chỉ có một con số khởi
    đầu — lần đo mới nhất — rồi chiếu ngược nó lên ngày bắt đầu mà người dùng chọn.
    Đo được 29/09/2026: đổi "Ngày bắt đầu" về 27/09 thì dòng 27/09 hiện 34,39 m³
    (số lúc 15:59 ngày 29/09) trong khi bồn thật hôm đó 15,40, và bảng bịa ra một
    lần nạp 29/09 trong khi xe thật đã tới 28/09.

    Một truy vấn LATERAL mỗi ngày, ``LIMIT 1`` theo PK ``(psn, sampled_at)``: 366
    lần dò index thay vì sắp xếp ~500 000 dòng của một năm ở nhịp 1 phút.

    Trả kèm ``sampled_at``: bồn im cả đêm thì "lần đo đầu ngày" có thể là 09:00 —
    sau giờ nạp — và người gọi phải biết để không gọi nó là thể tích đầu ngày.
    Ngày không có số đo thì không xuất hiện.
    """
    rows = session.execute(
        _DAILY_OPEN_SQL, {"psn": psn, "start": start, "end": end, "tz": tz_name}
    ).all()
    return [(r.day, r.sampled_at, float(r.volume_l)) for r in rows]


def max_sampled_at(session: Session, psns: list[str]) -> dict[str, datetime]:
    if not psns:
        return {}
    rows = session.execute(
        select(Telemetry.psn, func.max(Telemetry.sampled_at))
        .where(Telemetry.psn.in_(psns))
        .group_by(Telemetry.psn)
    ).all()
    return {psn: ts for psn, ts in rows if ts is not None}


def count_all(session: Session) -> int:
    return int(session.execute(select(func.count()).select_from(Telemetry)).scalar_one())


def relation_size_bytes(session: Session) -> int | None:
    """Heap + index của bảng telemetry. Rẻ hơn COUNT(*). None nếu không đọc được."""
    try:
        n = session.execute(text("SELECT pg_total_relation_size('telemetry')")).scalar_one()
    except Exception:
        return None
    return None if n is None else int(n)

