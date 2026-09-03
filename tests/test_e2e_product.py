"""E2E tầng thuần — chạy ở MỌI môi trường, không cần PostgreSQL.

Bốn test, cố ý không test nào cần DB:

1. ``test_openapi_surface`` — kiểm kê route hai chiều: thiếu route thì đỏ, mà thừa
   route lạ cũng đỏ.
2. ``test_unauthenticated_guard`` — quét 401 trên TOÀN BỘ endpoint dữ liệu.
3. ``test_forecast_pipeline`` — chuỗi mẫu -> tiêu thụ -> giữ áp -> cạn -> đề xuất,
   đối chiếu số tính tay trên cấu hình bồn thật.
4. ``test_delivery_planning`` — gom bồn thành chuyến theo tải xe.

Hai test cuối trùng phạm vi với ``tests/test_forecast.py`` (29 test đơn vị, chặt hơn
về hình dạng thuật toán). Chúng được giữ với một việc KHÁC: assert **giá trị tính tay
chính xác** cho cấu hình đội bồn thật (10425 L, dự trữ 15%, trần rót 90%), chỗ mà
test_forecast.py chỉ assert ``is not None``. Nếu ai đó đổi hằng số cấu hình mà không
đổi công thức, hai test này là chỗ nó lộ ra.

Phiên bản trước của file này còn 10 test ``@pytest.mark.db``. Đã bỏ — không phải vì
cần DB, mà vì **chúng chưa từng chạy một lần nào**: fixture gọi
``run_cycle(trigger=..., stats=...)`` trong khi ``IngestionService.run_cycle`` không
nhận ``stats`` — mọi test đó ``TypeError`` ngay dòng đầu. ``conftest`` bỏ qua chúng
khi thiếu ``TEST_DATABASE_URL`` nên lỗi bị che, và suite báo "10 skipped" như thể chỉ
thiếu môi trường. Phần việc thật của chúng (API trên dữ liệu thật) nay do
``scripts/test_live_e2e.py`` đảm nhiệm — chạy trên server thật và KIỂM NỘI DUNG.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_settings
from app.config import Settings
from app.domain.forecast import Sample, build_forecast, plan_trips
from app.main import create_app

UTC = ZoneInfo("UTC")
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# Bề mặt API của sản phẩm, phân theo CƠ CHẾ BẢO VỆ chứ không theo router: đây là
# thứ quyết định endpoint nào được phép trả dữ liệu cho ai. Thêm route mà quên khai
# ở đây thì test đỏ — và đó là chủ ý: một endpoint dữ liệu mới xuất hiện mà không ai
# xem lại guard chính là cách rò dữ liệu xảy ra.

# Không cần phiên. Ba route này là toàn bộ bề mặt vô danh của hệ thống.
PUBLIC = frozenset({"/api/health", "/api/auth/login", "/api/auth/logout"})

# Cần cookie phiên. {psn} được thay bằng PSN thật khi gọi.
SESSION_GET = frozenset({
    "/api/auth/me",
    "/api/alerts",
    # Báo động do NGUỒN phát (khác /api/alerts là cảnh báo platform tự suy).
    # Cùng nhóm bảo vệ: chúng là nhật ký sự cố của nhà máy, không công khai.
    "/api/alarms/vendor",
    "/api/alarms/vendor/summary",
    "/api/stats/summary",
    "/api/terminals",
    "/api/terminals/{psn}",
    "/api/telemetry/{psn}",
    "/api/telemetry/{psn}/latest",
    "/api/forecast",
    "/api/forecast/{psn}",
    "/api/analytics",
    "/api/analytics/{psn}",
    "/api/refills/{psn}",
    "/api/plan/readings/{psn}",
    "/api/plan/settings/{psn}",
    "/api/delivery-plan",
    "/api/export/report.html",
    "/api/export/tanks.csv",
    "/api/export/refills.csv",
    "/api/export/telemetry.csv",
    "/api/settings",
})
SESSION_PATCH = frozenset({"/api/settings", "/api/terminals/{psn}"})
SESSION_POST = frozenset({"/api/settings/test-email"})
# Số đo tay của trang Kế hoạch. Ghi bằng PUT vì địa chỉ (bồn, ngày) xác định đúng
# một số đo — bấm Lưu hai lần không được sinh hai dòng.
SESSION_PUT = frozenset({
    "/api/plan/readings/{psn}/{day}",
    "/api/plan/settings/{psn}",
})
SESSION_DELETE = frozenset({"/api/plan/readings/{psn}/{day}"})

# Cần header X-Admin-Token, KHÔNG dùng phiên.
ADMIN_GET = frozenset({"/api/admin/ingest/runs", "/api/admin/notifications"})
ADMIN_POST = frozenset({
    "/api/admin/ingest/run",
    "/api/admin/ingest/resume",
    "/api/admin/backfill",
    "/api/admin/notify/run",
    "/api/admin/db/sync",
})

ALL_DOCUMENTED = (
    PUBLIC
    | SESSION_GET
    | SESSION_PATCH
    | SESSION_POST
    | SESSION_PUT
    | SESSION_DELETE
    | ADMIN_GET
    | ADMIN_POST
)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        app_env="test",
        db_password="x",
        session_secret="test-secret-32-chars-long-enough!",
        admin_token="test-admin-token",
        scheduler_enabled=False,
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    """App thật, KHÔNG vào lifespan nên không cần DB lẫn adapter vendor.

    ``TestClient(app)`` ngoài context manager thì Starlette không chạy lifespan —
    đúng thứ cần ở đây: guard 401 nằm ở tầng dependency, đứng TRƯỚC mọi truy cập
    DB, nên kiểm được mà không cần Postgres.
    """
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


def test_openapi_surface() -> None:
    """Kiểm kê route hai chiều: thiếu thì đỏ, mà thừa route lạ cũng đỏ."""
    spec = create_app().openapi()
    assert spec["info"]["title"] == "NLX LNG Monitoring - Internal API"

    found = {p for p in spec["paths"] if p.startswith("/api/")}
    missing, extra = ALL_DOCUMENTED - found, found - ALL_DOCUMENTED
    assert not missing, f"route khai báo nhưng KHÔNG tồn tại: {sorted(missing)}"
    assert not extra, (
        f"route MỚI chưa khai báo trong test: {sorted(extra)} — thêm vào đúng nhóm "
        "bảo vệ (PUBLIC / SESSION_* / ADMIN_*) và xem lại guard của nó"
    )

    # /api/cron/ingest CỐ Ý không có trong schema (include_in_schema=False): nó chỉ
    # dành cho Vercel Cron với Bearer CRON_SECRET, không phải bề mặt cho con người.
    # Assert điều này để lần sau ai bỏ cờ đó thì biết mình đang công bố nó.
    assert "/api/cron/ingest" not in spec["paths"]


def test_unauthenticated_guard(client: TestClient) -> None:
    """Không phiên, không token -> 401 ở MỌI endpoint dữ liệu.

    Assert đúng 401, KHÔNG phải "401 hoặc 5xx". Một endpoint sập cũng chặn được truy
    cập nhưng không chứng minh guard tồn tại — đúng cái bẫy khiến bản test trước báo
    "security validated" trong khi nó chỉ chấp nhận ``in (401, 503)``.
    """
    checks: list[tuple[str, str]] = (
        [("GET", p) for p in SESSION_GET | ADMIN_GET]
        + [("PATCH", p) for p in SESSION_PATCH]
        + [("POST", p) for p in SESSION_POST | ADMIN_POST]
        + [("PUT", p) for p in SESSION_PUT]
        + [("DELETE", p) for p in SESSION_DELETE]
    )
    for method, path in sorted(checks):
        url = path.replace("{psn}", "2604200016").replace("{day}", "2026-08-28")
        resp = client.request(
            method, url, json={} if method in ("PATCH", "POST", "PUT") else None
        )
        assert resp.status_code == 401, f"{method} {url} trả {resp.status_code}, cần 401"

    # /api/health phải mở: monitor bên ngoài không có phiên, và một health endpoint
    # đòi đăng nhập là health endpoint không ai dùng được.
    assert client.get("/api/health").status_code in (200, 503)


def test_forecast_pipeline() -> None:
    """Chuỗi mẫu -> tiêu thụ -> giữ áp -> cạn -> đề xuất, đối chiếu số tính tay.

    10 ngày rút đúng 500 L/ngày, mỗi giờ một mẫu, trên bồn 10425 L. Mọi con số dưới
    đây tính tay được — test bắt sai lệch về GIÁ TRỊ, không chỉ về kiểu.
    """
    now = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    samples = [
        Sample(
            at=now - timedelta(hours=240 - i),
            volume_l=8000.0 - (i / 24.0) * 500.0,
            pressure_mpa=0.10 + (i / 240.0) * 0.05,
        )
        for i in range(240)
    ]

    fc = build_forecast(
        samples,
        psn="2604200016",
        volume_l=3000.0,
        capacity_l=10425.0,
        pressure_mpa=0.15,
        now=now,
        tz=VN_TZ,
        reserve_percent=15.0,
        lead_time_days=2.0,
        service_level=95,
        relief_mpa=0.8,
        max_fill_percent=90.0,
        reading_at=now,
        max_reading_age_days=1.0,
    )

    assert fc.stale is False
    # Tiêu thụ dựng vào chuỗi là 500 L/ngày; nới 5 L cho deadband.
    assert fc.consumption.daily_use_l == pytest.approx(500.0, abs=5.0)
    assert fc.consumption.confidence in ("high", "medium")

    # Mức dự trữ = 15% x 10425 = 1563.75 L, khớp chính xác.
    assert fc.reserve_l == pytest.approx(1563.75, abs=1e-6)
    # Giữ áp: khoảng trống = van an toàn 0.8 - hiện tại 0.15 = 0.65 MPa.
    assert fc.hold.headroom_mpa == pytest.approx(0.65, abs=1e-9)

    # Tới mức dự trữ: (3000 - 1563.75) / ~505 L/ngày ~= 2.84 ngày. Tới cạn phải xa
    # hơn, và hai mốc KHÔNG được bằng nhau — chúng bằng nhau là dấu hiệu mức dự trữ
    # bị bỏ khỏi công thức.
    d_res, d_empty = fc.runout.days_to_reserve, fc.runout.days_to_empty
    assert d_res is not None and d_empty is not None
    # (3000 - 1563.75) / 505.2 L/ngày = 2.843 ngày.
    assert d_res == pytest.approx(2.843, abs=0.01)
    # Tới cạn phải xa hơn tới dự trữ đúng bằng 1563.75 / 505.2 = 3.095 ngày. Hai mốc
    # bằng nhau là dấu hiệu mức dự trữ bị bỏ khỏi công thức.
    assert d_empty - d_res == pytest.approx(3.095, abs=0.01)

    # Chuỗi đặt hàng, từng bước tính tay được:
    sg = fc.suggestion
    #   đích = 90% x 10425 (chừa khoảng hơi cho giãn nở nhiệt)
    assert sg.target_l == pytest.approx(9382.5, abs=1e-6)
    #   điểm đặt lại = MAX(dự trữ người vận hành 1563.75 ; hao trong lead time
    #   505.2 x 2 = 1010.4 + dự trữ an toàn ~0 vì chuỗi tuyến tính hoàn hảo).
    #   Chính sách người vận hành là sàn, mô hình không được hạ xuống.
    assert sg.reorder_point_l == pytest.approx(1563.75, abs=1e-6)
    assert sg.safety_stock_l == pytest.approx(0.0, abs=1e-6)
    #   lượng đặt = đích - mức LÚC XE TỚI, không phải mức hiện tại: xe tới sau
    #   2.843 + 2 = 4.843 ngày, lúc đó còn 3000 - 505.2 x 4.843 = 553.3 L.
    #   => 9382.5 - 553.3 = 8829.2 L. Nếu ai đó "sửa" thành mức hiện tại thì con số
    #   tụt về 6382.5 và bồn sẽ về non nửa bồn sau mỗi lần giao.
    assert sg.order_l == pytest.approx(8829.2, abs=1.0)
    # Mức 3000 L còn cao hơn điểm đặt lại 1563.75 -> chưa gấp. Assert đúng một giá
    # trị, không phải "một trong ba": urgency là kết luận, không phải liệt kê.
    assert sg.urgency == "ok"
    assert len(sg.reasons) >= 3


def test_delivery_planning() -> None:
    """Gom bồn thành chuyến: không vượt tải xe, không mất bồn, tổng khớp các điểm."""
    now = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    # `i` đi LÙI thời gian, nên volume phải TĂNG theo i để thành đường rút xuống
    # 240 L/ngày. Viết ngược dấu ở đây thì chuỗi thành đường nạp lên, mức tiêu thụ
    # không đo được, và test âm thầm rơi vào nhánh dự phòng thay vì nhánh chính.
    samples = [
        Sample(at=now - timedelta(hours=i), volume_l=2000.0 + i * 10, pressure_mpa=0.1)
        for i in range(48)
    ]

    def mk(psn: str, volume_l: float):
        return build_forecast(
            samples,
            psn=psn,
            volume_l=volume_l,
            capacity_l=10000.0,
            pressure_mpa=0.12,
            now=now,
            tz=VN_TZ,
            reserve_percent=15.0,
            reading_at=now,
        )

    # Cả hai mức phải THẤP HƠN HẲN mức dự trữ (15% x 10000 = 1500 L). Đặt đúng 1500
    # là rơi vào biên: điều kiện là `volume < reserve` nên bồn ở đúng mức dự trữ trả
    # order_l = None và bị loại khỏi lịch giao — đúng lỗi đã mắc khi viết test này.
    trips = plan_trips(
        [mk("TANK-01", 1200.0), mk("TANK-02", 1000.0)],
        truck_capacity_l=20000.0,
        horizon_days=7.0,
        names={"TANK-01": "Kho A", "TANK-02": "Kho B"},
    )

    assert trips, "hai bồn dưới mức dự trữ mà không sinh chuyến nào"
    for t in trips:
        assert t.truck_capacity_l == 20000.0
        assert t.total_l <= t.truck_capacity_l, "chuyến vượt tải xe"
        # Tổng chuyến phải bằng tổng các điểm giao — chống lỗi cộng dồn im lặng.
        assert t.total_l == pytest.approx(sum(s.order_l for s in t.stops), abs=0.01)

    stops = [s for t in trips for s in t.stops]
    assert {s.psn for s in stops} == {"TANK-01", "TANK-02"}, "có bồn bị bỏ rơi"
    assert {s.name for s in stops} == {"Kho A", "Kho B"}, "tên bồn không tới điểm giao"
