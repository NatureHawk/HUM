"""The board WebSocket endpoint.

## The handshake, and why its order matters

A client that reads the board first and subscribes second has a hole: anything
committed between the read and the subscribe is lost, silently, and the client
believes it is current. So the order here is the other way round:

    1. accept the socket
    2. validate the board exists          <- access check, before anything else
    3. register with the hub              <- subscribed from this instant on;
                                             arriving events queue up
    4. read the snapshot at revision R    <- one transaction, board + cards
    5. join presence
    6. send the `sync` frame              <- straight out, ahead of the queue
    7. start the writer                   <- flushes everything buffered in (3-5)

Events buffered during steps 4-5 are delivered immediately after `sync`. Any
with `revision <= R` are already reflected in the snapshot and the client drops
them by revision; any above R apply in order. Nothing can slip through the gap,
because the subscription predates the read.

## Sessions

The socket lives for minutes or hours; a database connection must not. So this
endpoint does not take a session dependency. It opens a short-lived session for
the board check, for the snapshot, and for a resync, and holds none in between.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.api.deps import socket_actor
from app.core.errors import NotFoundError
from app.db.session import SessionFactory
from app.realtime.hub import Connection, get_hub
from app.realtime.presence import PresenceStore
from app.schemas.board import BoardSnapshot
from app.schemas.realtime import (
    CLOSE_BOARD_NOT_FOUND,
    CLOSE_RATE_LIMITED,
    CLOSE_SERVER_ERROR,
    CLOSE_UNAUTHORIZED,
    MAX_MESSAGE_BYTES,
    ClientEnvelope,
    DragFrame,
    DragMessage,
    EditingMessage,
    ErrorFrame,
    PingMessage,
    PointerFrame,
    PointerMessage,
    PongFrame,
    PresenceFrame,
    PresenceMember,
    ResyncMessage,
    SyncFrame,
)
from app.services import boards as board_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["realtime"])

# Clients send only ping / resync / editing, so this is far above normal use
# and only trips on something misbehaving.
RATE_LIMIT_MESSAGES = 60
RATE_LIMIT_WINDOW_SECONDS = 10.0


class _RateLimiter:
    """Fixed-window counter, per connection."""

    def __init__(self) -> None:
        self._window_start = time.monotonic()
        self._count = 0

    def allow(self) -> bool:
        now = time.monotonic()
        if now - self._window_start >= RATE_LIMIT_WINDOW_SECONDS:
            self._window_start = now
            self._count = 0
        self._count += 1
        return self._count <= RATE_LIMIT_MESSAGES


async def _send_error(websocket: WebSocket, code: str, message: str) -> None:
    """Report a protocol error without leaking anything internal."""
    with contextlib.suppress(Exception):
        await websocket.send_text(ErrorFrame(code=code, message=message).model_dump_json())  # type: ignore[arg-type]


def _presence_frame(
    kind: str,
    board_id: uuid.UUID,
    member: PresenceMember,
) -> PresenceFrame:
    return PresenceFrame(
        type=kind,  # type: ignore[arg-type]
        board_id=board_id,
        occurred_at=datetime.now(UTC),
        member=member,
    )


@router.websocket("/ws/boards/{board_id}")
async def board_socket(websocket: WebSocket, board_id: uuid.UUID) -> None:
    """Realtime channel for one board.

    The actor comes from the signed session cookie, which the browser sends with
    the WebSocket handshake like any other request. There is no `actor_id` query
    parameter any more and no actor field on any client frame, so a connection
    cannot claim to be anybody: it is whoever the server said it was when it
    issued the cookie.

    Every frame this connection produces is stamped with that identity.
    """
    hub = get_hub()
    if hub is None:  # pragma: no cover - only if startup failed
        await websocket.close(code=CLOSE_SERVER_ERROR)
        return

    actor = socket_actor(websocket)
    if actor is None:
        # No cookie, or a forged/expired one. The client answers this by
        # calling POST /api/session and reconnecting.
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="no guest session")
        return

    await websocket.accept()

    # Step 2: the access check. Every socket goes through the same service the
    # REST layer uses, so no connection can bypass the board's existence and
    # (later) permission rules.
    async with SessionFactory() as session:
        try:
            await board_service.ensure_exists(session, board_id)
        except NotFoundError:
            await _send_error(websocket, "board_not_found", "That board does not exist.")
            await websocket.close(code=CLOSE_BOARD_NOT_FOUND)
            return

    connection = Connection(websocket, board_id, actor)
    presence = PresenceStore(hub.redis)

    # Step 3: subscribed from here on. Anything committed from now until the
    # sync frame goes out is buffered, not lost.
    await hub.register(connection)

    try:
        # Steps 4-6.
        member = await presence.join(board_id=board_id, connection_id=connection.id, actor=actor)
        members = await presence.members(board_id)
        snapshot = await _read_snapshot(board_id)
        if snapshot is None:
            await _send_error(websocket, "board_not_found", "That board does not exist.")
            await websocket.close(code=CLOSE_BOARD_NOT_FOUND)
            return

        await connection.send_now(
            SyncFrame(
                connection_id=connection.id,
                actor=actor,
                board=snapshot.board,
                notes=snapshot.notes,
                revision=snapshot.revision,
                presence=members,
            ).model_dump_json()
        )

        # Step 7: release whatever queued up behind the handshake.
        connection.start_writer()

        await hub.publish_presence(_presence_frame("presence.joined", board_id, member))

        await _receive_loop(websocket, connection, presence)

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("websocket failed for board %s", board_id)
        await _send_error(websocket, "server_error", "Something went wrong.")
        with contextlib.suppress(Exception):
            await websocket.close(code=CLOSE_SERVER_ERROR)
    finally:
        # Leave presence and tell the room, whatever the reason for leaving.
        with contextlib.suppress(Exception):
            await presence.leave(board_id=board_id, connection_id=connection.id)
            await hub.publish_presence(
                _presence_frame(
                    "presence.left",
                    board_id,
                    PresenceMember(
                        connection_id=connection.id,
                        actor=actor,
                        connected_at=datetime.now(UTC),
                    ),
                )
            )
        await hub.unregister(connection)


async def _receive_loop(
    websocket: WebSocket,
    connection: Connection,
    presence: PresenceStore,
) -> None:
    hub = get_hub()
    assert hub is not None
    limiter = _RateLimiter()

    while True:
        raw = await websocket.receive_text()

        if not limiter.allow():
            await _send_error(websocket, "rate_limited", "Too many messages.")
            await websocket.close(code=CLOSE_RATE_LIMITED)
            return

        # Checked before parsing: an oversized frame should never be handed to
        # the JSON decoder in the first place.
        if len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
            await _send_error(websocket, "message_too_large", "That message is too large.")
            continue

        try:
            envelope = ClientEnvelope.model_validate({"message": json.loads(raw)})
        except (ValueError, ValidationError):
            # Untrusted input: anything that is not one of the three permitted
            # shapes is rejected here, and never reaches the rest of the system.
            await _send_error(websocket, "invalid_event", "Unrecognised message.")
            continue

        message = envelope.message

        if isinstance(message, PingMessage):
            # Doubles as the presence heartbeat: a connection that stops
            # pinging expires out of the presence set.
            await presence.heartbeat(board_id=connection.board_id, connection_id=connection.id)
            await connection.send_now(PongFrame(server_time=datetime.now(UTC)).model_dump_json())

        elif isinstance(message, ResyncMessage):
            snapshot = await _read_snapshot(connection.board_id)
            if snapshot is None:
                await _send_error(websocket, "board_not_found", "That board is gone.")
                await websocket.close(code=CLOSE_BOARD_NOT_FOUND)
                return
            members = await presence.members(connection.board_id)
            await connection.send_now(
                SyncFrame(
                    connection_id=connection.id,
                    actor=connection.actor,
                    board=snapshot.board,
                    notes=snapshot.notes,
                    revision=snapshot.revision,
                    presence=members,
                ).model_dump_json()
            )

        elif isinstance(message, EditingMessage):
            updated = await presence.set_editing(
                board_id=connection.board_id,
                connection_id=connection.id,
                note_id=message.note_id,
            )
            if updated is not None:
                await hub.publish_presence(
                    _presence_frame("presence.updated", connection.board_id, updated)
                )

        elif isinstance(message, PointerMessage):
            # Ephemeral: broadcast, never stored, no revision. A cursor is only
            # interesting while it is moving, and a client that misses one sees
            # the next one 50ms later. The actor is the connection's, never the
            # message's - a client cannot move somebody else's cursor.
            await hub.publish_ephemeral(
                connection.board_id,
                PointerFrame(
                    board_id=connection.board_id,
                    connection_id=connection.id,
                    actor=connection.actor,
                    x=message.x,
                    y=message.y,
                ),
            )

        elif isinstance(message, DragMessage):
            # Also ephemeral, and for a concrete reason: a drag produces a
            # position every animation frame, and persisting those would be
            # thousands of writes and thousands of revisions per minute. This
            # frame is a rumour about where a note is being carried; the single
            # REST call on release is what actually commits it.
            await hub.publish_ephemeral(
                connection.board_id,
                DragFrame(
                    board_id=connection.board_id,
                    connection_id=connection.id,
                    actor=connection.actor,
                    note_id=message.note_id,
                    x=message.x,
                    y=message.y,
                    settled=message.settled,
                ),
            )


async def _read_snapshot(board_id: uuid.UUID) -> BoardSnapshot | None:
    """Board and cards at one revision, in one transaction.

    Reusing the same service the REST snapshot endpoint calls, so there is a
    single definition of "the canonical state of a board".
    """
    async with SessionFactory() as session:
        try:
            return await board_service.get_snapshot(session, board_id)
        except NotFoundError:
            return None
