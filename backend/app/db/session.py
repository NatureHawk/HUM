"""Async engine and session factory.

Async rather than sync on purpose: Milestone 2 holds a WebSocket per connected
browser in this same event loop. A blocking DB driver would stall every socket
in the process while one query ran. psycopg 3 speaks async natively, so the same
`postgresql+psycopg://` URL serves the async app engine here and the sync engine
Alembic uses.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

_settings = get_settings()

engine: AsyncEngine = create_async_engine(
    _settings.database_url,
    echo=_settings.sql_echo,
    pool_pre_ping=True,  # a recycled-but-dead connection should not fail a request
)

SessionFactory = async_sessionmaker(
    engine,
    expire_on_commit=False,  # we read ORM objects after commit to build events
    autoflush=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session (and one transaction) per request.

    The session is committed by the service layer, never by a route, so a
    partially-applied mutation can never be observed.
    """
    async with SessionFactory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
