"""FastAPI application wiring."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import boards, health, ws
from app.api.routes import session as session_routes
from app.core.config import get_settings
from app.core.errors import DomainError
from app.core.redis import close_redis, get_redis
from app.db.session import engine
from app.realtime.hub import init_hub, shutdown_hub

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # One hub per process. It owns this process's sockets and its Redis
    # subscription; a second process gets its own, and Redis bridges them.
    hub = init_hub(get_redis())
    await hub.start()

    yield

    # Close pools explicitly so a reload does not leak connections.
    await shutdown_hub()
    await close_redis()
    await engine.dispose()


app = FastAPI(
    title="SyncBoard API",
    version="0.1.0",
    summary="Realtime collaborative board workspace",
    lifespan=lifespan,
)

settings = get_settings()
# Explicit origins, never "*": the session cookie travels with credentials and
# browsers refuse the wildcard in that combination. `allowed_origins` adds the
# local dev server automatically outside production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)


@app.middleware("http")
async def limit_request_size(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Refuse absurd bodies before they are parsed.

    A public demo should not let one caller stream a gigabyte into the JSON
    decoder. Checks the declared length only - that is enough to stop the
    accidental and the lazy, and the per-field validators catch everything that
    gets past it.
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > settings.max_request_bytes:
        return JSONResponse(
            status_code=413,
            content={
                "code": "payload_too_large",
                "message": "That request is too large.",
                "details": {"limit_bytes": settings.max_request_bytes},
            },
        )
    return await call_next(request)


@app.exception_handler(DomainError)
async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
    """One place that turns a domain error into HTTP.

    Every failure the client is expected to handle - version conflicts, missing
    anchors, deleted cards - arrives in the same shape, so the frontend has one
    error path rather than one per endpoint.
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message, "details": exc.details},
    )


app.include_router(health.router, prefix="/api")
app.include_router(session_routes.router, prefix="/api")
app.include_router(boards.router, prefix="/api")

# No /api prefix: the socket lives at /ws/boards/{board_id}, outside the REST
# surface, because it is a different protocol rather than another resource.
app.include_router(ws.router)
