"""Dịch lỗi SMTP thành một câu người vận hành kho làm được.

Vì sao cần một module riêng cho việc này: màn hình Cài đặt trước đây trả nguyên
văn lỗi của thư viện, ví dụ ``SMTPAuthenticationError: (535, b"5.7.139
Authentication unsuccessful ... SmtpClientAuthentication is disabled for the
Tenant")``.

Người đọc thông báo đó là nhân viên vận hành kho LNG, không phải quản trị hệ
thống. Câu trên đúng về kỹ thuật nhưng KHÔNG nói họ phải làm gì, nên kết quả
thực tế là bỏ dở việc cấu hình. Riêng với Outlook/Microsoft 365 đây không phải
trường hợp hiếm: Microsoft tắt SMTP AUTH theo mặc định ở tổ chức mới, nên đó là
đường đi thường gặp NHẤT chứ không phải ngoại lệ.

Ba nguyên tắc:

1. **Chỉ dẫn đứng trước, chi tiết kỹ thuật đứng sau trong ngoặc.** Không bỏ chi
   tiết: khi phải nhờ bên IT thì đó là thứ duy nhất họ cần. Cắt nó đi là đổi một
   nhóm người dùng khó chịu sang nhóm khác.
2. **Khớp theo nội dung phản hồi của máy chủ, không theo loại ngoại lệ.** Cùng
   một ``SMTPAuthenticationError`` có thể là sai mật khẩu, là tổ chức tắt SMTP,
   hay là dùng mật khẩu thường thay cho mật khẩu ứng dụng — ba việc phải làm
   khác nhau.
3. **Hàm thuần, không import smtplib.** Nhận một ngoại lệ bất kỳ và tự đọc thuộc
   tính nếu có, nên kiểm thử được mà không cần máy chủ thư và không kéo tầng vận
   chuyển vào domain.
"""

from __future__ import annotations

#: Giới hạn phần chi tiết kỹ thuật. Phản hồi của một số máy chủ dài vài trăm ký
#: tự kèm URL; đặt nguyên vào một dòng thông báo sẽ đẩy phần chỉ dẫn ra khỏi màn
#: hình trên điện thoại, đúng lúc người ta cần đọc nó nhất.
MAX_DETAIL_CHARS = 240


def _haystack(exc: BaseException) -> str:
    """Gom mọi chỗ máy chủ có thể đã nhét mã lỗi vào, hạ về chữ thường.

    ``str(exc)`` một mình là không đủ: với ``SMTPResponseException`` phần chuỗi
    dễ đọc nằm ở ``smtp_error`` dạng bytes, và không phải phiên bản smtplib nào
    cũng đưa nó vào ``__str__`` theo cùng một cách.
    """
    parts = [str(exc)]
    code = getattr(exc, "smtp_code", None)
    if code is not None:
        parts.append(str(code))
    raw = getattr(exc, "smtp_error", None)
    if isinstance(raw, bytes):
        parts.append(raw.decode("utf-8", "replace"))
    elif raw is not None:
        parts.append(str(raw))
    # SMTPRecipientsRefused giữ lỗi của từng người nhận trong dict, không ở
    # smtp_error — bỏ qua chỗ này thì mọi lỗi người nhận rơi xuống nhánh mặc định.
    recips = getattr(exc, "recipients", None)
    if isinstance(recips, dict):
        for addr, val in recips.items():
            parts.append(str(addr))
            parts.append(str(val))
    return " ".join(parts).lower()


def _detail(exc: BaseException) -> str:
    text = " ".join(str(exc).split()) or exc.__class__.__name__
    if len(text) > MAX_DETAIL_CHARS:
        text = text[: MAX_DETAIL_CHARS - 1] + "…"
    return f"{type(exc).__name__}: {text}"


