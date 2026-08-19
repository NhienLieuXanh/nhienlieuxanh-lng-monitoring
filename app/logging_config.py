"""Cấu hình logging.

RotatingFileHandler(delay=True), KHÔNG phải TimedRotatingFileHandler: trên Windows
việc rotate theo thời gian fail với PermissionError khi file đang bị một process
khác mở (rất dễ xảy ra khi vừa chạy API vừa chạy CLI). delay=True để file chỉ được
tạo khi có dòng log đầu tiên.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path

# Redaction ở tầng log: token và password không được nằm trong file mà rồi sẽ bị
# copy vào ticket. Adapter đã cố ý không log token, đây là lưới an toàn thứ hai.
_SECRET_RE = re.compile(
    r"(Bearer\s+\S+|[\"']?password[\"']?\s*[:=]\s*\S+|[\"']?token[\"']?\s*[:=]\s*\S+)",
    re.I,
)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and _SECRET_RE.search(record.msg):
            record.msg = _SECRET_RE.sub("[REDACTED]", record.msg)
        return True


def setup_logging(level: str = "INFO", log_dir: str | None = "logs") -> None:
    root = logging.getLogger()
    if root.handlers:
        return  # đã cấu hình (vd uvicorn --reload gọi lại)
    root.setLevel(level.upper())

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S%z",
    )
    redact = RedactFilter()

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(redact)
    root.addHandler(console)

    if log_dir:
        path = Path(log_dir)
        path.mkdir(parents=True, exist_ok=True)
        fileh = logging.handlers.RotatingFileHandler(
            path / "app.log",
            maxBytes=5_000_000,
            backupCount=5,
            encoding="utf-8",
            delay=True,
        )
        fileh.setFormatter(fmt)
        fileh.addFilter(redact)
        root.addHandler(fileh)

    # httpx log mọi request ở INFO, kèm URL đầy đủ -> rò path vendor vào log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
