"""Dữ liệu mẫu cho e2e: đủ để MỌI màn hình có cái để hiện, và sát vận hành thật.

* ``YKH-TANK-01`` — bồn đang báo số: 30 ngày số đo mỗi 30 phút, tiêu thụ 6 m³/ngày
  (Chủ Nhật nhà máy nghỉ), xe tới lúc 14:00 khi bồn xuống dưới 12 m³.
* ``2604200016`` — bồn ngoại tuyến 69 ngày (thiết bị chết), để thấy nhánh "số liệu cũ".
* Thông số kế hoạch đã lưu, một ngày nghỉ sắp tới, báo động nhà máy (cùng việc,
  khác số đo — để thấy phần gộp), và một lần thu thập thành công cho health.

Mọi mốc thời gian tính TỪ LÚC CHẠY, nên bộ test không bao giờ "hết hạn".
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.db.models import IngestRun, PlanDayFlag, PlanSetting, Telemetry, Terminal
from app.domain.contracts import NormalizedAlarm
from app.repositories import vendor_alarms as alarm_repo

UTC = ZoneInfo("UTC")
VN = ZoneInfo("Asia/Ho_Chi_Minh")

YKH = "YKH-TANK-01"
FUJI = "2604200016"
CUSTOMER = "Khách hàng E2E"
CAPACITY_L = 60_000.0
USE_L_PER_DAY = 6_000.0
REFILL_BELOW_L = 12_000.0
REFILL_TO_L = 54_000.0


def rest_day(today: date) -> date:
    """Ngày nghỉ được đánh dấu sẵn: thứ Ba của tuần sau."""
    d = today + timedelta(days=7)
    return d + timedelta(days=(1 - d.weekday()) % 7)


def seed(engine: Engine) -> dict[str, object]:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    today = now.astimezone(VN).date()
    refills: list[datetime] = []
    with Session(engine) as s:
        ykh = Terminal(
            id=uuid.uuid4(), psn=YKH, name="Bồn LNG - YKH-TANK-01", capacity_l=Decimal("60000"),
            medium_name="LNG", status="online", last_seen_at=now - timedelta(minutes=5),
            latitude=Decimal("10.932519"), longitude=Decimal("106.734816"),
        )
        fuji = Terminal(
            id=uuid.uuid4(), psn=FUJI, name="Bồn LNG - Fuji Seal", capacity_l=Decimal("10425"),
            medium_name="LNG", status="offline", last_seen_at=now - timedelta(days=69),
            latitude=Decimal("10.971047"), longitude=Decimal("106.750161"),
        )
        s.add_all([ykh, fuji])
        s.flush()

        # 30 ngày, mỗi 30 phút, kết thúc 5 phút trước.
        at = now - timedelta(days=30)
        vol = 40_000.0
        step = USE_L_PER_DAY / 48
        rows = []
        while at <= now - timedelta(minutes=5):
            local = at.astimezone(VN)
            if local.weekday() != 6:                      # Chủ Nhật nhà máy nghỉ
                vol -= step
            if vol < REFILL_BELOW_L and local.weekday() != 6 and local.hour == 14 and local.minute == 0:
                vol = REFILL_TO_L
                refills.append(at)
            rows.append(Telemetry(
                terminal_id=ykh.id, psn=YKH, sampled_at=at,
                volume_l=Decimal(str(round(vol, 1))),
                volume_percent=Decimal(str(round(vol / CAPACITY_L * 100, 3))),
                pressure_mpa=Decimal("0.42"), temperature_c=Decimal("30"),
                source="e2e", raw_payload={},
            ))
            at += timedelta(minutes=30)
        rows.append(Telemetry(
            terminal_id=fuji.id, psn=FUJI, sampled_at=now - timedelta(days=69),
            volume_l=Decimal("61"), volume_percent=Decimal("0.59"), pressure_mpa=Decimal("0.07"),
            battery_v=Decimal("3.6"), signal_percent=Decimal("20"), source="e2e", raw_payload={},
        ))
        s.add_all(rows)

        s.add(PlanSetting(
            psn=YKH, max_fill_percent=Decimal("90"), daily_use_l=Decimal("6000"),
            reserve_l=Decimal("10000"), refill_time=time(8, 0), horizon_days=60,
            customer_name=CUSTOMER,
        ))
        s.add(PlanDayFlag(psn=YKH, flag_date=rest_day(today), rest=True, forced=False,
                          no_delivery=True, updated_by="e2e"))
        s.add(IngestRun(
            trigger="scheduler", status="success", started_at=now - timedelta(minutes=6),
            finished_at=now - timedelta(minutes=5), fetched=10, inserted=10,
            duplicates=0, terminals_created=0, error_count=0, params={}, mapping_report={},
        ))
        s.flush()

        msg = "Mức bồn LT1: Refuelling – Nhiên liệu thấp hơn mức dự trữ (yêu cầu nạp): {} m3"
        alarms = [
            NormalizedAlarm(source="e2e", site_code="YKH", device_id="LT1",
                            raised_at=now - timedelta(hours=h), vendor_ts_raw=f"t{h}",
                            message=msg.format(v), symbol="danger")
            for h, v in ((1, "14.897530555725"), (2, "9.35400390625"), (3, "5.9563598632812"),
                         (4, "6.1351833343506"))
        ] + [
            NormalizedAlarm(source="e2e", site_code="YKH", device_id="SV4",
                            raised_at=now - timedelta(hours=h), vendor_ts_raw=f"s{h}",
                            message="Van SV4: Lỗi mở van SV4", symbol="danger")
            for h in (5, 6)
        ]
        alarm_repo.bulk_insert(s, alarms)
        s.commit()
    return {"now": now, "today": today, "refills": refills, "rest_day": rest_day(today)}
