"""Stroke request/response shapes.

Mirrors notes' shape deliberately (see schemas/note.py): each operation names
only the fields it owns, and the client never chooses a `z_index` - it says
which end of the stack it wants and the server resolves that against the
board's current state under the lock.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.note import WORLD_LIMIT
from app.models.stroke import (
    MAX_STROKE_POINTS,
    MAX_STROKE_WIDTH,
    MIN_STROKE_POINTS,
    MIN_STROKE_WIDTH,
)

StrokeWidth = Annotated[float, Field(ge=MIN_STROKE_WIDTH, le=MAX_STROKE_WIDTH)]

# "#15150f" or "#fff" - a plain hex colour, not an arbitrary CSS colour string.
# Kept loose (any hex) rather than a closed set like NoteTone: a pen stroke is
# a mark, not a themed object, and there is exactly one control for it.
_HEX_COLOR_PATTERN = r"^#[0-9a-fA-F]{3}$|^#[0-9a-fA-F]{6}$"
StrokeColor = Annotated[str, Field(pattern=_HEX_COLOR_PATTERN)]


def _validate_points(points: list[list[float]]) -> list[list[float]]:
    if not (MIN_STROKE_POINTS <= len(points) <= MAX_STROKE_POINTS):
        raise ValueError(
            f"a stroke must have between {MIN_STROKE_POINTS} and {MAX_STROKE_POINTS} points"
        )
    for point in points:
        if len(point) != 2:
            raise ValueError("each point must be an [x, y] pair")
        x, y = point
        if not (-WORLD_LIMIT <= x <= WORLD_LIMIT) or not (-WORLD_LIMIT <= y <= WORLD_LIMIT):
            raise ValueError("a stroke point fell outside the board's world bounds")
    return points


class StrokeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    board_id: uuid.UUID
    points: list[list[float]]
    color: str
    width: float
    z_index: int
    version: int
    author_name: str | None
    created_at: datetime
    updated_at: datetime


class StrokeCreate(BaseModel):
    points: list[list[float]]
    color: StrokeColor = "#15150f"
    width: StrokeWidth = 4.0

    op_id: uuid.UUID

    @field_validator("points")
    @classmethod
    def _points_valid(cls, value: list[list[float]]) -> list[list[float]]:
        return _validate_points(value)


class StrokeReorder(BaseModel):
    """Raise a stroke to the front, or send it to the back.

    Shares the same shared board cursor as a note's "front" - see
    app/repositories/boards.py:bump_z_cursor - so grabbing a stroke and
    grabbing a note compete for the same single stack.
    """

    to: str = Field(pattern="^(front|back)$")

    op_id: uuid.UUID


class StrokeDelete(BaseModel):
    op_id: uuid.UUID
