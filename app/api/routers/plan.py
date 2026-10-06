"""Số đo tay của trang Kế hoạch: ``/api/plan/readings/...``

Vì sao có endpoint này. Kế hoạch nạp là một chuỗi số học từ *một* mức khởi đầu:
mỗi ngày trừ đi mức tiêu thụ bình quân. Mức bình quân thì đúng trên cả tháng
nhưng sai mỗi ngày — xưởng chạy ít thì cuối ngày còn 48 m³ chứ không phải 46,60
m³ như công thức. Không có đường nhập số thực tế, người vận hành buộc phải sửa
"thể tích ban đầu" rồi dịch "ngày bắt đầu": mất lịch sử những ngày trước, và mất
luôn thứ họ cần nhất là so ước tính với thực tế.

Phạm vi đã chốt: số này CHỈ dùng cho trang Kế hoạch. Dashboard, dự báo, mức tiêu
thụ đo được, nhận diện lần nạp và cảnh báo vẫn chỉ đọc ``telemetry``, tức chỉ đọc
số của thiết bị. Nhờ vậy không có chỗ nào trong hệ thống pha số người vào số máy.

Đơn vị là **lít** như mọi field thể tích khác của API. UI quy đổi m³ ở biên.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.api.deps import SessionDep, SettingsDep, UserDep
from app.api.schemas import (
    PlanDayFlagIn,
    PlanDayFlagOut,
    PlanExportIn,
    PlanReadingIn,
    PlanReadingOut,
    PlanSettingsIn,
    PlanSettingsOut,
)
from app.domain import forecast as fc
from app.repositories import plan_readings as pr_repo
from app.repositories import telemetry as tel_repo
from app.repositories import terminals as term_repo
from app.services import plan_export

router = APIRouter(prefix="/plan", tags=["plan"])

#: Trần tuyệt đối, KHÔNG theo dung tích lưu trong DB. 1.000 m³ là ngưỡng "chắc chắn
#: gõ sai đơn vị" (thêm một số 0, hoặc nhập lít vào ô m³) mà vẫn không cản trở ca
#: dùng thật nào — bồn LNG ở đây cỡ vài chục m³.
#:
#: Vì sao KHÔNG chặn theo ``terminals.capacity_l``: bản đầu có chặn, và nó đã chặn
#: đúng một lần nhập hợp lệ trên production. `capacity_l` được ingest từ vendor
#: (``cylinderVolume``) và với bồn Fuji Seal nó là 10425 L trong khi bồn thật là
#: 54 m³ — người vận hành gõ 42 m³ thì bị từ chối 422 kèm một thông điệp nói rằng
#: chính số đo của họ là sai. Một con số vendor có thể sai không được phép phủ quyết
#: số người vận hành TỰ ĐO. Cảnh báo vượt dung tích nằm ở client, nơi có con số
#: "Dung tích" mà chính người dùng đang lập kế hoạch với, và nó CẢNH BÁO chứ không chặn.
FALLBACK_MAX_L = 1_000_000


def _require_terminal(session: SessionDep, psn: str) -> None:
    if term_repo.get_by_psn(session, psn) is None:
        # 404 chứ không phải 200 rỗng: PSN gõ sai phải phân biệt được với "bồn này
        # chưa nhập số nào". Cùng luật với /api/terminals/{psn}.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Terminal not found")


@router.get("/readings/{psn}", response_model=list[PlanReadingOut])
def list_readings(
    psn: str,
    session: SessionDep,
    _: UserDep,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: Annotated[date | None, Query()] = None,
) -> list[PlanReadingOut]:
    """Các số đo tay đã lưu của một bồn, cũ trước mới sau."""
    _require_terminal(session, psn)
    if from_ is not None and to is not None and from_ > to:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "from phải <= to")
    rows = pr_repo.list_for(session, psn, start=from_, end=to)
    return [PlanReadingOut.model_validate(r) for r in rows]


@router.put("/readings/{psn}/{day}", response_model=PlanReadingOut)
def put_reading(
    psn: str,
    day: date,
    body: PlanReadingIn,
    session: SessionDep,
    user: UserDep,
) -> PlanReadingOut:
    """Ghi số đo của một ngày. Gửi lại cùng ngày là ghi đè.

    PUT chứ không POST: địa chỉ ``(bồn, ngày)`` xác định đúng một số đo, nên lệnh
    này idempotent — bấm Lưu hai lần không được sinh ra hai dòng.
    """
    _require_terminal(session, psn)

    # Chỉ trần TUYỆT ĐỐI. Xem FALLBACK_MAX_L: chặn theo capacity_l của DB đã từ chối
    # một lần nhập hợp lệ trên production vì chính capacity_l sai.
    if float(body.volume_l) > FALLBACK_MAX_L:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Thể tích {body.volume_l} L vượt ngưỡng hợp lý ({FALLBACK_MAX_L:g} L) — "
            "kiểm lại đơn vị, giá trị này tính bằng lít",
        )

    row = pr_repo.upsert(session, psn, day, body.volume_l, by=user)
    session.commit()
    return PlanReadingOut.model_validate(row)


@router.delete("/readings/{psn}/{day}", status_code=status.HTTP_204_NO_CONTENT)
def delete_reading(psn: str, day: date, session: SessionDep, _: UserDep) -> Response:
    """Xoá số đo của một ngày — quay về dùng số ước tính cho ngày đó."""
    _require_terminal(session, psn)
    if not pr_repo.delete(session, psn, day):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ngày này chưa có số đo tay")
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/flags/{psn}", response_model=list[PlanDayFlagOut])
def list_flags(
    psn: str,
    session: SessionDep,
    _: UserDep,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: Annotated[date | None, Query()] = None,
) -> list[PlanDayFlagOut]:
    """Các ngày đã đánh dấu nghỉ / nạp chỉ định của một bồn."""
    _require_terminal(session, psn)
    if from_ is not None and to is not None and from_ > to:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "from phải <= to")
    rows = pr_repo.list_flags(session, psn, start=from_, end=to)
    return [PlanDayFlagOut.model_validate(r) for r in rows]


@router.put("/flags/{psn}/{day}", response_model=PlanDayFlagOut)
def put_flags(
    psn: str,
    day: date,
    body: PlanDayFlagIn,
    session: SessionDep,
    user: UserDep,
) -> PlanDayFlagOut:
    """Đặt cờ cho một ngày. Bỏ cả hai tích là xoá đánh dấu của ngày đó.

    PUT chứ không POST, cùng lý do như ``put_reading``: địa chỉ ``(bồn, ngày)``
    xác định đúng một bộ cờ, nên lệnh này idempotent.

    Bỏ cả hai tích vẫn trả 200 kèm ``rest=false, forced=false`` chứ không phải 404:
    ở đây "ngày này không đánh dấu gì" là một trạng thái hợp lệ và chính là thứ
    người dùng vừa yêu cầu, khác hẳn ``delete_reading`` nơi xoá một ngày vốn không
    có số là một lệnh không làm gì và đáng báo lỗi.
    """
    _require_terminal(session, psn)
    row = pr_repo.set_flags(
        session,
        psn,
        day,
        rest=body.rest,
        forced=body.forced,
        no_delivery=body.no_delivery,
        by=user,
    )
    session.commit()
    if row is None:
        return PlanDayFlagOut(
            psn=psn, flag_date=day, rest=False, forced=False, no_delivery=False
        )
    return PlanDayFlagOut.model_validate(row)


@router.get("/settings/{psn}", response_model=PlanSettingsOut)
def get_plan_settings(psn: str, session: SessionDep, _: UserDep) -> PlanSettingsOut:
    """Thông số lập kế hoạch đã lưu của một bồn.

    Chưa lưu gì thì trả một bản ghi toàn ``null`` chứ KHÔNG phải 404: "bồn này chưa
    ai đặt thông số" là trạng thái bình thường của mọi bồn mới, còn 404 sẽ buộc client
    phải phân biệt hai ca không khác nhau về hành vi.
    """
    _require_terminal(session, psn)
    row = pr_repo.get_settings_for(session, psn)
    if row is None:
        return PlanSettingsOut(psn=psn)
    return PlanSettingsOut.model_validate(row)


@router.put("/settings/{psn}", response_model=PlanSettingsOut)
def put_plan_settings(
    psn: str,
    body: PlanSettingsIn,
    session: SessionDep,
    user: UserDep,
) -> PlanSettingsOut:
    """Lưu thông số lập kế hoạch. Chỉ ghi những field được gửi."""
    _require_terminal(session, psn)
    patch = body.model_dump(include=body.model_fields_set)
    row = pr_repo.save_settings(session, psn, patch, by=user)
    session.commit()
    return PlanSettingsOut.model_validate(row)


_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _actuals(
    session: SessionDep,
    psn: str,
    *,
    capacity_l: float | None,
    from_day: date,
    to_day: date,
    now: datetime,
    tz: ZoneInfo,
    rest: set[date],
) -> tuple[list[plan_export.ActualRefill], list[plan_export.DayRow]]:
    """Phần THẬT của báo cáo, đọc thẳng từ số đo: các lần nạp và từng ngày đã qua.

    Lần nạp dùng đúng ``detect_refills`` của dự báo và nhật ký nạp, trên chuỗi gộp
    30 phút như ở đó — ba chỗ không thể ra ba con số khác nhau cho cùng một xe.
    Đầu ngày là ``daily_open`` — cùng nguồn với cột "đo thật" của trang Kế hoạch.
    """
    today = now.date()
    if from_day > today:
        return [], []
    end_day = min(to_day, today)
    start = datetime.combine(from_day, time(), tz)
    end = min(datetime.combine(end_day + timedelta(days=1), time(), tz), now)
    rows = tel_repo.series_with_gas(
        session, psn, start - timedelta(hours=12), end, bucket_minutes=30
    )
    samples = [
        fc.Sample(at=at, volume_l=v, pressure_mpa=p, totalizer_nm3=g) for at, v, p, g in rows
    ]
    refills = [
        plan_export.ActualRefill(
            at=e.at.astimezone(tz), before_m3=e.before_l / 1000, after_m3=e.after_l / 1000
        )
        for e in fc.detect_refills(samples, capacity_l=capacity_l)
        if from_day <= e.at.astimezone(tz).date() <= end_day
    ]
    refill_by_day: dict[date, float] = {}
    for a in refills:
        refill_by_day[a.at.date()] = refill_by_day.get(a.at.date(), 0.0) + a.m3
    opens = {
        d: v / 1000
        for d, _, v in tel_repo.daily_open(
            session, psn, from_day, end_day + timedelta(days=1), tz_name=str(tz)
        )
    }
    # Cuối ngày khi hôm sau không có lần đo đầu ngày (bồn im qua đêm): lần đo cuối
    # cùng của chính ngày đó.
    last_of_day: dict[date, float] = {}
    for smp in samples:
        if smp.volume_l is not None:
            last_of_day[smp.at.astimezone(tz).date()] = smp.volume_l / 1000

    days: list[plan_export.DayRow] = []
    d = from_day
    while d <= end_day:
        nxt = d + timedelta(days=1)
        close = None if d >= today else opens.get(nxt, last_of_day.get(d))
        notes = []
        if d in rest:
            notes.append("Ngày nghỉ")
        if d not in opens:
            notes.append("Không có số đo")
        elif d >= today:
            notes.append("Hôm nay — chưa hết ngày")
        days.append(
            plan_export.DayRow(
                day=d, open_m3=opens.get(d), close_m3=close,
                refill_m3=refill_by_day.get(d, 0.0), rest=d in rest, note="; ".join(notes),
            )
        )
        d = nxt
    return refills, days


@router.post("/export/{psn}", response_class=Response)
def export_refills_xlsx(
    psn: str, body: PlanExportIn, session: SessionDep, settings: SettingsDep, _: UserDep
) -> Response:
    """Báo cáo Excel 3 sheet: Lịch nạp, Nhật ký nạp thật, Tiêu thụ theo ngày.

    POST vì lịch KẾ HOẠCH do trang gửi lên: nó được tính ở trình duyệt, từ đúng
    những ô tích và số đo mà người dùng đang nhìn. Phần THẬT — lần nạp đã qua,
    thể tích từng ngày, ngày nghỉ — server tự đọc, không tin trang.
    """
    term = term_repo.get_by_psn(session, psn)
    if term is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Terminal not found")
    tz = ZoneInfo(settings.app_tz)
    now = datetime.now(tz)
    st = pr_repo.get_settings_for(session, psn)
    rest = {
        f.flag_date
        for f in pr_repo.list_flags(session, psn, start=body.from_day, end=body.to_day)
        if f.rest or f.no_delivery
    }
    refills, days = _actuals(
        session, psn,
        capacity_l=None if term.capacity_l is None else float(term.capacity_l),
        from_day=body.from_day, to_day=body.to_day, now=now, tz=tz, rest=rest,
    )
    schedule = plan_export.build_schedule(
        [plan_export.PlannedRow(day=r.day, m3=float(r.m3), forced=r.forced) for r in body.rows],
        refills, sorted(rest),
        from_day=body.from_day, to_day=body.to_day, today=now.date(),
    )
    meta = plan_export.ReportMeta(
        tank_name=term.name or psn, psn=psn,
        customer=(body.customer or "").strip()
        or (st.customer_name if st is not None else None),
        from_day=body.from_day, to_day=body.to_day,
        generated_at=now.replace(tzinfo=None), basis=body.basis,
    )
    content = plan_export.build_report(schedule, refills, days, meta)
    # Tên file chỉ ASCII: PSN là chuỗi từ URL, và header Content-Disposition
    # không phải chỗ để thử xem trình duyệt nào chịu ký tự lạ.
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", psn)
    name = f"lich_nap_{safe}_{body.from_day:%Y%m%d}_{body.to_day:%Y%m%d}.xlsx"
    return Response(
        content=content,
        media_type=_XLSX,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
