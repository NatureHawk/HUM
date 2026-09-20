"""Note table — a sticky note at a position on a board's canvas.

Replaces the Kanban card. `column_key` and `rank` are gone rather than hidden:
they encoded "which of three lists, and where in it", which is a question the
canvas does not ask. Keeping them as invisible required fields would be exactly
the dead concept worth deleting.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.board import Board


class NoteTone(enum.StrEnum):
    """Which fill a note carries.

    Deliberately a small closed set, not a free colour field: HUM's language has
    one accent, and letting every note pick an arbitrary hex is how a coherent
    surface turns into confetti. Stored as a varchar with a CHECK for the same
    reason the old column was - a PG enum is awkward to extend in a migration.

    Four presets, curated from the existing HUM tokens rather than invented:
    one highlighter (the accent - used deliberately, not as a default), and
    three muted, paper-like fills that read as "a stack of real notes" instead
    of "a hue wheel". `INK` (a near-black fill) existed in Milestone 3 and is
    retired here - a corkboard of near-black notes is the exact flatness this
    palette exists to fix. Migration 0003 reassigns any existing `ink` notes
    to one of the three new fills rather than deleting them.
    """

    ACCENT = "accent"
    PAPER = "paper"
    PEACH = "peach"
    SKY = "sky"


TONES: tuple[NoteTone, ...] = (NoteTone.ACCENT, NoteTone.PAPER, NoteTone.PEACH, NoteTone.SKY)

# World units. A note is created at this size and resized from there.
DEFAULT_WIDTH = 240.0
DEFAULT_HEIGHT = 180.0
MIN_SIZE = 120.0
MAX_SIZE = 1200.0

# The canvas is large but not infinite: a bounded world keeps "fit to content"
# meaningful and stops a stray drag from losing a note at x = 1e9.
WORLD_LIMIT = 100_000.0
MAX_ROTATION = 20.0


class Note(Base):
    __tablename__ = "notes"

    # See models/board.py - required for async serialisation safety.
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012

    __table_args__ = (
        CheckConstraint(
            "tone IN ('accent', 'paper', 'peach', 'sky')",
            name="ck_notes_tone",
        ),
        # No lower bound: a note is created empty and typed into.
        CheckConstraint("length(title) <= 500", name="ck_notes_title_length"),
        CheckConstraint("version >= 1", name="ck_notes_version_positive"),
        CheckConstraint(
            f"width BETWEEN {MIN_SIZE} AND {MAX_SIZE} AND height BETWEEN {MIN_SIZE} AND {MAX_SIZE}",
            name="ck_notes_size_bounds",
        ),
        CheckConstraint(
            f"x BETWEEN -{WORLD_LIMIT} AND {WORLD_LIMIT} "
            f"AND y BETWEEN -{WORLD_LIMIT} AND {WORLD_LIMIT}",
            name="ck_notes_world_bounds",
        ),
        # The only hot read: every note on a board, already in paint order.
        Index("ix_notes_board_z", "board_id", "z_index"),
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

    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")

    # World coordinates - the board's own space, identical for every viewer at
    # every zoom level. The camera that turns these into screen pixels is
    # per-browser and is never stored. See docs/canvas.md section 4.
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)
    width: Mapped[float] = mapped_column(
        Float, nullable=False, default=DEFAULT_WIDTH, server_default=str(DEFAULT_WIDTH)
    )
    height: Mapped[float] = mapped_column(
        Float, nullable=False, default=DEFAULT_HEIGHT, server_default=str(DEFAULT_HEIGHT)
    )
    rotation: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")

    # Stacking order is board state, not DOM order: two people must see the
    # same note on top, which a render-order accident cannot guarantee.
    z_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    tone: Mapped[NoteTone] = mapped_column(
        String(16), nullable=False, default=NoteTone.ACCENT, server_default="accent"
    )

    # Optimistic-concurrency token for CONTENT only. Moving, resizing and
    # restacking deliberately do NOT bump it - otherwise dragging a note would
    # invalidate someone else's in-progress text edit on the same note. See
    # docs/canvas.md section 2.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    author_name: Mapped[str | None] = mapped_column(String(40), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    board: Mapped[Board] = relationship(back_populates="notes")
