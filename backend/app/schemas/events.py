"""The event envelope.

This is the most important type in the system, and Milestone 3 changed only its
payloads. The JSON returned from a REST mutation is still byte-for-byte the
frame pushed over the WebSocket, so the frontend still has exactly one function
that advances board state and still does not care which transport delivered it.

Two conventions carried over unchanged:

* Note events carry the **whole note**, never a diff. That is safe even though
  each operation writes only its own fields, because the payload is assembled
  from committed server state *after* the write - so it is internally
  consistent no matter which fields were touched. Whole-note payloads are what
  keep `applyEvent` idempotent, which is what makes replay-after-resync safe.
* `presence.*` events carry **no revision**. Presence is ephemeral and is not
  part of the board's durable history, so it must not consume sequence numbers.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.schemas.note import Actor, NoteRead


class EventBase(BaseModel):
    id: uuid.UUID
    board_id: uuid.UUID

    # Board revision *after* this mutation. Gapless and monotonic, which is what
    # lets a client tell "duplicate" from "I missed one" with a comparison.
    revision: int

    occurred_at: datetime

    # Echoed from the client's request. This is how a browser recognises the
    # broadcast of its own optimistic change instead of applying it twice.
    op_id: uuid.UUID | None = None

    actor: Actor


class NoteCreatedEvent(EventBase):
    type: Literal["note.created"] = "note.created"
    note: NoteRead


class NoteUpdatedEvent(EventBase):
    """Text or tone changed. The only event that follows a version bump."""

    type: Literal["note.updated"] = "note.updated"
    note: NoteRead


class NoteMovedEvent(EventBase):
    type: Literal["note.moved"] = "note.moved"
    note: NoteRead
    from_x: float
    from_y: float


class NoteResizedEvent(EventBase):
    type: Literal["note.resized"] = "note.resized"
    note: NoteRead


class NoteReorderedEvent(EventBase):
    type: Literal["note.reordered"] = "note.reordered"
    note: NoteRead
    from_z_index: int


class NoteDeletedEvent(EventBase):
    type: Literal["note.deleted"] = "note.deleted"
    note_id: uuid.UUID


BoardEvent = Annotated[
    NoteCreatedEvent
    | NoteUpdatedEvent
    | NoteMovedEvent
    | NoteResizedEvent
    | NoteReorderedEvent
    | NoteDeletedEvent,
    Field(discriminator="type"),
]


class MutationResult(BaseModel):
    """What every mutating endpoint returns."""

    event: BoardEvent
    board_revision: int
