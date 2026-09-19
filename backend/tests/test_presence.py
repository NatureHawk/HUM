"""PresenceStore against real Redis.

Presence never touches Postgres - see app/realtime/presence.py's module
docstring. These tests exercise the real store rather than a fake one, since
the whole point of that design is Redis-specific behaviour (hash + ZSET,
TTL-by-score eviction) that a mock would not actually verify.

Every test builds its own board id so the presence keys and heartbeat ZSET
never collide with another test sharing the same Redis instance.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator

import pytest_asyncio
from redis.asyncio import Redis

from app.core.config import get_settings
from app.realtime.presence import PRESENCE_TTL_SECONDS, PresenceStore, heartbeat_key
from app.schemas.note import Actor


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[Redis]:
    client = Redis.from_url(get_settings().redis_url, decode_responses=True)
    yield client
    await client.aclose()


def _actor(suffix: str = "1") -> Actor:
    return Actor(id=f"u-{suffix}", name=f"User {suffix}")


async def test_join_then_members_returns_that_member(redis_client: Redis) -> None:
    board_id = uuid.uuid4()
    store = PresenceStore(redis_client)
    connection_id = uuid.uuid4()

    joined = await store.join(board_id=board_id, connection_id=connection_id, actor=_actor())
    members = await store.members(board_id)

    assert [m.connection_id for m in members] == [connection_id]
    assert members[0].actor == joined.actor


async def test_leave_removes_the_member(redis_client: Redis) -> None:
    board_id = uuid.uuid4()
    store = PresenceStore(redis_client)
    connection_id = uuid.uuid4()
    await store.join(board_id=board_id, connection_id=connection_id, actor=_actor())

    await store.leave(board_id=board_id, connection_id=connection_id)

    assert await store.members(board_id) == []


async def test_stale_connection_is_evicted_on_read(redis_client: Redis) -> None:
    """A killed browser (or a dropped network) sends no close frame, so
    nothing ever calls `leave()` for it. The heartbeat ZSET is what makes this
    self-healing: `members()` drops any connection whose last heartbeat is
    older than PRESENCE_TTL_SECONDS before returning. This test simulates that
    abrupt-disconnect path directly by backdating the heartbeat score, rather
    than waiting PRESENCE_TTL_SECONDS in real time."""
    board_id = uuid.uuid4()
    store = PresenceStore(redis_client)
    connection_id = uuid.uuid4()
    await store.join(board_id=board_id, connection_id=connection_id, actor=_actor())

    stale_score = time.time() - PRESENCE_TTL_SECONDS - 10
    await redis_client.zadd(heartbeat_key(board_id), {str(connection_id): stale_score})

    assert await store.members(board_id) == []


async def test_heartbeat_refreshes_the_score_and_prevents_eviction(redis_client: Redis) -> None:
    board_id = uuid.uuid4()
    store = PresenceStore(redis_client)
    connection_id = uuid.uuid4()
    await store.join(board_id=board_id, connection_id=connection_id, actor=_actor())

    # Push the score to just short of stale, then heartbeat. If heartbeat()
    # did not refresh it, the very next read would evict this connection.
    near_stale_score = time.time() - PRESENCE_TTL_SECONDS + 5
    await redis_client.zadd(heartbeat_key(board_id), {str(connection_id): near_stale_score})
    await store.heartbeat(board_id=board_id, connection_id=connection_id)

    members = await store.members(board_id)
    assert [m.connection_id for m in members] == [connection_id]


async def test_set_editing_updates_and_returns_the_member(redis_client: Redis) -> None:
    board_id = uuid.uuid4()
    store = PresenceStore(redis_client)
    connection_id = uuid.uuid4()
    await store.join(board_id=board_id, connection_id=connection_id, actor=_actor())
    card_id = uuid.uuid4()

    updated = await store.set_editing(
        board_id=board_id, connection_id=connection_id, note_id=card_id
    )

    assert updated is not None
    assert updated.editing_note_id == card_id
    # And the change is durable in the store, not just the returned copy.
    members = await store.members(board_id)
    assert members[0].editing_note_id == card_id


async def test_same_actor_with_multiple_connections_are_separate_members(
    redis_client: Redis,
) -> None:
    """Presence is tracked per *connection*, not per actor - see
    PresenceMember's docstring. Someone with three tabs open genuinely has
    three sockets; the UI groups them by actor.id, but the model underneath
    must keep every connection distinct so disconnect handling stays
    unambiguous."""
    board_id = uuid.uuid4()
    store = PresenceStore(redis_client)
    actor = _actor("shared")
    connection_a = uuid.uuid4()
    connection_b = uuid.uuid4()

    await store.join(board_id=board_id, connection_id=connection_a, actor=actor)
    await store.join(board_id=board_id, connection_id=connection_b, actor=actor)

    members = await store.members(board_id)

    assert {m.connection_id for m in members} == {connection_a, connection_b}
    assert all(m.actor == actor for m in members)


async def test_presence_keys_carry_a_whole_key_expiry(redis_client) -> None:  # type: ignore[no-untyped-def]
    """Field-level eviction is lazy, so the keys need their own TTL.

    Stale CONNECTIONS are dropped on the next read of that board. But a board
    whose last connection died abruptly and which nobody ever opens again is
    never read again - so without a whole-key expiry its presence hash would
    sit in Redis forever. This was a real leak: an afternoon of testing left
    128 orphaned keys behind.
    """
    from app.realtime.presence import KEY_TTL_SECONDS, heartbeat_key, presence_key

    board_id = uuid.uuid4()
    store = PresenceStore(redis_client)
    await store.join(board_id=board_id, connection_id=uuid.uuid4(), actor=Actor(id="u", name="U"))

    for key in (presence_key(board_id), heartbeat_key(board_id)):
        ttl = await redis_client.ttl(key)
        assert 0 < ttl <= KEY_TTL_SECONDS, f"{key} has no expiry (ttl={ttl})"


async def test_a_heartbeat_pushes_the_key_expiry_out(redis_client) -> None:  # type: ignore[no-untyped-def]
    """A busy board must never expire underneath its live connections."""
    from app.realtime.presence import KEY_TTL_SECONDS, presence_key

    board_id = uuid.uuid4()
    connection_id = uuid.uuid4()
    store = PresenceStore(redis_client)
    await store.join(board_id=board_id, connection_id=connection_id, actor=Actor(id="u", name="U"))

    # Simulate the key having aged.
    await redis_client.expire(presence_key(board_id), 5)
    assert await redis_client.ttl(presence_key(board_id)) <= 5

    await store.heartbeat(board_id=board_id, connection_id=connection_id)
    assert await redis_client.ttl(presence_key(board_id)) > KEY_TTL_SECONDS - 10
