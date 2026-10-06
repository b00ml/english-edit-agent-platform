# tests/conftest.py —— 单元测试共享 fixtures
import os

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# 设置测试环境变量
os.environ.setdefault("JWT_SECRET", "test-secret-for-unit-tests-only")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


# SQLite fallback for JSONB
@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "TEXT"


# SQLite fallback for pgvector.Vector
try:
    from pgvector.sqlalchemy import Vector

    @compiles(Vector, "sqlite")
    def compile_vector_sqlite(type_, compiler, **kw):
        return "TEXT"

except ImportError:
    pass  # pgvector 不可用时跳过


@pytest.fixture()
def db(monkeypatch):
    """内存 SQLite 数据库会话（仅用于单元测试）。"""
    from app.models import Base

    # 创建内存数据库
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # SQLite 需要启用外键约束
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    # 创建所有表
    Base.metadata.create_all(bind=engine)

    # 创建会话
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    original_close = session.close
    # Worker tasks call SessionLocal().close() in finally. Keep the fixture
    # session bound during eager task execution so ORM objects returned by
    # fixtures remain usable for assertions; close it explicitly on teardown.
    monkeypatch.setattr(session, "close", lambda: None)

    # Celery eager execution must use the same in-memory database as the test.
    # The worker closes the session after each task; SQLAlchemy sessions remain
    # reusable after close(), so sharing this fixture preserves test visibility.
    monkeypatch.setattr("app.worker.tasks.SessionLocal", lambda: session)
    # Dispatch tests execute tasks eagerly and exercise child jobs separately;
    # avoid requiring a live broker for the dispatch-side publish call.
    monkeypatch.setattr("app.worker.tasks._item_sender", lambda *args, **kwargs: None)

    yield session

    original_close()
    engine.dispose()
