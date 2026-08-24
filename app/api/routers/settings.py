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
from app.domain.smtp_errors import explain as explain_smtp
from app.repositories import app_settings as store
from app.services import notifier
from app.services.appconfig import EffectiveConfig, load_config

log = logging.getLogger(__name__)
router = APIRouter(prefix="/settings", tags=["settings"])
UTC = ZoneInfo("UTC")


def _out(session: SessionDep, settings: SettingsDep) -> SettingsOut:
    cfg = load_config(session, settings)
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


def _why_blocked(cfg: EffectiveConfig) -> str | None:
    """Nói CHÍNH XÁC thiếu gì, thay vì một cờ false không giải thích.

    Đây là màn hình mà người dùng đến khi "email không chạy"; trả về đúng mảnh
    còn thiếu tiết kiệm cho họ một vòng thử-và-đoán.
    """
    if not cfg.notify_enabled:
        return "Trạng thái gửi cảnh báo đang tắt"
    if not cfg.smtp_host:
        return "Chưa khai báo địa chỉ máy chủ thư"
    if not (cfg.smtp_from or cfg.smtp_user):
        return "Chưa khai báo địa chỉ gửi hoặc tài khoản đăng nhập"
    if not cfg.alert_email_list:
        return "Chưa khai báo địa chỉ nhận"
    if not cfg.has_secret("smtp_password"):
        # KHÔNG chặn: một số máy chủ SMTP nội bộ không cần xác thực. Chỉ nhắc.
        return "Chưa có mật khẩu ứng dụng — chỉ phù hợp nếu máy chủ thư không yêu cầu xác thực"
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
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Không có thông số nào để lưu"
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
    cfg = load_config(session, settings)
    reason = _why_blocked(cfg)
    if not cfg.smtp_ready:
        return ActionOut(ok=False, message=reason or "Cấu hình máy chủ thư chưa đầy đủ")

    now = datetime.now(tz=UTC)
    local = now.astimezone(cfg.tzinfo)
    subject = "[KIỂM TRA] Hệ thống giám sát bồn LNG — cấu hình thư điện tử hoạt động"
    body = "\n".join([
        f"Thư kiểm tra do {user} gửi lúc {local:%d/%m/%Y %H:%M:%S} (giờ {cfg.app_tz}).",
        "",
        "Nhận được thư này nghĩa là cảnh báo của hệ thống sẽ tới đúng các địa chỉ sau:",
        *[f"  - {a}" for a in cfg.alert_email_list],
        "",
        f"Máy chủ thư: {cfg.smtp_host}, cổng {cfg.smtp_port}",
        f"Chu kỳ nhắc lại cùng một cảnh báo: {cfg.alert_resend_hours} giờ.",
        "",
        "Đây là thư kiểm tra cấu hình, không phải cảnh báo.",
    ])
    try:
        notifier.send_email(cfg, subject, body)
    except Exception as exc:
        # Dịch thành việc-cần-làm rồi mới kèm chi tiết kỹ thuật. Trả nguyên văn
        # lỗi smtplib là đúng về kỹ thuật nhưng vô dụng với người vận hành kho:
        # "535 5.7.139 SmtpClientAuthentication is disabled for the Tenant" và
        # "sai mật khẩu" đọc y như nhau, trong khi việc phải làm thì khác hẳn.
        log.warning("settings: gửi email thử thất bại: %s", exc)
        return ActionOut(ok=False, message=explain_smtp(exc))
    return ActionOut(
        ok=True,
        message=f"Đã gửi thư kiểm tra tới {', '.join(cfg.alert_email_list)}",
    )
