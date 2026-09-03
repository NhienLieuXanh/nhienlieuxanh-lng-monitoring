"""Khối ``gas`` đi được hết chuỗi: DB -> repo -> domain -> schema -> response.

``test_dual_consumption.py`` chứng minh HÀM đúng. Bài này chứng minh con số THẬT
SỰ tới được người dùng — đúng cái đã sai suốt lần trước: 10 cột nằm trong DB mà
không cột nào ra tới API, nên "đầy đủ thông tin" đúng trên giấy mà bằng 0 trên
màn hình.

Gọi thẳng hàm endpoint thay vì qua TestClient, theo đúng lối ``test_plan_readings``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from app.api.routers.forecast import ForecastParams, forecast_one
from app.config import Settings
from app.db.models import Telemetry, Terminal
from app.domain.forecast import VAPORIZATION_NM3_PER_M3

UTC = ZoneInfo("UTC")
PSN_GAS = "YKH-TEST-01"
PSN_NO_GAS = "XK-TEST-01"
CAP_L = Decimal("60000.000")
STEP = timedelta(minutes=30)
DROP_L = 154.0
N = 60

pytestmark = pytest.mark.db


def _params() -> ForecastParams:
    return ForecastParams(
        window_days=30,
        reserve_l=None,
        lead_time_days=1.0,
        service_level=95,
        relief_mpa=0.8,
        max_fill_percent=90.0,
        reserve_percent=15.0,
    )


def _settings() -> Settings:
    return Settings(app_env="test", db_password="x", scheduler_enabled=False)


def _seed(session: Session, psn: str, *, with_gas: bool) -> None:
    """Bồn + chuỗi đo tụt đều. ``with_gas`` quyết định có đồng hồ khí hay không."""
    tid = uuid.uuid4()
    session.add(
        Terminal(
            id=tid,
            psn=psn,
            name=f"bồn thử {psn}",
            capacity_l=CAP_L,
            status="offline",
        )
    )
    session.flush()

    # Kết thúc ở "vừa xong" để lần đọc không bị đánh dấu stale.
    end = datetime.now(tz=UTC)
    vol = 54_000.0
    gas = 1_000_000.0
    rows = []
    for i in range(N + 1):
        rows.append(
            Telemetry(
                terminal_id=tid,
                psn=psn,
                sampled_at=end - STEP * (N - i),
                volume_l=Decimal(str(round(vol, 3))),
                gm_totalizer_nm3=(Decimal(str(round(gas, 3))) if with_gas else None),
                source="tst",
                raw_payload={},
            )
        )
        vol -= DROP_L
        gas += (DROP_L / 1000.0) * VAPORIZATION_NM3_PER_M3
    session.add_all(rows)
    session.flush()


def test_bon_co_dong_ho_khi_phat_ra_khoi_gas(session: Session) -> None:
    _seed(session, PSN_GAS, with_gas=True)

    out = forecast_one(
        PSN_GAS, session, _settings(), _params(), None  # type: ignore[arg-type]
    )

    assert out.gas is not None, "bồn có đồng hồ khí mà response không có khối gas"
    g = out.gas
    assert g.verdict == "match"
    assert g.ratio_nm3_per_m3 is not None
    assert abs(float(g.ratio_nm3_per_m3) - VAPORIZATION_NM3_PER_M3) < 2.0
    # Hai con số được phát RIÊNG, không cái nào bị quy đổi thành cái nào.
    assert g.liquid_net_l is not None and float(g.liquid_net_l) > 0
    assert g.gas_nm3 is not None and float(g.gas_nm3) > 0
    # Mốc và dải đi kèm để client không phải hard-code ngưỡng.
    assert float(g.reference_ratio_nm3_per_m3) == VAPORIZATION_NM3_PER_M3
    assert float(g.band_lo_nm3_per_m3) < float(g.band_hi_nm3_per_m3)


def test_bon_khong_co_dong_ho_khi_thi_gas_la_None(session: Session) -> None:
    """None nghĩa là KHÔNG CÓ cảm biến, không phải "chưa đủ dữ liệu".

    Hai bồn Xingke không có đồng hồ khí. Phát một khối gas rỗng cho chúng sẽ làm
    người đọc tưởng cảm biến đang hỏng hoặc dữ liệu đang thiếu.
    """
    _seed(session, PSN_NO_GAS, with_gas=False)

    out = forecast_one(
        PSN_NO_GAS, session, _settings(), _params(), None  # type: ignore[arg-type]
    )

    assert out.gas is None


def test_muoi_cot_do_them_ra_duoc_api(session: Session) -> None:
    """``TelemetryOut`` phải phát 10 cột mới, nếu không dashboard không vẽ được gì."""
    from app.api.schemas import TelemetryOut

    fields = set(TelemetryOut.model_fields)
    for name in (
        "gm_totalizer_nm3",
        "gm_flow_rate_nm3h",
        "gm_pressure_kpa",
        "gm_temperature_c",
        "ps1_bar",
        "ps2_bar",
        "gd1_percent",
        "gd2_percent",
        "gd3_percent",
        "refill_counter",
    ):
        assert name in fields, (
            f"TelemetryOut thiếu {name} — số nằm trong DB mà không ra được API"
        )

    # Và giá trị thật đi qua được, không chỉ có tên field.
    _seed(session, PSN_GAS, with_gas=True)
    from app.repositories import telemetry as tel_repo

    latest = tel_repo.latest_for(session, PSN_GAS)
    assert latest is not None
    out = TelemetryOut.model_validate(latest)
    assert out.gm_totalizer_nm3 is not None and out.gm_totalizer_nm3 > 0
