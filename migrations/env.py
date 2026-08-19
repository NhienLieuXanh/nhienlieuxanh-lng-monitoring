"""Alembic env — sync template.

Sync là lợi ích cụ thể của quyết định dùng sync SQLAlchemy: không có boilerplate
run_sync/greenlet nào.

QUAN TRỌNG — không dùng ``config.set_main_option("sqlalchemy.url", ...)``:
configparser chạy %-interpolation trên giá trị được set, và ``URL.render_as_string``
percent-encode password. Một password chứa ``!`` trở thành ``%21`` và alembic chết với
``ValueError: invalid interpolation syntax``. Tạo engine trực tiếp thì tránh
configparser hoàn toàn, chứ không chỉ escape ``%`` thành ``%%`` (cách đó vẫn để lại
cái bẫy cho người sau).
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

# Import để model được đăng ký vào Base.metadata trước khi autogenerate chạy.
import app.db.models  # noqa: F401
from app.config import get_settings
from app.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

DB_URL = get_settings().sqlalchemy_url
target_metadata = Base.metadata

# compare_type + compare_server_default: thiếu chúng thì thay đổi kiểu cột và
# server_default âm thầm KHÔNG vào migration, và model phân kỳ với schema cho tới lúc
# deploy mới mới lộ ra.
_OPTS = {
    "target_metadata": target_metadata,
    "compare_type": True,
    "compare_server_default": True,
}


def run_migrations_offline() -> None:
    context.configure(
        url=DB_URL,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_OPTS,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(DB_URL, poolclass=pool.NullPool, future=True)
    try:
        with connectable.connect() as connection:
            context.configure(connection=connection, **_OPTS)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
