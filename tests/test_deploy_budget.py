"""Ngân sách của một cycle phải khớp giữa cấu hình deploy và setting của app.

Hai hằng số ở hai file khác nhau, không ai nối chúng lại, nên chúng lệch nhau âm
thầm — và hậu quả chỉ lộ ra trên production dưới dạng function bị kill giữa cycle
hoặc nguồn mới không bao giờ nạp được dòng nào. Nối bằng test.

Nguồn đo phút bỏ qua bộ lọc ngày nên MỖI cycle stream lại từ bản ghi mới nhất lùi
về 00:00 của ngày cũ nhất trong cửa sổ — với ``ingest_days_back=1`` là 24…48 h dữ
liệu, khoảng 1,9…3,8 MB một lần.

``.github/workflows/ingest.yml`` XIN nhịp 30 phút, nhưng đó không phải nhịp nhận
được: GitHub siết lịch cron. Đo trên 30 lần chạy gần nhất, khoảng cách min 121 /
trung vị 242 / max 440 phút — khoảng 6 cycle/ngày, không phải 48. Chi phí thực
~11…23 MB/ngày. Ghi lại đây vì con số 48 là cái bẫy: nó nằm ngay trong file
workflow và trông như sự thật.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.adapters.yokohama.adapter import EST_BYTES_PER_DAY
from app.adapters.yokohama.config import YokohamaSettings
from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent
VERCEL = ROOT / "vercel.json"

# Một cycle còn phải fetch nguồn kia, lấy báo động và ghi DB sau khi stream xong.
# Cho stream nhiều nhất một nửa ngân sách.
STREAM_SHARE_OF_BUDGET = 0.5


def _max_duration_seconds() -> float:
    cfg = json.loads(VERCEL.read_text(encoding="utf-8"))
    fn = cfg["functions"]["app/main.py"]
    return float(fn["maxDuration"])


def _settings() -> Settings:
    return Settings(app_env="test", db_password="x", scheduler_enabled=False)


def test_stream_time_cap_leaves_headroom_for_rest_of_cycle() -> None:
    """Trần stream không được bằng ngân sách của cả function.

    Bằng nhau nghĩa là riêng stream nguồn mới có thể ăn hết thời gian và function
    bị kill trước khi ghi được gì — mất luôn dữ liệu nguồn đang chạy được.
    """
    budget = _max_duration_seconds()
    cap = YokohamaSettings().max_stream_seconds
    allowed = budget * STREAM_SHARE_OF_BUDGET
    assert cap <= allowed, (
        f"max_stream_seconds={cap}s vượt {allowed}s "
        f"({STREAM_SHARE_OF_BUDGET:.0%} của maxDuration={budget}s trong vercel.json). "
        "Riêng stream sẽ ăn hết ngân sách và function bị kill giữa cycle."
    )


def test_byte_budget_covers_the_configured_window() -> None:
    """Trần byte phải phủ được đúng cửa sổ đang cấu hình.

    Nếu không thì adapter từ chối NGAY ở ngày cũ nhất của mọi cycle, và nguồn đo
    phút không bao giờ nạp được dòng nào — im lặng, vì lỗi schema không fatal.
    Đây là cái bẫy khi ai đó nâng ``ingest_days_back`` mà không nâng trần byte.
    """
    span_days = _settings().ingest_days_back + 1
    need = span_days * EST_BYTES_PER_DAY
    have = YokohamaSettings().max_stream_bytes
    assert need <= have, (
        f"cửa sổ {span_days} ngày cần ~{need} byte nhưng trần là {have}. "
        "Hạ ingest_days_back hoặc nâng YOKOHAMA_MAX_STREAM_BYTES."
    )
