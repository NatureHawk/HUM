"""The realtime hub: local sockets on one side, Redis pub/sub on the other.

The problem it exists to solve is process-crossing. A WebSocket is held by one
specific process. The HTTP mutation that should reach that socket may land on a
different one, and neither can see the other's memory:

    Browser A ──POST──▶ worker 1                 worker 2 ◀──socket── Browser B
                           │                         ▲
                           └────▶ Redis channel ─────┘
                                  board:{id}:events

So every process keeps two things: the set of sockets it is holding, and a
subscription to the channel of each board it is holding sockets for. It
subscribes on the first local connection to a board and unsubscribes on the
last, so a process only carries traffic for boards it actually serves.

**Published events come back through Redis even for local sockets.** The
publisher does not also deliver directly. A single delivery path is worth the
loopback: there is no way to double-deliver to a local client, and every client
on every process observes the same order.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid

from fastapi import WebSocket
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.schemas.events import BoardEvent
from app.schemas.note import Actor
from app.schemas.realtime import DragFrame, PointerFrame, PresenceFrame

logger = logging.getLogger(__name__)

# Bounded so one stalled browser cannot back up the dispatcher for everyone
# else. Overflow closes that connection; it reconnects and resyncs, which is
# strictly better than degrading the whole board.
OUTBOUND_QUEUE_SIZE = 256


def events_channel(board_id: uuid.UUID) -> str:
    return f"board:{board_id}:events"


class Connection:
    """One WebSocket, with its own outbound queue and writer task."""

    def __init__(self, websocket: WebSocket, board_id: uuid.UUID, actor: Actor) -> None:
        self.id = uuid.uuid4()
        self.websocket = websocket
        self.board_id = board_id
        self.actor = actor
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=OUTBOUND_QUEUE_SIZE)
        self._writer: asyncio.Task[None] | None = None
        self.overflowed = False

    def start_writer(self) -> None:
        self._writer = asyncio.create_task(self._writer_loop())

    async def close_writer(self) -> None:
        if self._writer is None:
            return
        self._writer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._writer

    def enqueue(self, text: str) -> None:
        """Hand a frame to this connection's writer.

        Never blocks: the dispatcher serves every socket on the process, so it
        must not wait on any one of them.
        """
        try:
            self._queue.put_nowait(text)
        except asyncio.QueueFull:
            self.overflowed = True
            logger.warning("connection %s outbound queue full; closing", self.id)

    async def send_now(self, text: str) -> None:
        """Send immediately, bypassing the queue.

        Used only for the handshake and for errors, where ordering relative to
        the queue does not matter because the queue is still empty.
        """
        await self.websocket.send_text(text)

    async def _writer_loop(self) -> None:
        while True:
            text = await self._queue.get()
            try:
                await self.websocket.send_text(text)
            except Exception:
                return


class RealtimeHub:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._connections: dict[uuid.UUID, set[Connection]] = {}
        self._pubsub = redis.pubsub(ignore_subscribe_messages=True)
        self._reader: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def redis(self) -> Redis:
        """Shared client, so presence and pub/sub use one connection pool."""
        return self._redis

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._reader is None:
            self._reader = asyncio.create_task(self._reader_loop())

    async def stop(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        with contextlib.suppress(Exception):
            await self._pubsub.aclose()  # type: ignore[no-untyped-call]

    # -- local registry ----------------------------------------------------

    async def register(self, connection: Connection) -> None:
        """Add a socket, subscribing this process to the board if it is the first."""
        async with self._lock:
            board_connections = self._connections.setdefault(connection.board_id, set())
            first_for_board = len(board_connections) == 0
            board_connections.add(connection)
            if first_for_board:
                await self._pubsub.subscribe(events_channel(connection.board_id))
        # The writer is deliberately NOT started here. Registering first means
        # events that land while the endpoint is still reading its snapshot are
        # captured in the queue instead of being missed - but they must not
        # reach the client before the sync frame does. The endpoint calls
        # start_writer() once the handshake is on the wire, which flushes them
        # in order. See app/api/routes/ws.py.

    async def unregister(self, connection: Connection) -> None:
        """Remove a socket, unsubscribing if it was the last one for that board."""
        async with self._lock:
            board_connections = self._connections.get(connection.board_id)
            if board_connections is not None:
                board_connections.discard(connection)
                if not board_connections:
                    del self._connections[connection.board_id]
                    with contextlib.suppress(RedisError):
                        await self._pubsub.unsubscribe(events_channel(connection.board_id))
        await connection.close_writer()

    def local_connection_count(self, board_id: uuid.UUID) -> int:
        """Sockets this process holds for a board. Not the board's total."""
        return len(self._connections.get(board_id, ()))

    # -- publishing --------------------------------------------------------

    async def publish_event(self, event: BoardEvent) -> None:
        """Fan a committed board event out to every process serving this board.

        A publish failure never fails the mutation that produced it: the write
        is already durable in Postgres. Clients simply do not hear about it
        until their own next mutation exposes a revision gap and they resync.
        That is a degraded mode, not a corrupt one.
        """
        # The event goes on the wire unwrapped. It already carries `type`
        # ("card.created", ...), which is the same field every other frame
        # discriminates on, and it is byte-for-byte the envelope the REST
        # response returns - so the client feeds both through one applyEvent.
        await self._publish(event.board_id, event.model_dump_json())

    async def publish_presence(self, frame: PresenceFrame) -> None:
        await self._publish(frame.board_id, frame.model_dump_json())

    async def publish_ephemeral(self, board_id: uuid.UUID, frame: PointerFrame | DragFrame) -> None:
        """Fan out a cursor or a drag position.

        Same channel and same delivery path as everything else, but nothing is
        written down and no revision is consumed. These frames describe what is
        happening this instant; a client that misses one gets the next one
        milliseconds later, and a board's history is entirely unaffected by
        whether any of them arrived.
        """
        await self._publish(board_id, frame.model_dump_json())

    async def _publish(self, board_id: uuid.UUID, payload: str) -> None:
        try:
            await self._redis.publish(events_channel(board_id), payload)
        except RedisError:
            logger.exception("failed to publish to board %s; clients will resync", board_id)

    # -- delivery ----------------------------------------------------------

    async def _reader_loop(self) -> None:
        """Pump Redis messages into local sockets.

        Errors are swallowed and retried: this task must outlive a Redis blip,
        because if it dies the process stops delivering realtime entirely while
        still looking healthy.
        """
        while True:
            # A PubSub with no channels has no connection yet, and get_message
            # raises rather than blocking. The process legitimately sits in this
            # state whenever it is holding no sockets, so it is a normal idle
            # condition, not an error.
            if not self._pubsub.subscribed:
                await asyncio.sleep(0.05)
                continue

            try:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("realtime reader failed; retrying")
                await asyncio.sleep(1.0)
                continue

            if message is None or message.get("type") != "message":
                continue

            channel = message["channel"]
            data = message["data"]
            if isinstance(channel, bytes):
                channel = channel.decode()
            if isinstance(data, bytes):
                data = data.decode()

            board_id = _board_id_from_channel(channel)
            if board_id is not None:
                self._dispatch_local(board_id, data)

    def _dispatch_local(self, board_id: uuid.UUID, payload: str) -> None:
        for connection in tuple(self._connections.get(board_id, ())):
            connection.enqueue(payload)


def _board_id_from_channel(channel: str) -> uuid.UUID | None:
    parts = channel.split(":")
    if len(parts) != 3 or parts[0] != "board" or parts[2] != "events":
        return None
    try:
        return uuid.UUID(parts[1])
    except ValueError:
        return None


_hub: RealtimeHub | None = None


def init_hub(redis: Redis) -> RealtimeHub:
    global _hub
    _hub = RealtimeHub(redis)
    return _hub


def get_hub() -> RealtimeHub | None:
    """The process-wide hub, or None before startup (and in unit tests)."""
    return _hub


async def shutdown_hub() -> None:
    global _hub
    if _hub is not None:
        await _hub.stop()
        _hub = None
