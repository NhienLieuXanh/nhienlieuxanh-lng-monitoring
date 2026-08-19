"""Fixture dùng chung.

Test DB dùng PostgreSQL THẬT, không SQLite. Tầng data phụ thuộc JSONB,
INSERT ... ON CONFLICT, DISTINCT ON, gen_random_uuid(), composite FK, xmax, và
semantics timestamptz — mỗi thứ đó hoặc fail hoặc hành xử khác trên SQLite, nên
một suite SQLite sẽ test một chuyện hư cấu.

Test nào cần DB thì mark `@pytest.mark.db`; chúng tự skip nếu chưa có
TEST_DATABASE_URL. Test thuần (mapping, alerts, status, isolation) luôn chạy.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

FIXTURES = Path(__file__).parent / "fixtures" / "xingke"
TEST_DB_URL = os.getenv("TEST_DATABASE_URL")


def pytest_collection_modifyitems(config: pytest.Config, items: list) -> None:
    if TEST_DB_URL:
        return
    skip = pytest.mark.skip(reason="TEST_DATABASE_URL chưa set — bỏ qua test cần DB")
    for item in items:
        # get_closest_marker, KHÔNG phải `"db" in item.keywords`: keywords chứa cả
        # param id, nên một test parametrize với param tên "db" sẽ bị skip oan —
        # đúng chuyện đã xảy ra với test_pure_layers_do_not_import_vendor[db].
        if item.get_closest_marker("db") is not None:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    assert TEST_DB_URL, "engine fixture cần TEST_DATABASE_URL"
    import app.db.models  # noqa: F401
    from app.db.base import Base

    eng = create_engine(TEST_DB_URL, future=True)
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Isolation per-test bằng outer transaction + savepoint.

    Rollback ở cuối nên không test nào thấy dữ liệu của test khác, và không cần
    truncate giữa các test.
    """
    conn = engine.connect()
    trans = conn.begin()
    sess = Session(bind=conn, join_transaction_mode="create_savepoint")
    try:
        yield sess
    finally:
        sess.close()
        trans.rollback()
        conn.close()


@pytest.fixture
def session_factory(engine: Engine, session: Session) -> sessionmaker[Session]:
    """Factory trả về CHÍNH session của test, và KHÔNG close nó.

    ``with factory() as s`` trên SQLAlchemy Session sẽ ``close()`` khi thoát —
    điều đó giết session của test. Guard chỉ yield session, không đóng.
    """

    class _Guard:
        def __enter__(self) -> Session:
            return session

        def __exit__(self, *exc: object) -> None:
            return None

        def begin(self):
            return session.begin()

    class _Factory:
        def __call__(self) -> Session:
            return _Guard()  # type: ignore[return-value]

    return _Factory()  # type: ignore[return-value]


@pytest.fixture
def settings():
    from app.config import Settings

    return Settings(
        app_env="test",
        db_password="x",
        online_stale_minutes=90,
        default_tank_capacity_l=10425.0,
        admin_token="test-admin-token",
        scheduler_enabled=False,
    )
