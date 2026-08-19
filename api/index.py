"""Vercel serverless entrypoint.

Vercel Python runtime phát hiện biến ``app`` (ASGI) trong file dưới ``api/`` và
phục vụ nó. Toàn bộ khởi tạo state nằm trong app.main._init_state (chạy lúc import),
nên endpoint hoạt động kể cả khi Vercel không chạy lifespan.

Lưu ý vận hành trên Vercel (đặt trong Environment Variables, KHÔNG commit):
  SCHEDULER_ENABLED=false   # serverless không giữ process nền; dùng Vercel Cron
  DATABASE_URL=...          # trỏ endpoint có connection pooling (Neon/pgbouncer)
  SESSION_SECRET=...        # bí mật ngẫu nhiên, KHÔNG để mặc định
  ADMIN_TOKEN=... CRON_SECRET=...
  XINGKE_TOKEN=... (hoặc XINGKE_USERNAME + XINGKE_PASSWORD), XINGKE_ALLOWED_PSNS=...
"""

from __future__ import annotations

from app.main import app

__all__ = ["app"]
