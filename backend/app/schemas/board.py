"""Board request/response shapes."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.note import NoteRead
from app.schemas.stroke import StrokeRead


class BoardCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class BoardRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    revision: int
    created_at: datetime
    updated_at: datetime


class BoardSummary(BoardRead):
    """Board list entry - adds the note count so the lobby can show something
    true about each board without fetching all of its notes."""

    note_count: int


class BoardSnapshot(BaseModel):
    """The complete durable state of one board at one revision.

    Board and notes are read in a single transaction, so `revision` is
    guaranteed to describe exactly the note list beside it. That atomicity is
    what makes this safe to use as the recovery point after a desync - a
    snapshot stitched from two reads could land a client in a state that never
    actually existed.
    """

    board: BoardRead
    notes: list[NoteRead]
    strokes: list[StrokeRead]
    revision: int
