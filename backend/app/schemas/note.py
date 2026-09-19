"""Note request/response shapes.

The operations are deliberately separate rather than one generic patch. Each
one names the fields it owns, so a move can never carry a stale title in on its
back - see docs/canvas.md section 2.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.note import (
    MAX_ROTATION,
    MAX_SIZE,
    MIN_SIZE,
    WORLD_LIMIT,
    NoteTone,
)

# Annotated aliases rather than shared `Field(...)` instances. A bare Field()
# carries no default, so reusing one as `width: float | None = Extent` would
# silently make the field REQUIRED - which it did, until this was fixed. An
# Annotated type constrains the value and leaves the default to each field.
WorldCoord = Annotated[float, Field(ge=-WORLD_LIMIT, le=WORLD_LIMIT)]
Extent = Annotated[float, Field(ge=MIN_SIZE, le=MAX_SIZE)]
Rotation = Annotated[float, Field(ge=-MAX_ROTATION, le=MAX_ROTATION)]


class Actor(BaseModel):
    """Who performed an operation.

    A server-issued guest identity, read from a signed HTTP-only cookie - see
    app/core/session.py. It is deliberately NOT a field on any request model:
    the client has no way to state who it is, which is what makes impersonation
    impossible rather than merely discouraged.
    """

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=40)


class NoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    board_id: uuid.UUID
    title: str
    body: str
    x: float
    y: float
    width: float
    height: float
    rotation: float
    z_index: int
    tone: NoteTone
    version: int
    author_name: str | None
    created_at: datetime
    updated_at: datetime


class NoteCreate(BaseModel):
    title: str = Field(default="", max_length=500)
    body: str = Field(default="", max_length=4000)
    x: WorldCoord
    y: WorldCoord
    width: Extent | None = None
    height: Extent | None = None
    rotation: Rotation = 0.0
    tone: NoteTone = NoteTone.ACCENT

    op_id: uuid.UUID


class NoteContentUpdate(BaseModel):
    """Edit a note's text.

    The only operation that takes `expected_version`, and the only one that
    increments it. Position, size and stacking are not part of what `version`
    describes, so dragging a note never invalidates somebody's open editor.
    """

    title: str | None = Field(default=None, max_length=500)
    body: str | None = Field(default=None, max_length=4000)
    tone: NoteTone | None = None
    expected_version: int = Field(ge=1)

    op_id: uuid.UUID

    @model_validator(mode="after")
    def _at_least_one_field(self) -> NoteContentUpdate:
        if self.title is None and self.body is None and self.tone is None:
            raise ValueError("provide at least one of 'title', 'body' or 'tone'")
        return self


class NoteMove(BaseModel):
    """Commit a note's position.

    No `expected_version`: two people dragging the same note are physically
    fighting over it, and the right answer is that it ends up where the last
    one let go. The board row lock makes "last" well defined, so every client
    converges on the same value.
    """

    x: WorldCoord
    y: WorldCoord
    rotation: Rotation | None = None

    op_id: uuid.UUID


class NoteResize(BaseModel):
    """Resize, optionally moving the origin too.

    A drag on the top-left handle changes position and size together; sending
    them as one operation keeps that atomic instead of letting a note flicker
    through an intermediate shape.
    """

    width: Extent
    height: Extent
    x: WorldCoord | None = None
    y: WorldCoord | None = None

    op_id: uuid.UUID


class NoteReorder(BaseModel):
    """Raise a note to the front, or send it to the back.

    The client does not choose a z-index. It says which end it wants, and the
    server computes a value against the board's *current* stack - the same
    reason the Kanban version never accepted an array index.
    """

    to: str = Field(pattern="^(front|back)$")

    op_id: uuid.UUID


class NoteDelete(BaseModel):
    op_id: uuid.UUID
