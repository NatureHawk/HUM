"""Stroke table — a freehand pen mark on a board's canvas.

Follows notes' shape deliberately: a stroke is a first-class spatial object,
not a screenshot. `points` is data (a polyline in WORLD coordinates), which is
what lets it stay put under pan and zoom and what makes "select it, delete it,
bring it to front" possible at all. A bitmap could do none of that.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.board import Board

# World units, same space notes live in. A stroke this wide would be a
# hairline; this thick, a fat marker. Both ends are "sensible marker", not
# "brush engine".
MIN_STROKE_WIDTH = 1.0
MAX_STROKE_WIDTH = 24.0
DEFAULT_STROKE_WIDTH = 4.0

DEFAULT_STROKE_COLOR = "#15150f"  # ink

# A stroke is sampled while the pointer moves, not fit to a curve, so its point
# count is bounded rather than infinite - generous enough for a long, slow
# gesture at the ~30/s streaming rate, and small enough that one row can never
# become an abuse vector.
MIN_STROKE_POINTS = 2
MAX_STROKE_POINTS = 4000


class Stroke(Base):
    __tablename__ = "strokes"

    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012

    __table_args__ = (
        CheckConstraint(
            f"width BETWEEN {MIN_STROKE_WIDTH} AND {MAX_STROKE_WIDTH}",
            name="ck_strokes_width_bounds",
        ),
        CheckConstraint("version >= 1", name="ck_strokes_version_positive"),
        # The only hot read: every stroke on a board, already in paint order.
        Index("ix_strokes_board_z", "board_id", "z_index"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    board_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # A polyline: [[x, y], [x, y], ...] in WORLD coordinates, identical for
    # every viewer at every zoom level - the same rule as a note's x/y. Stored
    # as JSONB rather than a bitmap or a second table of point rows: it is read
    # and written whole (a stroke is never edited point-by-point after it is
    # committed), so one JSON column is the simplest thing that is still real
    # data rather than a picture of data.
    points: Mapped[list[list[float]]] = mapped_column(JSONB, nullable=False)

    color: Mapped[str] = mapped_column(
        String(9), nullable=False, default=DEFAULT_STROKE_COLOR, server_default=DEFAULT_STROKE_COLOR
    )
    width: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=DEFAULT_STROKE_WIDTH,
        server_default=str(DEFAULT_STROKE_WIDTH),
    )

    # Stacking order, sharing ONE cursor with notes (Board.z_cursor) so a
    # drawing and a note interleave correctly instead of keeping two stacks
    # that only coincidentally agree. See models/board.py.
    z_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Present for schema symmetry with Note and because a future "edit a
    # stroke's colour" operation would guard on it exactly as note content
    # does. Nothing currently increments it past 1 - a stroke today is created
    # and deleted, never revised in place.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    author_name: Mapped[str | None] = mapped_column(String(40), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    board: Mapped[Board] = relationship(back_populates="strokes")
