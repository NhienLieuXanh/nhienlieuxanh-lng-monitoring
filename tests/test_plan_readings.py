"""Số đo tay của trang Kế hoạch: schema, SQL upsert, và trần theo dung tích bồn.

Ba nhóm test chạy KHÔNG cần DB, một nhóm cần DB thật:

1. ``PlanReadingIn`` — 0 hợp lệ, số âm bị chặn.
2. SQL mà ``upsert()`` phát ra — phải là ON CONFLICT **DO UPDATE**, không phải
   DO NOTHING. Đây là chỗ dễ sao chép sai nhất: bảng ``telemetry`` ngay bên cạnh
   dùng DO NOTHING (điểm đo của máy là sự thật bất biến), nhưng số người nhập thì
   sửa lại là việc bình thường — DO NOTHING ở đây làm nút Lưu im lặng không làm gì.
3. Trần theo ``capacity_l`` ở router — chặn ca nhập m³ vào API nói bằng lít.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.api.routers import plan as plan_router
from app.api.schemas import PlanReadingIn
from app.db.models import Terminal
from app.repositories import plan_readings as pr_repo

PSN = "2604200016"
DAY = date(2026, 8, 28)


class TestSchema:
    def test_bon_can_la_so_do_hop_le(self) -> None:
        """0 m³ là số đo THẬT — bồn cạn, và đúng lúc cần nhập tay nhất."""
        assert PlanReadingIn(volume_l=Decimal("0")).volume_l == 0

    def test_so_am_bi_chan(self) -> None:
        with pytest.raises(ValidationError):
            PlanReadingIn(volume_l=Decimal("-1"))

    def test_giu_nguyen_phan_thap_phan(self) -> None:
        """47,5 m³ = 47500,5 L phải qua nguyên vẹn, không bị làm tròn về nghìn."""
        assert PlanReadingIn(volume_l=Decimal("47500.5")).volume_l == Decimal("47500.5")


class _FakeResult:
    def __init__(self, row: object) -> None:
        self._row = row

    def scalar_one(self) -> object:
        return self._row


class _CapturingSession:
    """Session giả: ghi lại statement, KHÔNG mở kết nối nào."""

    def __init__(self) -> None:
        self.stmts: list = []

    def execute(self, stmt, *a, **kw):  # type: ignore[no-untyped-def]
        self.stmts.append(stmt)
        return _FakeResult(object())


class TestUpsertGhiDeChuKhongBoQua:
    def _sql(self) -> str:
        s = _CapturingSession()
        pr_repo.upsert(
            s,  # type: ignore[arg-type]  # chỉ cần .execute(); không nối DB nào
            PSN,
            DAY,
            Decimal("48000"),
            by="operator",
        )
        assert s.stmts, "upsert() không phát ra statement nào"
        return str(s.stmts[0].compile(dialect=postgresql.dialect())).lower()

    def test_la_do_update(self) -> None:
        sql = self._sql()
        assert "on conflict" in sql
        assert "do update" in sql
        # DO NOTHING ở đây nghĩa là "sửa lại số đã nhập" âm thầm không có tác dụng.
        assert "do nothing" not in sql

    def test_khoa_xung_dot_la_psn_va_ngay(self) -> None:
        sql = self._sql()
        assert "psn" in sql and "reading_date" in sql

    def test_updated_at_duoc_set_tuong_minh(self) -> None:
        """``onupdate`` của SQLAlchemy KHÔNG chạy trên câu lệnh Core.

        Không set tay thì ``updated_at`` đứng mãi ở thời điểm nhập LẦN ĐẦU, và cột
        truy vết "ai sửa lúc nào" trở thành sai chứ không phải thiếu.
        """
        assert "updated_at" in self._sql()

    def test_luu_nguoi_nhap(self) -> None:
        assert "entered_by" in self._sql()


class TestTranTheoDungTich:
    """Chặn ca nhập m³ vào một API nói bằng lít.

    48 (m³ gõ vào ô lít) trên bồn 60.000 L không sai kiểu, không vi phạm CHECK nào,
    và biến kế hoạch thành vô nghĩa một cách im lặng. Chỉ ngữ nghĩa bắt được nó,
    nên trần phải nằm ở router — nơi duy nhất biết bồn nào.
    """

    def _put(self, volume_l: str, capacity_l: str | None) -> None:
        term = Terminal(
            psn=PSN,
            capacity_l=Decimal(capacity_l) if capacity_l is not None else None,
        )

        class _S:
            def execute(self, *a, **kw):  # type: ignore[no-untyped-def]
                raise AssertionError("không được ghi khi thể tích vượt trần")

            def commit(self) -> None:
                raise AssertionError("không được commit khi thể tích vượt trần")

        # Thay get_by_psn bằng hàm trả terminal dựng sẵn: test này kiểm LUẬT TRẦN,
        # không kiểm đường truy vấn DB.
        orig = plan_router.term_repo.get_by_psn
        plan_router.term_repo.get_by_psn = lambda *a, **kw: term  # type: ignore[assignment]
        try:
            plan_router.put_reading(
                PSN,
                DAY,
                PlanReadingIn(volume_l=Decimal(volume_l)),
                _S(),  # type: ignore[arg-type]
                "operator",
            )
        finally:
            plan_router.term_repo.get_by_psn = orig  # type: ignore[assignment]

    def test_vuot_dung_tich_bi_tu_choi(self) -> None:
        with pytest.raises(HTTPException) as e:
            self._put("70000", "60000")
        assert e.value.status_code == 422
        assert "dung tích" in e.value.detail

    def test_bang_dung_tich_van_chap_nhan(self) -> None:
        """Bồn đầy đúng bằng dung tích là trạng thái thật, không phải lỗi."""
        with pytest.raises(AssertionError, match="không được ghi"):
            # Tới được tầng ghi nghĩa là đã qua trần — session giả chặn ở đó.
            self._put("60000", "60000")

    def test_chua_biet_dung_tich_thi_dung_tran_du_phong(self) -> None:
        with pytest.raises(HTTPException) as e:
            self._put("2000000", None)
        assert e.value.status_code == 422
        assert "ngưỡng hợp lý" in e.value.detail

    def test_psn_la_bi_404(self) -> None:
        orig = plan_router.term_repo.get_by_psn
        plan_router.term_repo.get_by_psn = lambda *a, **kw: None  # type: ignore[assignment]
        try:
            with pytest.raises(HTTPException) as e:
                plan_router.list_readings("9999999999", None, "operator")  # type: ignore[arg-type]
            assert e.value.status_code == 404
        finally:
            plan_router.term_repo.get_by_psn = orig  # type: ignore[assignment]


@pytest.mark.db
class TestVongDoiTrenDbThat:
    """Ghi → đọc → ghi đè → xoá, trên PostgreSQL thật.

    Cần DB thật vì ON CONFLICT và kiểu DATE là thứ không mô phỏng được: chính
    Postgres phải xác nhận rằng nhập lại cùng ngày không sinh dòng thứ hai.
    """

    def _terminal(self, session) -> None:  # type: ignore[no-untyped-def]
        from app.repositories import terminals as term_repo

        term_repo.upsert(session, PSN, default_capacity_l=Decimal("60000"))
        session.flush()

    def test_ghi_doc_ghi_de_xoa(self, session) -> None:  # type: ignore[no-untyped-def]
        self._terminal(session)

        pr_repo.upsert(session, PSN, DAY, Decimal("48000"), by="a")
        rows = pr_repo.list_for(session, PSN)
        assert [(r.reading_date, r.volume_l) for r in rows] == [
            (DAY, Decimal("48000.000"))
        ]

        # Nhập lại cùng ngày: ghi đè, KHÔNG thêm dòng.
        pr_repo.upsert(session, PSN, DAY, Decimal("47500"), by="b")
        rows = pr_repo.list_for(session, PSN)
        assert len(rows) == 1
        assert rows[0].volume_l == Decimal("47500.000")
        assert rows[0].entered_by == "b"

        assert pr_repo.delete(session, PSN, DAY) is True
        assert pr_repo.list_for(session, PSN) == []
        # Xoá lần hai phải trả False để API phân biệt "đã xoá" với "vốn không có".
        assert pr_repo.delete(session, PSN, DAY) is False

    def test_loc_theo_khoang_ngay(self, session) -> None:  # type: ignore[no-untyped-def]
        self._terminal(session)
        for d in (date(2026, 8, 26), DAY, date(2026, 9, 2)):
            pr_repo.upsert(session, PSN, d, Decimal("48000"))
        got = pr_repo.list_for(
            session, PSN, start=date(2026, 8, 27), end=date(2026, 8, 31)
        )
        assert [r.reading_date for r in got] == [DAY]
