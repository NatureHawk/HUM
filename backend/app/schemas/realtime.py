"""The WebSocket protocol.

Two rules shape everything here.

**The socket transports events; it does not accept mutations.** Every board
change still goes through the REST path, the service layer, and a Postgres
transaction. The frames a client may send - `ping`, `resync`, `editing`,
`pointer` and `drag` - change no durable state. That is why there is exactly one
mutation implementation, not two.

`pointer` and `drag` are worth being precise about. A drag produces a position
every animation frame; persisting those would be thousands of writes and
thousands of revisions a minute, and would make the revision sequence
meaningless. So a `drag` frame is not a mutation - it is a rumour about where a
note is currently being carried, it is never written down, and it expires the
moment it stops arriving. The single REST call on pointer release is what
actually commits the position. See docs/canvas.md section 3.

**A client can never name its own actor in a message.** The actor is bound once,
at connection time, and the server stamps it onto everything that connection
produces. Putting a different user id in a frame achieves nothing because no
frame has a field for one. When real authentication replaces the self-declared
identity, only the handshake changes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.models.note import WORLD_LIMIT
from app.schemas.board import BoardRead
from app.schemas.note import Actor, NoteRead


class PresenceMember(BaseModel):
    """One live connection.

    Presence is tracked per *connection*, not per user: a person with three
    tabs open genuinely has three sockets, and pretending otherwise makes
    disconnect handling ambiguous. The UI groups these by `actor.id` so that
    person still appears once - see docs/realtime.md.
    """

    connection_id: uuid.UUID
    actor: Actor
    connected_at: datetime

    # Which note this connection currently has open in its editor, if any.
    # Ephemeral and advisory: it is a hint for collaborators, never a lock.
    editing_note_id: uuid.UUID | None = None


# --------------------------------------------------------------------------
# Server -> client
# --------------------------------------------------------------------------


class SyncFrame(BaseModel):
    """The first frame on every connection, and the answer to `resync`.

    Carries everything needed to start from a state that is known-current:
    the board, its cards, the revision they were read at, who this connection
    is, and who else is here.
    """

    type: Literal["sync"] = "sync"
    connection_id: uuid.UUID
    actor: Actor
    board: BoardRead
    notes: list[NoteRead]
    revision: int
    presence: list[PresenceMember]


class PresenceFrame(BaseModel):
    """A change to exactly one connection's presence.

    Carries a single `member`, not the whole room. That is the opposite of the
    obvious design, and it is deliberate.

    A full-list broadcast has to compute the list at publish time, and two
    presence changes that interleave then race: a `left` published slightly
    later can carry a list assembled slightly *earlier*, and overwrite a newer
    one. That is not hypothetical - a browser reconnecting (or React
    re-mounting in development) closes one socket and opens another within
    milliseconds, and the stale list drops the new connection.

    Per-connection deltas have no such problem. Frames for different
    connections are commutative, and frames for the *same* connection are
    published in order by the one handler that owns it. The client keys its map
    on `member.connection_id` and applies join/leave/update directly; the
    `sync` frame supplies the authoritative baseline it starts from.

    Note there is no `revision`: presence is not part of the board's durable
    history and must never consume a sequence number, or a tab opening would
    make every other client believe it had missed a card change.
    """

    type: Literal["presence.joined", "presence.left", "presence.updated"]
    board_id: uuid.UUID
    occurred_at: datetime
    member: PresenceMember


class PointerFrame(BaseModel):
    """Somebody's cursor, in world coordinates.

    Ephemeral: broadcast, never stored, never given a revision. A cursor is only
    interesting while it is moving, and a client that misses one simply sees the
    next one 50ms later. Writing these to Postgres would be the same category
    error as persisting presence.
    """

    type: Literal["presence.pointer"] = "presence.pointer"
    board_id: uuid.UUID
    connection_id: uuid.UUID
    actor: Actor
    x: float
    y: float


class DragFrame(BaseModel):
    """Somebody is carrying a note, and it is currently here.

    Also ephemeral, for the same reason and with the same consequences: no
    revision, no write, no place in the board's history. Observers render the
    note at this position until `settled` arrives, then fall back to board
    state - which the drag's REST commit updates a moment later.
    """

    type: Literal["presence.drag"] = "presence.drag"
    board_id: uuid.UUID
    connection_id: uuid.UUID
    actor: Actor
    note_id: uuid.UUID
    x: float
    y: float
    settled: bool


class ErrorFrame(BaseModel):
    """A protocol error. Never carries an exception or a stack trace."""

    type: Literal["error"] = "error"
    code: Literal[
        "unauthorized",
        "board_not_found",
        "invalid_event",
        "stale_operation",
        "rate_limited",
        "message_too_large",
        "server_error",
    ]
    message: str


class PongFrame(BaseModel):
    type: Literal["pong"] = "pong"
    server_time: datetime


ServerFrame = Annotated[
    SyncFrame | PresenceFrame | PointerFrame | DragFrame | ErrorFrame | PongFrame,
    Field(discriminator="type"),
]


# Board events travel on this socket unwrapped. Each already carries its own
# `type` ("card.created", "card.moved", ...) - the same field every frame above
# discriminates on - and is byte-for-byte the envelope the REST mutation
# response returns, which is what lets one client-side applyEvent serve both.


# --------------------------------------------------------------------------
# Client -> server
# --------------------------------------------------------------------------


class PingMessage(BaseModel):
    type: Literal["ping"] = "ping"


class ResyncMessage(BaseModel):
    """Ask for a fresh SyncFrame - sent after the client detects a revision gap."""

    type: Literal["resync"] = "resync"


class EditingMessage(BaseModel):
    """ "I have this note open." Advisory only; it is not a lock."""

    type: Literal["editing"] = "editing"
    note_id: uuid.UUID | None = None


class PointerMessage(BaseModel):
    """Where this person's cursor is, in WORLD coordinates.

    World rather than screen, because every receiver is looking through their
    own camera: a screen position would land somewhere different for each of
    them. Ephemeral by design - a cursor five seconds old is noise - so it is
    broadcast and never stored.
    """

    type: Literal["pointer"] = "pointer"
    x: float = Field(ge=-WORLD_LIMIT, le=WORLD_LIMIT)
    y: float = Field(ge=-WORLD_LIMIT, le=WORLD_LIMIT)


class DragMessage(BaseModel):
    """A note is being carried, and is currently here.

    Lets other people watch the movement at pointer speed without anybody
    writing to Postgres at pointer speed. `settled` marks the last frame of a
    gesture, so observers can drop the rumour and fall back to board state -
    which the drag's REST commit is about to update anyway.
    """

    type: Literal["drag"] = "drag"
    note_id: uuid.UUID
    x: float = Field(ge=-WORLD_LIMIT, le=WORLD_LIMIT)
    y: float = Field(ge=-WORLD_LIMIT, le=WORLD_LIMIT)
    settled: bool = False


ClientMessage = Annotated[
    PingMessage | ResyncMessage | EditingMessage | PointerMessage | DragMessage,
    Field(discriminator="type"),
]


class ClientEnvelope(BaseModel):
    """Parse target for inbound frames.

    Anything that is not one of the permitted shapes fails validation here and
    is answered with `invalid_event`. There is deliberately no passthrough for
    arbitrary JSON.
    """

    message: ClientMessage


# WebSocket close codes. 4000-4999 is the range reserved for applications.
CLOSE_UNAUTHORIZED = 4401
CLOSE_BOARD_NOT_FOUND = 4404
CLOSE_INVALID = 4400
CLOSE_RATE_LIMITED = 4429
CLOSE_SERVER_ERROR = 1011

# An inbound frame larger than this is refused without being parsed.
MAX_MESSAGE_BYTES = 8 * 1024
