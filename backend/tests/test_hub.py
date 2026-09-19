"""The multi-process proof.

RealtimeHub's entire reason to exist is the diagram in its module docstring:
a WebSocket lives on one process, and the HTTP mutation that should reach it
may land on another. This file builds exactly that shape - two independent
`RealtimeHub` instances, each with its own `Redis` client - so that "hub A"
and "hub B" share no Python memory at all, only the Redis server between
them. That is what a real second uvicorn worker would look like, and it is
the only way to actually exercise the pub/sub bridge rather than the local
dispatch a single-hub test would quietly fall back on.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

import pytest_asyncio
from redis.asyncio import Redis

from app.core.config import get_settings
from app.models.note import NoteTone
from app.realtime.hub import OUTBOUND_QUEUE_SIZE, Connection, RealtimeHub, events_channel
from app.schemas.events import NoteDeletedEvent
from app.schemas.note import Actor


class _FakeWebSocket:
    """Stands in for a starlette WebSocket. Connection only ever calls
    `send_text` on it, via the writer loop."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


def _actor() -> Actor:
    return Actor(id="u-hub", name="Hub Test")


def _card_deleted_event(board_id: uuid.UUID) -> NoteDeletedEvent:
    """The simplest BoardEvent variant - only the fields hub.publish_event
    actually touches (board_id, and the JSON it serialises to) matter here."""
    return NoteDeletedEvent(
        id=uuid.uuid4(),
        board_id=board_id,
        revision=1,
        occurred_at=datetime.now(UTC),
        op_id=uuid.uuid4(),
        actor=_actor(),
        note_id=uuid.uuid4(),
        column=NoteTone.ACCENT,
    )


async def _wait_until(
    predicate: Callable[[], bool], timeout: float = 2.0, step: float = 0.025
) -> bool:
    """Poll instead of a fixed sleep: delivery through Redis pub/sub crosses a
    real network round trip, so its latency is not worth guessing at."""
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return True
        await asyncio.sleep(step)
        elapsed += step
    return predicate()


@pytest_asyncio.fixture
async def redis_a() -> AsyncIterator[Redis]:
    client = Redis.from_url(get_settings().redis_url, decode_responses=True)
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def redis_b() -> AsyncIterator[Redis]:
    client = Redis.from_url(get_settings().redis_url, decode_responses=True)
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def hub_a(redis_a: Redis) -> AsyncIterator[RealtimeHub]:
    hub = RealtimeHub(redis_a)
    await hub.start()
    yield hub
    await hub.stop()


@pytest_asyncio.fixture
async def hub_b(redis_b: Redis) -> AsyncIterator[RealtimeHub]:
    hub = RealtimeHub(redis_b)
    await hub.start()
    yield hub
    await hub.stop()


async def test_event_published_on_hub_a_is_delivered_through_hub_b(
    hub_a: RealtimeHub, hub_b: RealtimeHub
) -> None:
    """The exact worker-A-to-worker-B path Redis exists for: hub A is the
    process that happened to handle the mutating HTTP request; hub B is the
    process holding the browser's socket and has never heard of hub A. They
    are bridged only by the Redis channel, never by shared memory."""
    board_id = uuid.uuid4()
    websocket = _FakeWebSocket()
    connection = Connection(websocket, board_id, _actor())
    connection.start_writer()
    await hub_b.register(connection)

    event = _card_deleted_event(board_id)
    await hub_a.publish_event(event)

    delivered = await _wait_until(lambda: len(websocket.sent) == 1)
    assert delivered, "event published on hub A never reached hub B's local connection"
    assert websocket.sent[0] == event.model_dump_json()

    await hub_b.unregister(connection)


async def test_hub_only_subscribes_to_boards_it_actually_serves(hub_b: RealtimeHub) -> None:
    """A process should not pay Redis traffic for boards it holds no sockets
    for (see hub.py's module docstring). Registering a connection for one
    board must not subscribe the hub to a different, unrelated board."""
    served_board = uuid.uuid4()
    unserved_board = uuid.uuid4()
    websocket = _FakeWebSocket()
    connection = Connection(websocket, served_board, _actor())
    connection.start_writer()

    await hub_b.register(connection)
    try:
        assert hub_b.local_connection_count(unserved_board) == 0
        assert events_channel(served_board) in hub_b._pubsub.channels
        assert events_channel(unserved_board) not in hub_b._pubsub.channels
    finally:
        await hub_b.unregister(connection)


async def test_hub_with_no_local_connections_never_subscribes(hub_b: RealtimeHub) -> None:
    """A freshly started hub holding no sockets at all must not be subscribed
    to anything - the idle state the reader loop's docstring describes."""
    assert hub_b.local_connection_count(uuid.uuid4()) == 0
    assert not hub_b._pubsub.subscribed


async def test_unregistering_the_last_connection_stops_local_delivery(
    hub_a: RealtimeHub, hub_b: RealtimeHub
) -> None:
    """Unsubscribing is the mirror image of the first test: once the last
    socket for a board leaves, an event published for that board afterwards
    must not be delivered locally - there is nothing left to deliver it to,
    and the hub should not still be listening for it."""
    board_id = uuid.uuid4()
    websocket = _FakeWebSocket()
    connection = Connection(websocket, board_id, _actor())
    connection.start_writer()
    await hub_b.register(connection)
    await hub_b.unregister(connection)

    await hub_a.publish_event(_card_deleted_event(board_id))
    # Negative assertion: give a (wrongly) still-subscribed reader a real
    # chance to misbehave before concluding nothing arrived.
    await asyncio.sleep(0.3)

    assert websocket.sent == []
    assert hub_b.local_connection_count(board_id) == 0


async def test_enqueue_overflow_sets_overflowed_without_raising_or_blocking() -> None:
    """The dispatcher serves every socket on the process from a single reader
    loop, so no one connection's queue may ever block it (see
    OUTBOUND_QUEUE_SIZE's comment in hub.py). Filling a connection's queue
    past capacity - with no writer draining it - must flag `overflowed` and
    return immediately rather than raising or hanging."""
    websocket = _FakeWebSocket()
    connection = Connection(websocket, uuid.uuid4(), _actor())
    # Deliberately never call start_writer(): nothing drains the queue, so it
    # fills for real instead of racing a consumer.

    for i in range(OUTBOUND_QUEUE_SIZE + 10):
        connection.enqueue(f"frame-{i}")

    assert connection.overflowed is True
    assert websocket.sent == []
