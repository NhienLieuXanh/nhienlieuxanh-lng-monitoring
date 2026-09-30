"""Các lỗi tìm được khi test e2e thủ công trên production, 30/09/2026.

#1 — Trang Cài đặt báo "Cấu hình đầy đủ" cho Outlook chưa có mật khẩu: không thư
     cảnh báo nào gửi được mà màn hình nói là ổn.
#2 — Lịch giao cắt lượng đặt bằng tải xe: bồn cần 41 m³, xe 20 m³ -> "1 chuyến
     20 m³", nửa còn lại biến mất. Và mặc định 20 m³ là nhầm "20 tấn" (≈ 44 m³).
#7 — "51 dòng thô -> 51 việc (gộp 0%)": cùng một báo động "nhiên liệu thấp" không
     gộp được vì nguồn gắn số đo khác nhau vào mỗi dòng.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from app.api.routers.settings import _why_blocked
from app.config import Settings
from app.domain.contracts import NormalizedAlarm
from app.domain.forecast import build_forecast, plan_trips
from app.domain.smtp_errors import host_requires_auth
from app.repositories import vendor_alarms as repo
from app.services.appconfig import EffectiveConfig

UTC = ZoneInfo("UTC")
VN = ZoneInfo("Asia/Ho_Chi_Minh")


# --------------------------------------------------------------------- #1 SMTP
def _env(**kw: object) -> Settings:
    base: dict[str, object] = {
        "notify_enabled": True, "smtp_host": "smtp-mail.outlook.com",
        "smtp_from": "bot@outlook.com", "alert_email_to": "ops@x.com", "smtp_password": "",
    }
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


def test_may_chu_cong_cong_bat_dang_nhap() -> None:
    for h in ("smtp.gmail.com", "smtp-mail.outlook.com", "SMTP.Office365.com.", "smtp-relay.brevo.com"):
        assert host_requires_auth(h), h
    assert not host_requires_auth("mail.congty.local")
    assert not host_requires_auth(None)


def test_outlook_thieu_mat_khau_la_CHUA_san_sang() -> None:
    """Đúng cấu hình đo được trên production 30/09: Outlook, không mật khẩu."""
    assert _env().smtp_ready is False
    assert EffectiveConfig(_env()).smtp_ready is False
    assert _env(smtp_password="x").smtp_ready is True
    assert EffectiveConfig(_env(), {"smtp_password": "x"}).smtp_ready is True
    why = _why_blocked(EffectiveConfig(_env()))
    assert why is not None and "bắt buộc đăng nhập" in why and "chưa gửi được" in why


def test_may_chu_noi_bo_khong_mat_khau_van_san_sang() -> None:
    """Máy chủ nội bộ không cần xác thực: giữ hành vi cũ, chỉ nhắc chứ không chặn."""
    cfg = EffectiveConfig(_env(smtp_host="mail.congty.local"))
    assert cfg.smtp_ready is True
    assert "chỉ phù hợp nếu" in (_why_blocked(cfg) or "")


# --------------------------------------------------------------- #2 chia chuyến
T0 = datetime(2026, 9, 30, 11, 0, tzinfo=VN)


def _stub(psn: str, days: float, order: float):
    f = build_forecast([], psn=psn, volume_l=29000.0, capacity_l=60000.0,
                       pressure_mpa=0.4, now=T0, tz=VN, reading_at=T0)
    return replace(f, runout=replace(f.runout, days_to_reserve=days),
                   suggestion=replace(f.suggestion, order_l=order))


def test_luong_dat_lon_hon_mot_xe_thi_chia_chuyen_khong_cat() -> None:
    trips = plan_trips([_stub("YKH", 3.3, 41165.0)], truck_capacity_l=20000.0)
    stops = [s for t in trips for s in t.stops]
    assert sum(s.order_l for s in stops) == pytest.approx(41165.0), "mất hàng"
    assert [s.order_l for s in stops] == pytest.approx([20000.0, 20000.0, 1165.0])
    assert [(s.part, s.parts) for s in stops] == [(1, 3), (2, 3), (3, 3)]
    assert all(t.total_l <= t.truck_capacity_l + 1e-6 for t in trips)


def test_vua_mot_xe_thi_mot_phan() -> None:
    trips = plan_trips([_stub("YKH", 3.3, 41165.0)], truck_capacity_l=44000.0)
    assert len(trips) == 1 and trips[0].stops[0].parts == 1


def test_mac_dinh_xe_la_20_tan_khoang_44_m3() -> None:
    assert Settings().truck_capacity_l == 44000.0


# --------------------------------------------------------------- #7 gộp báo động
def test_mau_cau_bo_so_do_nhung_giu_ten_thiet_bi() -> None:
    a = "Mức bồn LT1: Nhiên liệu thấp hơn mức dự trữ (yêu cầu nạp): 14.897530555725 m3"
    b = "Mức bồn LT1: Nhiên liệu thấp hơn mức dự trữ (yêu cầu nạp): 5.9563598632812 m3"
    assert repo.message_template(a) == repo.message_template(b)
    assert "LT1" in repo.message_template(a) and "m3" in repo.message_template(a)
    assert repo.message_template("Van SV4: loi") != repo.message_template("Van SV3: loi")


def _alarm(device: str, message: str, at: datetime) -> NormalizedAlarm:
    return NormalizedAlarm(source="tst", site_code="TST", device_id=device, raised_at=at,
                           vendor_ts_raw=at.strftime("%d/%m/%Y %H:%M:%S"),
                           message=message, symbol="danger")


@pytest.mark.db
def test_bao_dong_chi_khac_so_do_la_MOT_viec(session: Session) -> None:
    now = datetime.now(tz=UTC)
    msg = "Mức bồn LT1: Nhiên liệu thấp hơn mức dự trữ (yêu cầu nạp): {} m3"
    vals = ["14.897530555725", "9.35400390625", "5.9563598632812", "6.1351833343506"]
    repo.bulk_insert(session, [
        _alarm("LT1", msg.format(v), now - timedelta(minutes=30 * (i + 1)))
        for i, v in enumerate(vals)
    ] + [_alarm("SV4", "Van SV4: loi dong van", now - timedelta(minutes=5))])
    session.flush()

    eps, raw = repo.summarize(session, start=now - timedelta(hours=24), end=now, site_code="TST")
    assert raw == 5
    assert len(eps) == 2
    lt1 = next(e for e in eps if e.device_id == "LT1")
    assert lt1.count == 4
    assert lt1.message.endswith("5.96–14.9 m3"), lt1.message
    assert lt1.first_raised_at < lt1.last_raised_at
    # Khoá chặn-gửi-lại theo MẪU: số đo mới không thành việc mới.
    assert lt1.message_hash == repo.message_hash(repo.message_template(msg.format("1")))
    assert eps[0].device_id == "SV4", "việc mới nhất lên đầu"
