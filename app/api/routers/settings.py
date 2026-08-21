"""Cài đặt vận hành: đọc, sửa, và GỬI THỬ email ngay trong app.

Vì sao có file này: trước đó email nhận cảnh báo và các ngưỡng đều là biến môi
trường trên Vercel — đổi một địa chỉ phải sửa env rồi redeploy. Với thứ thay đổi
thường xuyên đó là thiết kế sai; cấu hình vận hành phải bấm được.

Giới hạn có ý thức: **credential SMTP vẫn phải do người dùng tạo** (Gmail app
password, API key...). Không nhà cung cấp mail nào cho gửi mà không có credential,
nên bước đó không tự động hoá được. Phần còn lại — ai nhận, bao lâu gửi lại, gửi
thử — thì nằm ở đây.

Quyền: ``UserDep`` (đã đăng nhập dashboard), KHÔNG phải ``AdminDep``. Đánh đổi
được nói rõ: ai đăng nhập được dashboard thì đổi được nơi cảnh báo gửi tới và
lưu được mật khẩu ứng dụng của hộp thư. Với một tool nội bộ dùng chung một tài
khoản cổng telemetry, mức tin cậy đó bằng với mức "xem được toàn bộ số liệu bồn".
Khi nào có nhiều người dùng với vai trò khác nhau thì siết lại thành AdminDep.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, status

from app.api.deps import SessionDep, SettingsDep, UserDep
from app.api.schemas import ActionOut, SettingsIn, SettingsOut
from app.repositories import app_settings as store
from app.services import appconfig, notifier

log = logging.getLogger(__name__)
router = APIRouter(prefix="/settings", tags=["settings"])
UTC = ZoneInfo("UTC")


def _out(session: SessionDep, settings: SettingsDep) -> SettingsOut:
    cfg = appconfig.load(session, settings)
    updated_at, updated_by = store.meta(session)
    return SettingsOut(
        values=cfg.public_values(),
        sources=cfg.sources(),
        smtp_password_set=cfg.has_secret("smtp_password"),
        smtp_ready=cfg.smtp_ready,
        smtp_blocked_reason=_why_blocked(cfg),
        updated_at=updated_at,
        updated_by=updated_by,
    )


def _why_blocked(cfg: appconfig.EffectiveConfig) -> str | None:
    """Nói CHÍNH XÁC thiếu gì, thay vì một cờ false không giải thích.

    Đây là màn hình mà người dùng đến khi "email không chạy"; trả về đúng mảnh
    còn thiếu tiết kiệm cho họ một vòng thử-và-đoán.
    """
    if not cfg.notify_enabled:
        return "Đang tắt gửi thông báo"
    if not cfg.smtp_host:
        return "Chưa có máy chủ SMTP"
    if not (cfg.smtp_from or cfg.smtp_user):
        return "Chưa có địa chỉ gửi (From hoặc User)"
    if not cfg.alert_email_list:
        return "Chưa có địa chỉ nhận"
    if not cfg.has_secret("smtp_password"):
        # KHÔNG chặn: một số máy chủ SMTP nội bộ không cần xác thực. Chỉ nhắc.
        return "Chưa có mật khẩu ứng dụng — chỉ đúng nếu máy chủ SMTP không cần xác thực"
    return None


@router.get("", response_model=SettingsOut)
def get_settings(session: SessionDep, settings: SettingsDep, _: UserDep) -> SettingsOut:
    return _out(session, settings)


@router.patch("", response_model=SettingsOut)
def patch_settings(
    body: SettingsIn, session: SessionDep, settings: SettingsDep, user: UserDep
) -> SettingsOut:
    """Chỉ ghi những field CÓ trong request.

    ``exclude_unset=True`` là mấu chốt: nó phân biệt "không gửi field" (giữ nguyên)
    với "gửi null" (xoá override, trả về giá trị .env). Nếu dùng ``model_dump()``
    thường thì mọi field không gửi sẽ thành None và một form chỉ sửa email sẽ âm
    thầm xoá sạch phần cấu hình vận hành.
    """
    patch = body.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "không có field nào để lưu"
        )
    # Chuỗi rỗng = xoá override (người dùng bôi trống một ô rồi lưu). Không quy đổi
    # thì "" sẽ được lưu như một giá trị thật và ghi đè .env bằng chuỗi trắng.
    for k, v in list(patch.items()):
        if isinstance(v, str) and not v.strip():
            patch[k] = None
    store.save(session, patch, by=user)
    session.commit()
    log.info("settings: %s đã cập nhật %s", user, sorted(patch))
    return _out(session, settings)


@router.post("/test-email", response_model=ActionOut)
def test_email(session: SessionDep, settings: SettingsDep, user: UserDep) -> ActionOut:
    """Gửi một email thử tới đúng danh sách người nhận đang lưu.

    KHÔNG đi qua cửa chặn gửi lại và KHÔNG ghi vào nhật ký thông báo: đây là hành
    động kiểm tra cấu hình, không phải một cảnh báo. Nhập nó vào nhật ký sẽ làm
    bản ghi kiểm toán lẫn thư rác thử nghiệm, và nếu tính vào cửa chặn thì một lần
    bấm thử sẽ làm cảnh báo thật im lặng suốt 12 giờ sau đó.
    """
    cfg = appconfig.load(session, settings)
    reason = _why_blocked(cfg)
    if not cfg.smtp_ready:
        return ActionOut(ok=False, message=reason or "Cấu hình SMTP chưa đủ")

    now = datetime.now(tz=UTC)
    local = now.astimezone(cfg.tzinfo)
    subject = "[THỬ] Hệ thống theo dõi bồn LNG — cấu hình email hoạt động"
    body = "\n".join([
        f"Email thử do {user} gửi lúc {local:%d/%m/%Y %H:%M:%S} ({cfg.app_tz}).",
        "",
        "Nếu bạn đọc được thư này thì cảnh báo thật sẽ tới đúng địa chỉ:",
        *[f"  - {a}" for a in cfg.alert_email_list],
        "",
        f"Máy chủ gửi: {cfg.smtp_host}:{cfg.smtp_port}",
        f"Không gửi lại cùng một cảnh báo trong {cfg.alert_resend_hours} giờ.",
        "",
        "Đây là thư thử, không phải cảnh báo.",
    ])
    try:
        notifier.send_email(cfg, subject, body)
    except Exception as exc:
        # Trả nguyên loại lỗi + thông điệp SMTP: đây là màn hình cấu hình, người
        # dùng CẦN biết "sai mật khẩu" khác "sai cổng". Không có tên vendor
        # telemetry nào trong chuỗi này nên không vi phạm nguyên tắc chống rò.
        log.warning("settings: gửi email thử thất bại: %s", exc)
        return ActionOut(ok=False, message=f"{type(exc).__name__}: {exc}")
    return ActionOut(
        ok=True,
        message=f"Đã gửi thư thử tới {', '.join(cfg.alert_email_list)}",
    )
