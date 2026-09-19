"""Presence, stored in Redis.

Presence is the textbook example of ephemeral realtime state (architecture.md
section 1): it describes what is happening *right now*, it is worthless seconds
later, and losing it on restart costs nothing. It must never touch Postgres -
a browser that dies without closing its socket would otherwise leave a
permanent ghost on the board, and every heartbeat would be a durable write.

Storage per board, so that any process can answer "who is here":

    board:{id}:presence            HASH   connection_id -> member JSON
    board:{id}:presence:heartbeat  ZSET   connection_id -> last-seen epoch

The ZSET is what makes abrupt disconnects self-healing. A clean disconnect
removes the connection immediately, but a killed browser or a severed network
sends no close frame, so entries are also expired by age: every read first
drops connections whose heartbeat is older than PRESENCE_TTL_SECONDS.

A hash field cannot carry its own TTL, which is why the timestamp lives in a
parallel sorted set rather than in the hash value.

Field-level eviction is lazy - it happens on the next read of that board - so
both keys ALSO carry a whole-key expiry, refreshed on every write. Without it,
a board whose last connection dropped abruptly and which nobody ever opens
again would leave its presence hash in Redis forever. Lazy eviction cleans the
boards people come back to; the key TTL cleans the ones they do not.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

from redis.asyncio import Redis

from app.schemas.note import Actor
from app.schemas.realtime import PresenceMember

# Generous relative to the client's heartbeat interval, so one dropped ping
# does not evict a healthy connection.
PRESENCE_TTL_SECONDS = 75

# Whole-key expiry, refreshed on every write. Comfortably longer than the
# per-connection TTL so it never races a live connection out of existence; its
# job is only to reclaim boards that have gone completely quiet.
KEY_TTL_SECONDS = PRESENCE_TTL_SECONDS * 4


def presence_key(board_id: uuid.UUID) -> str:
    return f"board:{board_id}:presence"


def heartbeat_key(board_id: uuid.UUID) -> str:
    return f"board:{board_id}:presence:heartbeat"


def _encode(member: PresenceMember) -> str:
    return member.model_dump_json()


def _decode(raw: bytes | str) -> PresenceMember | None:
    # The client is created with decode_responses=True, but redis-py's types
    # still allow bytes, so normalise rather than assume.
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        return PresenceMember.model_validate_json(raw)
    except ValueError:
        # A malformed entry is someone else's bug or an old schema; drop it
        # rather than breaking presence for everyone on the board.
        return None


class PresenceStore:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def join(
        self,
        *,
        board_id: uuid.UUID,
        connection_id: uuid.UUID,
        actor: Actor,
    ) -> PresenceMember:
        member = PresenceMember(
            connection_id=connection_id,
            actor=actor,
            connected_at=datetime.now(UTC),
        )
        pipe = self._redis.pipeline()
        pipe.hset(presence_key(board_id), str(connection_id), _encode(member))
        pipe.zadd(heartbeat_key(board_id), {str(connection_id): time.time()})
        pipe.expire(presence_key(board_id), KEY_TTL_SECONDS)
        pipe.expire(heartbeat_key(board_id), KEY_TTL_SECONDS)
        await pipe.execute()
        return member

    async def leave(self, *, board_id: uuid.UUID, connection_id: uuid.UUID) -> None:
        pipe = self._redis.pipeline()
        pipe.hdel(presence_key(board_id), str(connection_id))
        pipe.zrem(heartbeat_key(board_id), str(connection_id))
        await pipe.execute()

    async def heartbeat(self, *, board_id: uuid.UUID, connection_id: uuid.UUID) -> None:
        pipe = self._redis.pipeline()
        pipe.zadd(heartbeat_key(board_id), {str(connection_id): time.time()})
        # Push the whole-key expiry out too, so a board with live connections
        # never expires underneath them.
        pipe.expire(presence_key(board_id), KEY_TTL_SECONDS)
        pipe.expire(heartbeat_key(board_id), KEY_TTL_SECONDS)
        await pipe.execute()

    async def set_editing(
        self,
        *,
        board_id: uuid.UUID,
        connection_id: uuid.UUID,
        note_id: uuid.UUID | None,
    ) -> PresenceMember | None:
        """Record which note this connection has open. Returns the updated member."""
        raw = await self._redis.hget(presence_key(board_id), str(connection_id))
        if raw is None:
            return None
        member = _decode(raw)
        if member is None:
            return None

        updated = member.model_copy(update={"editing_note_id": note_id})
        pipe = self._redis.pipeline()
        pipe.hset(presence_key(board_id), str(connection_id), _encode(updated))
        pipe.zadd(heartbeat_key(board_id), {str(connection_id): time.time()})
        pipe.expire(presence_key(board_id), KEY_TTL_SECONDS)
        pipe.expire(heartbeat_key(board_id), KEY_TTL_SECONDS)
        await pipe.execute()
        return updated

    async def members(self, board_id: uuid.UUID) -> list[PresenceMember]:
        """Everyone currently on the board, stale connections evicted first."""
        await self._evict_stale(board_id)

        raw_members = await self._redis.hgetall(presence_key(board_id))
        members = [m for m in (_decode(raw) for raw in raw_members.values()) if m is not None]
        # Stable order so two clients render the same rail.
        members.sort(key=lambda m: (m.connected_at, str(m.connection_id)))
        return members

    async def _evict_stale(self, board_id: uuid.UUID) -> None:
        """Drop connections that stopped heartbeating.

        This is how a browser that was killed, slept, or lost its network
        disappears from the board without ever having sent a close frame.
        """
        cutoff = time.time() - PRESENCE_TTL_SECONDS
        stale = await self._redis.zrangebyscore(heartbeat_key(board_id), "-inf", cutoff)
        if not stale:
            return
        pipe = self._redis.pipeline()
        pipe.hdel(presence_key(board_id), *stale)
        pipe.zremrangebyscore(heartbeat_key(board_id), "-inf", cutoff)
        await pipe.execute()
