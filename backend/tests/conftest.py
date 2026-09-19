"""Test fixtures.

These are integration tests against a real PostgreSQL, not tests against a
mock or SQLite. That is deliberate: the things most worth testing here -
`SELECT ... FOR UPDATE`, `COLLATE "C"` ordering, `ON DELETE CASCADE`, CHECK
constraints, and the compare-and-swap on `version` - are all database
behaviour. A test double would assert that the mock behaves like the mock.

The suite uses its own database (`syncboard_test`), created and dropped here, so
running tests can never touch development data.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# psycopg's async driver cannot run on Windows' ProactorEventLoop. Same
# constraint as run.py; see that module for the full explanation.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import app.api.routes.ws as ws_module
import app.main as main_module
import app.models  # noqa: F401  registers the tables on Base.metadata
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import get_session

# Aliased: a later `import app.models` would otherwise rebind the name
# `app` to the package and shadow the FastAPI instance.
from app.main import app as fastapi_app

TEST_URL = get_settings().test_database_url


def _admin_url() -> str:
    """Same server, but the `postgres` database - you cannot CREATE DATABASE
    while connected to the database you are creating."""
    return TEST_URL.rsplit("/", 1)[0] + "/postgres"


@pytest.fixture(scope="session")
def event_loop():  # type: ignore[no-untyped-def]
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _database() -> AsyncIterator[None]:
    db_name = TEST_URL.rsplit("/", 1)[1]

    # CREATE DATABASE cannot run inside a transaction block, hence AUTOCOMMIT.
    admin = create_async_engine(_admin_url(), isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        await conn.exec_driver_sql(f'CREATE DATABASE "{db_name}"')
    await admin.dispose()

    engine = create_async_engine(TEST_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

    yield

    admin = create_async_engine(_admin_url(), isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
    await admin.dispose()


@pytest_asyncio.fixture
async def engine():  # type: ignore[no-untyped-def]
    eng = create_async_engine(TEST_URL)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def clean_tables(engine) -> AsyncIterator[None]:  # type: ignore[no-untyped-def]
    """Empty both tables between tests.

    TRUNCATE ... CASCADE rather than recreating the schema: it is far faster and
    it also resets nothing we depend on, since ids are client-generated UUIDs
    and `revision` lives on the row.
    """
    async with engine.begin() as conn:
        await conn.exec_driver_sql("TRUNCATE notes, boards CASCADE")
    yield


@pytest_asyncio.fixture
async def session(engine, clean_tables) -> AsyncIterator[AsyncSession]:  # type: ignore[no-untyped-def]
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as s:
        yield s


@pytest_asyncio.fixture
async def _db_override(engine, clean_tables) -> AsyncIterator[None]:  # type: ignore[no-untyped-def]
    """Point `get_session` at the test engine for every client in this test.

    Split out from `client` itself so `client` and `second_client` can share
    one override instead of each installing and tearing down their own -
    installing it twice would just mean the second install wins and the first
    client's teardown clears it out from under the second.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def _override() -> AsyncIterator[AsyncSession]:
        async with factory() as s:
            try:
                yield s
            except Exception:
                await s.rollback()
                raise

    fastapi_app.dependency_overrides[get_session] = _override
    yield
    fastapi_app.dependency_overrides.clear()


async def _client_with_session() -> AsyncIterator[AsyncClient]:
    """An AsyncClient with its own cookie jar and an established guest session.

    ASGITransport skips the network but exercises the full middleware and
    dependency stack, so routing, validation and the error handler are all
    really under test. httpx's AsyncClient keeps its own cookie jar per
    instance and resends whatever the server has set, so the one
    `POST /api/session` below is enough to carry identity on every request
    this client makes afterwards - no test has to think about the cookie again.
    """
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/session")
        yield c


@pytest_asyncio.fixture
async def client(_db_override: None) -> AsyncIterator[AsyncClient]:  # type: ignore[no-untyped-def]
    """The default actor: an HTTP client with a guest session already established."""
    async for c in _client_with_session():
        yield c


@pytest_asyncio.fixture
async def second_client(_db_override: None) -> AsyncIterator[AsyncClient]:  # type: ignore[no-untyped-def]
    """A second, genuinely different guest.

    Its own AsyncClient means its own cookie jar, which means its own
    server-issued identity - the honest way to express "two different people"
    now that identity is server-side rather than a caller-supplied dict.
    """
    async for c in _client_with_session():
        yield c


@pytest_asyncio.fixture
async def alice(client: AsyncClient) -> dict[str, str]:
    """The identity behind `client`, for tests that assert on `author_name`.

    `POST /api/session` is idempotent (see app/api/routes/session.py), so this
    does not mint a second guest - it just reads back the one `client` already
    established.
    """
    return (await client.post("/api/session")).json()["actor"]


@pytest_asyncio.fixture
async def bob(second_client: AsyncClient) -> dict[str, str]:
    """The identity behind `second_client` - a different guest from `alice`."""
    return (await second_client.post("/api/session")).json()["actor"]


@pytest.fixture
def sync_client(engine, clean_tables, monkeypatch):  # type: ignore[no-untyped-def]
    """A synchronous client with a live app and an established guest session.

    Lives here rather than beside the websocket tests because more than one
    file needs it, and because building a bare `TestClient` per test is a trap:
    a fresh one runs the app's lifespan on its own event loop, whose shutdown
    disposes the module-level async engine that the async tests are using. That
    surfaces as "Event loop is closed" during teardown, and only when the whole
    suite runs together.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def _override():  # type: ignore[no-untyped-def]
        async with factory() as s:
            try:
                yield s
            except Exception:
                await s.rollback()
                raise

    fastapi_app.dependency_overrides[get_session] = _override
    # The websocket route opens its own sessions rather than taking a
    # dependency (see its module docstring), so it reads this name directly.
    monkeypatch.setattr(ws_module, "SessionFactory", factory)

    # The app's lifespan disposes the module-level engine on shutdown. Here that
    # shutdown runs on TestClient's own event loop while the async tests hold
    # connections from theirs, and disposing across loops raises "Event loop is
    # closed" at teardown. Tests never use that engine anyway - every session is
    # overridden above - so disposing it is pure downside.
    class _NoDispose:
        """Stands in for the engine during lifespan shutdown only."""

        async def dispose(self) -> None:
            return None

    # `app.main` imported both names directly, so replacing them there is
    # enough; AsyncEngine.dispose itself is read-only and cannot be patched in
    # place. The Redis client has the same cross-loop problem as the engine:
    # its connection was opened on the async tests' loop, and closing it from
    # TestClient's loop raises the same "Event loop is closed".
    async def _skip_close_redis() -> None:
        return None

    monkeypatch.setattr(main_module, "engine", _NoDispose())
    monkeypatch.setattr(main_module, "close_redis", _skip_close_redis)

    with TestClient(fastapi_app) as c:
        c.post("/api/session")
        yield c
    fastapi_app.dependency_overrides.clear()
