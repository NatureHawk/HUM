"""Health endpoints.

Two, because they answer different questions and a platform uses them
differently.

`/health/live` asks "is this process alive?" and touches nothing. It must never
depend on Postgres or Redis: if it did, a database blip would make the platform
kill and restart every healthy replica, turning a recoverable outage into a
crash loop.

`/health/ready` asks "should traffic be sent here?" and really probes both
dependencies, under a timeout. A readiness check that can hang is worse than
none at all, because the platform waits on it instead of routing elsewhere.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import get_redis
from app.db.session import get_session

router = APIRouter(tags=["health"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# Generous enough not to trip on a slow first connection, short enough that a
# platform's own health timeout never fires first.
PROBE_TIMEOUT_SECONDS = 3.0


class DependencyStatus(BaseModel):
    ok: bool
    detail: str | None = None


class LiveResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadyResponse(BaseModel):
    status: Literal["ok", "degraded"]
    postgres: DependencyStatus
    redis: DependencyStatus


@router.get("/health/live", response_model=LiveResponse)
async def live() -> LiveResponse:
    """Liveness. Deliberately does no I/O at all."""
    return LiveResponse()


async def _probe(awaitable: Awaitable[object]) -> DependencyStatus:
    """Run one dependency check under a timeout, reporting instead of raising.

    The detail is the exception *type*, never its message: a connection error's
    message routinely contains the host, port and username.
    """
    try:
        await asyncio.wait_for(awaitable, timeout=PROBE_TIMEOUT_SECONDS)
        return DependencyStatus(ok=True)
    except TimeoutError:
        return DependencyStatus(ok=False, detail="timeout")
    except Exception as exc:
        return DependencyStatus(ok=False, detail=type(exc).__name__)


@router.get("/health/ready", response_model=ReadyResponse)
async def ready(response: Response, session: SessionDep) -> ReadyResponse:
    """Readiness. Probes both dependencies; answers 503 if either is down."""
    postgres = await _probe(session.execute(text("SELECT 1")))
    redis = await _probe(get_redis().ping())

    healthy = postgres.ok and redis.ok
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(
        status="ok" if healthy else "degraded",
        postgres=postgres,
        redis=redis,
    )


@router.get("/health", response_model=ReadyResponse)
async def health(response: Response, session: SessionDep) -> ReadyResponse:
    """Alias for readiness - it is the obvious URL to try."""
    return await ready(response, session)
