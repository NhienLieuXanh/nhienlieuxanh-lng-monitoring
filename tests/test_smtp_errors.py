"""Test dịch lỗi SMTP. Không cần máy chủ thư, không cần DB — hàm thuần.

Giá trị của bộ test này nằm ở thứ tự luật, không ở việc từng luật khớp. Ba lỗi
Microsoft trả về cùng mã 535 nhưng ứng với ba việc phải làm khác nhau; nếu một
luật chung chặn trước luật cụ thể thì hàm vẫn "chạy đúng" mà vẫn dẫn người dùng
đi thử lại một việc bất khả thi.
"""

from __future__ import annotations

import smtplib
import socket

from app.domain.smtp_errors import MAX_DETAIL_CHARS, explain


def _auth(code: int, msg: str) -> smtplib.SMTPAuthenticationError:
    return smtplib.SMTPAuthenticationError(code, msg.encode())


class TestMicrosoft:
    def test_tenant_smtp_disabled_khong_doi_thanh_sai_mat_khau(self) -> None:
        """Luật quan trọng nhất: 535 do tổ chức khoá, KHÔNG phải do sai mật khẩu.

        Máy chủ trả cùng mã 535 cho cả hai. Nếu luật "sai mật khẩu" chặn trước thì
        người vận hành sẽ nhập lại mật khẩu mãi mà không bao giờ hết lỗi.
        """
        exc = _auth(
            535,
            "5.7.139 Authentication unsuccessful, the request did not meet the "
            "criteria to be authenticated. SmtpClientAuthentication is disabled "
            "for the Tenant. Visit https://aka.ms/smtp_auth_disabled",
        )
        got = explain(exc)
        assert "KHOÁ gửi thư qua SMTP" in got
        assert "quản trị Microsoft 365" in got
        # KHÔNG được dẫn người dùng đi nhập lại mật khẩu.
        assert "Nhập lại mật khẩu ứng dụng" not in got

    def test_basic_auth_disabled(self) -> None:
        got = explain(_auth(535, "5.7.139 Basic authentication is disabled"))
        assert "quản trị Microsoft 365" in got

    def test_sai_mat_khau_that_van_ra_huong_dan_nhap_lai(self) -> None:
        got = explain(_auth(535, "5.7.3 Authentication unsuccessful"))
        assert "nhập lại mật khẩu" in got.lower()
        assert "quản trị Microsoft 365" not in got

    def test_gui_thay_dia_chi_khac(self) -> None:
        exc = smtplib.SMTPSenderRefused(
            550,
            b"5.7.60 SMTP; Client does not have permissions to send as this sender",
            "canhbao@congty.com",
        )
        got = explain(exc)
        assert "Địa chỉ gửi (From)" in got


class TestGmail:
    def test_can_mat_khau_ung_dung(self) -> None:
        got = explain(_auth(534, "5.7.9 Application-specific password required"))
        assert "apppasswords" in got
        assert "16 chữ cái" in got

    def test_sai_mat_khau_ung_dung(self) -> None:
        got = explain(_auth(535, "5.7.8 Username and Password not accepted"))
        assert "16 chữ cái" in got
        assert "không phải mật khẩu đăng nhập Google" in got


class TestCauHinhKetNoi:
    def test_starttls_khong_ho_tro(self) -> None:
        got = explain(
            smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server.")
        )
        assert "587" in got and "465" in got

    def test_khong_phan_giai_duoc_ten_may_chu(self) -> None:
        got = explain(socket.gaierror(11001, "getaddrinfo failed"))
        assert "Không tìm thấy máy chủ thư" in got

    def test_het_thoi_gian_cho(self) -> None:
        got = explain(TimeoutError("timed out"))
        assert "cổng 587" in got

    def test_may_chu_ngat_ket_noi(self) -> None:
        got = explain(smtplib.SMTPServerDisconnected("Connection unexpectedly closed"))
        assert "Không kết nối được" in got

    def test_nguoi_nhan_bi_tu_choi(self) -> None:
        exc = smtplib.SMTPRecipientsRefused(
            {"sai@@dia.chi": (550, b"No such user here")}
        )
        got = explain(exc)
        assert "địa chỉ người nhận" in got


class TestDangChung:
    def test_luon_kem_chi_tiet_ky_thuat(self) -> None:
        """Chỉ dẫn cho người vận hành, chi tiết cho bên IT — phải có cả hai."""
        got = explain(_auth(535, "5.7.3 Authentication unsuccessful"))
        assert got.startswith("Máy chủ thư không nhận")
        assert "SMTPAuthenticationError" in got
        assert "(Chi tiết:" in got

    def test_loi_la_khong_lam_no_ham(self) -> None:
        got = explain(ValueError("chuyện gì đó chưa từng thấy"))
        assert "Chưa gửi được thư" in got
        assert "ValueError" in got

    def test_chi_tiet_bi_cat_ngan(self) -> None:
        """Phản hồi dài không được đẩy phần chỉ dẫn ra khỏi màn hình điện thoại."""
        got = explain(RuntimeError("x" * 2000))
        assert len(got) < MAX_DETAIL_CHARS + 400
        assert got.endswith("…)")

    def test_chi_tiet_gop_dong_khong_giu_newline(self) -> None:
        got = explain(RuntimeError("dòng một\n   dòng hai"))
        assert "\n" not in got
        assert "dòng một dòng hai" in got
