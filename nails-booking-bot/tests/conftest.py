import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DB_NAME = "nails_booking_test"
ADMIN_URL = "postgresql+asyncpg://nails:nails_dev_password@localhost:5432/nails_booking"
TEST_URL = f"postgresql+asyncpg://nails:nails_dev_password@localhost:5432/{TEST_DB_NAME}"

# Подменяем URL до импорта настроек приложения.
os.environ["DATABASE_URL"] = TEST_URL

TRUNCATE_SQL = """
TRUNCATE TABLE
    notification_tasks,
    appointments,
    time_blocks,
    date_overrides,
    work_schedules,
    services,
    clients,
    businesses
RESTART IDENTITY CASCADE
"""


@pytest.fixture(scope="session")
def test_database_url() -> str:
    return TEST_URL


@pytest.fixture(scope="session", autouse=True)
def _prepare_test_database() -> None:
    """Создаёт nails_booking_test при необходимости и накатывает миграции."""
    import asyncio

    async def _create_db() -> None:
        engine = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": TEST_DB_NAME},
            )
            if not exists:
                await conn.execute(text(f"CREATE DATABASE {TEST_DB_NAME}"))
        await engine.dispose()

    asyncio.run(_create_db())
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env={**os.environ, "DATABASE_URL": TEST_URL},
        check=True,
    )


@pytest.fixture
async def engine(test_database_url: str):
    engine = create_async_engine(test_database_url, pool_pre_ping=True, pool_size=5)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session_factory(engine):
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def session(session_factory) -> AsyncSession:
    async with session_factory() as session:
        yield session


@pytest.fixture(autouse=True)
async def _clean_tables(engine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(TRUNCATE_SQL))
    yield
