"""Shared test fixtures.

`db_session` (and the `db` pytest marker) exist to close the IRR-2026-07-11
testing-audit gap: before this file, no test in the suite touched a real or
in-memory database (`session_factory=None` was passed ~21 times specifically
to bypass persistence). Every model here uses Postgres JSONB (see
`database/tables.py`), so an in-memory SQLite substitute wouldn't exercise
the same code paths -- this spins up (or reuses) a real Postgres instead.

Resolution order for where that Postgres comes from:
1. `TEST_DATABASE_URL` env var, if set (this is what CI provides -- see
   .github/workflows/backend-tests.yml's `postgres` service container).
2. Otherwise, spin up a throwaway `postgres:16-alpine` container via the
   `docker` CLI for the test session and tear it down afterward (local dev).

Each test gets a `session_factory` bound to a real connection pool (not a
single shared connection): several engines in this codebase fan out with
`asyncio.gather()` across multiple concurrently-open sessions (composite.py,
conviction.py, opportunity.py all do this), and a single physical
connection can't safely serve two overlapping queries at once -- an
earlier SAVEPOINT-per-connection design corrupted the connection's
transaction state the moment a gather-based engine actually got exercised
against it (`PendingRollbackError: Can't reconnect until invalid savepoint
transaction is rolled back`). A real pool gives each concurrent session its
own connection, matching how the app actually runs in production. Isolation
between tests is TRUNCATE-after, not rollback -- cheap enough at this table
count and avoids the concurrency trap entirely.
"""

import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

CONTAINER_NAME = "quantstack-test-postgres"
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _docker_available() -> bool:
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10
        ).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _wait_for_postgres(container: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = subprocess.run(
            ["docker", "exec", container, "pg_isready", "-U", "quantstack"],
            capture_output=True,
        )
        if result.returncode == 0:
            return
        time.sleep(0.5)
    raise RuntimeError(f"test postgres container {container!r} never became ready")


@pytest.fixture(scope="session")
def postgres_test_url() -> AsyncIterator[str]:
    """An asyncpg URL for a real, empty-schema-then-migrated Postgres.

    Yields None (via skip) if neither TEST_DATABASE_URL nor a working
    `docker` CLI is available -- `db`-marked tests skip cleanly rather than
    failing a whole local run for someone without Docker installed.
    """
    env_url = os.environ.get("TEST_DATABASE_URL")
    started_container = False

    if env_url:
        url = env_url
    elif _docker_available():
        port = _free_port()
        subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True)
        subprocess.run(
            [
                "docker", "run", "-d", "--name", CONTAINER_NAME,
                "-e", "POSTGRES_USER=quantstack",
                "-e", "POSTGRES_PASSWORD=quantstack",
                "-e", "POSTGRES_DB=quantstack_test",
                "-p", f"{port}:5432",
                "postgres:16-alpine",
            ],
            check=True, capture_output=True,
        )
        started_container = True
        _wait_for_postgres(CONTAINER_NAME)
        url = f"postgresql+asyncpg://quantstack:quantstack@localhost:{port}/quantstack_test"
    else:
        pytest.skip("no TEST_DATABASE_URL and no working docker CLI -- can't provision a test Postgres")

    migrate_result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True, text=True,
    )
    if migrate_result.returncode != 0:
        if started_container:
            subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True)
        raise RuntimeError(
            f"alembic upgrade head failed against the test database:\n"
            f"{migrate_result.stdout}\n{migrate_result.stderr}"
        )

    yield url

    if started_container:
        subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True)


@pytest_asyncio.fixture
async def test_session_factory(postgres_test_url):
    """A `session_factory` callable (the same shape every engine's
    constructor accepts) bound to a real connection pool -- safe for
    concurrent sessions, unlike a single shared connection. Tables are
    truncated after the test so the next test starts clean."""
    engine = create_async_engine(postgres_test_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield session_factory

    from app.database.tables import Base

    table_names = [table.name for table in Base.metadata.sorted_tables]
    async with engine.begin() as conn:
        await conn.execute(
            text(f"TRUNCATE TABLE {', '.join(table_names)} RESTART IDENTITY CASCADE")
        )
    await engine.dispose()
