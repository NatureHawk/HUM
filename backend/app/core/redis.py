"""Redis connection.

Redis exists in this system for exactly one architectural reason: fan-out
between backend processes. The moment there are two uvicorn workers, a WebSocket
held by worker A is unreachable from worker B, where the mutating HTTP request
happened to land. An in-process subscriber set is only correct at exactly one
process, which is not something you can promise in production.

Milestone 2 will use:
  * `board:{id}:events`   - pub/sub channel carrying the same event envelope the
                            HTTP mutation response already returns.
  * `board:{id}:presence` - hash of connection_id -> actor with heartbeat TTLs,
                            so a browser that dies without closing its socket
                            expires instead of haunting the board.

Redis is deliberately NOT the source of truth and NOT a cache of card data. If
it is lost, clients see a stale board until they resync from Postgres: degraded,
not corrupted.

In Milestone 1 the pool is created and the health endpoint really PINGs it.
Nothing publishes yet.
"""

from __future__ import annotations

from redis.asyncio import Redis

from app.core.config import get_settings

_client: Redis | None = None


def get_redis() -> Redis:
    """Process-wide client. redis-py manages its own connection pool."""
    global _client
    if _client is None:
        _client = Redis.from_url(
            get_settings().redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
        )
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
