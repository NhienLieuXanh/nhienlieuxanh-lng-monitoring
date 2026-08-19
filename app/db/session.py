"""Engine, session factory, và helper transaction.

Sync SQLAlchemy, không async. Lý do: tải là 2 thiết bị x ~48 điểm/ngày và sẽ mãi
như vậy. Async không mua được gì ở quy mô này nhưng đánh thuế vĩnh viễn lên mọi
dòng code DB, cộng env.py async cho Alembic và fixture test async. Cầu nối duy
nhất cần thiết là ở ingestion job, nơi adapter HTTP được gọi.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings


def make_engine(settings: Settings, *, application_name: str = "lng-api") -> Engine:
    return create_engine(
        settings.sqlalchemy_url,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=10,
        pool_recycle=1800,
        # KHÔNG optional trên máy này. Postgres chạy local trên workstation sẽ bị
        # suspend/restart theo máy, và không có pre_ping thì request đầu sau khi
        # máy thức dậy fail với "server closed the connection unexpectedly".
        pool_pre_ping=True,
        connect_args={"application_name": application_name},
        future=True,
    )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False: sau commit ta vẫn đọc field của ORM object đã load
    # mà không phát thêm SELECT. Quan trọng cho repository trả object ra ngoài
    # phạm vi session.
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Commit khi thành công, rollback khi lỗi, luôn close."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