def _guidance(exc: BaseException, hay: str, name: str) -> str:
    """Chọn câu chỉ dẫn. Thứ tự là quan trọng: cụ thể trước, chung sau."""
    # --- Microsoft 365: tổ chức tắt SMTP AUTH. Phải đứng TRƯỚC mọi luật "sai mật
    # khẩu", vì máy chủ vẫn trả 535 y như khi sai mật khẩu, mà việc phải làm thì
    # hoàn toàn khác: không mật khẩu nào chữa được lỗi này.
    if "smtpclientauthentication is disabled" in hay or "smtp_auth_disabled" in hay:
        return (
            "Tổ chức Microsoft 365 của công ty đang KHOÁ gửi thư qua SMTP, nên "
            "không có mật khẩu nào đăng nhập được. Nhờ người quản trị Microsoft "
            "365 bật SMTP AUTH cho hộp thư này, hoặc dùng một hộp thư Gmail riêng "
            "cho cảnh báo."
        )
    if "5.7.139" in hay or "basic authentication is disabled" in hay:
        return (
            "Microsoft đã tắt cách đăng nhập bằng mật khẩu cho hộp thư này. Nhờ "
            "người quản trị Microsoft 365 mở lại SMTP AUTH, hoặc dùng một hộp thư "
            "Gmail riêng cho cảnh báo."
        )
    # --- Gmail: đã bật xác minh 2 bước nên mật khẩu thường không dùng được.
    if "application-specific password required" in hay or "5.7.9" in hay:
        return (
            "Gmail không nhận mật khẩu đăng nhập thường. Phải tạo Mật khẩu ứng "
            "dụng 16 chữ cái ở myaccount.google.com/apppasswords rồi dán vào ô "
            "Mật khẩu ứng dụng."
        )
    if "username and password not accepted" in hay:
        return (
            "Gmail từ chối cặp địa chỉ thư và mật khẩu ứng dụng. Kiểm tra lại địa "
            "chỉ Gmail, rồi dán lại Mật khẩu ứng dụng 16 chữ cái (không có dấu "
            "cách, và không phải mật khẩu đăng nhập Google)."
        )
    # --- Quyền gửi thay: From khác hộp thư đã đăng nhập.
    if "send as this sender" in hay or "5.7.60" in hay:
        return (
            "Máy chủ không cho gửi thư dưới địa chỉ khác với hộp thư đã đăng "
            "nhập. Mở Thiết lập nâng cao và đặt Địa chỉ gửi (From) đúng bằng địa "
            "chỉ thư đã khai ở Bước 2."
        )
    # --- Sai mật khẩu / sai tài khoản, các dạng còn lại.
    if "authentication unsuccessful" in hay or "5.7.3" in hay:
        return (
            "Máy chủ thư không nhận địa chỉ thư hoặc mật khẩu. Kiểm tra lại địa "
            "chỉ ở Bước 2 và nhập lại mật khẩu ứng dụng."
        )
    if name == "SMTPAuthenticationError" or "authentication failed" in hay:
        return (
            "Đăng nhập vào máy chủ thư thất bại. Nhập lại mật khẩu ứng dụng; nếu "
            "vẫn lỗi thì địa chỉ thư ở Bước 2 chưa đúng."
        )
    if "authentication required" in hay:
        return (
            "Máy chủ yêu cầu đăng nhập nhưng chưa có mật khẩu nào được lưu. Nhập "
            "Mật khẩu ứng dụng rồi bấm Lưu."
        )
    # --- Cấu hình bảo mật/cổng không khớp.
    if name == "SMTPNotSupportedError" or "starttls extension not supported" in hay:
        return (
            "Máy chủ này không dùng được kiểu bảo mật đang chọn. Mở Thiết lập nâng "
            "cao và đổi sang cặp còn lại: STARTTLS đi với cổng 587, SSL/TLS đi với "
            "cổng 465."
        )
    if name.startswith("SSL") or "wrong version number" in hay or "record layer" in hay:
        return (
            "Kiểu bảo mật không khớp với cổng. Trong Thiết lập nâng cao, chọn "
            "STARTTLS nếu cổng là 587, hoặc SSL/TLS nếu cổng là 465."
        )
    # --- Người nhận / người gửi bị từ chối.
    if name == "SMTPRecipientsRefused":
        return (
            "Máy chủ từ chối địa chỉ người nhận. Kiểm tra lại danh sách địa chỉ "
            "nhận cảnh báo ở khối phía trên."
        )
    if name == "SMTPSenderRefused":
        return (
            "Máy chủ từ chối địa chỉ gửi. Đặt Địa chỉ gửi (From) đúng bằng địa chỉ "
            "thư đã đăng nhập."
        )
    # --- Không kết nối được: sai tên máy chủ, sai cổng, hoặc mạng công ty chặn.
    if (
        name in {"gaierror", "SMTPConnectError"}
        or "name or service not known" in hay
        or "getaddrinfo failed" in hay
        or "nodename nor servname" in hay
    ):
        return (
            "Không tìm thấy máy chủ thư. Kiểm tra lại Địa chỉ máy chủ trong Thiết "
            "lập nâng cao; nếu đang chọn Gmail hoặc Outlook thì lỗi này thường do "
            "mạng chặn, hãy báo bên IT."
        )
    if (
        name in {"TimeoutError", "timeout", "SMTPServerDisconnected"}
        or "timed out" in hay
        or "refused" in hay
    ):
        return (
            "Không kết nối được tới máy chủ thư trong thời gian cho phép. Thường "
            "là mạng công ty chặn cổng gửi thư — nhờ bên IT mở cổng 587, hoặc thử "
            "cổng 465 trong Thiết lập nâng cao."
        )
    if isinstance(exc, OSError):
        return (
            "Không mở được kết nối tới máy chủ thư. Kiểm tra đường mạng và Địa chỉ "
            "máy chủ trong Thiết lập nâng cao."
        )
    return (
        "Chưa gửi được thư. Kiểm tra lại địa chỉ thư và mật khẩu ứng dụng ở Bước "
        "2; nếu vẫn lỗi, gửi nguyên dòng chi tiết bên dưới cho bên IT."
    )


def explain(exc: BaseException) -> str:
    """Một dòng: việc cần làm, rồi chi tiết kỹ thuật trong ngoặc.

    Luôn giữ phần chi tiết. Người vận hành đọc nửa đầu là đủ; khi phải chuyển việc
    cho bên IT thì nửa sau là toàn bộ thông tin họ cần, và người vận hành không
    phải mô tả lại lỗi bằng lời của mình.
    """
    return (
        f"{_guidance(exc, _haystack(exc), type(exc).__name__)} "
        f"(Chi tiết: {_detail(exc)})"
    )
