"""Fixtures for the database-backed tests.

A real PostgreSQL rather than SQLite: the invariant this service leans on is a
*partial* unique index (`WHERE state = 'active'`), and the whole point of the
integration suite is that the database refuses what the service would otherwise
have to remember to check. SQLite would accept the schema and enforce something
else.
"""

from collections.abc import AsyncIterator

import pytest
from edutap.db_definitions.public import metadata
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from testcontainers.community.postgres import PostgresContainer


@pytest.fixture(scope="session")
def postgres_dsn() -> AsyncIterator[str]:
    """One container for the whole session; starting it per test dominates the runtime."""
    with PostgresContainer("postgres:17-alpine", driver="asyncpg") as container:
        yield container.get_connection_url()


@pytest.fixture
async def engine(postgres_dsn: str) -> AsyncIterator[AsyncEngine]:
    """Create a fresh schema per test.

    Dropped and recreated rather than truncated: these tests assert on indexes and
    constraints, and a test that alters one must not leak into the next.
    """
    engine = create_async_engine(postgres_dsn)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
        await connection.run_sync(metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Yield a session the test commits itself.

    The repository deliberately never commits -- see its module docstring -- so a
    fixture that committed for it would hide exactly the behaviour under test.
    """
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
