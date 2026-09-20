"""note palette, freehand strokes, and a shared stacking cursor

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-20

Three independent changes, bundled because they are one visual/interaction
milestone (see docs/canvas.md and the SyncBoard canvas-polish brief):

1. `notes.tone` drops `ink` (a near-black fill) and gains `peach` and `sky` -
   two more muted, paper-like presets alongside the existing `accent` and
   `paper`. Existing `ink` notes are NOT deleted or left to violate the new
   CHECK; they are reassigned, deterministically (by a hash of their own id,
   so the same note gets the same new tone on every run of this migration and
   on every restart afterwards - never re-randomised), to one of the three
   non-accent tones. `accent` is deliberately excluded from that reassignment:
   it is meant to stay a rare, deliberate highlight, not a fallback.

2. A new `strokes` table for freehand pen marks - first-class spatial objects
   with the same shape discipline as notes (world-coordinate data, not a
   bitmap).

3. `boards.z_cursor`: one monotonic counter shared by notes and strokes for
   "bring to front", backfilled from each board's current highest note
   z_index so existing stacks are undisturbed. See app/models/board.py and
   app/repositories/boards.py:bump_z_cursor for why one counter, not two
   per-table maxima.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kept in step with app/models/stroke.py.
MIN_STROKE_WIDTH = 1.0
MAX_STROKE_WIDTH = 24.0
DEFAULT_STROKE_WIDTH = 4.0
DEFAULT_STROKE_COLOR = "#15150f"


def upgrade() -> None:
    # -- 1. note palette -----------------------------------------------------

    # The old constraint only permits ('accent', 'paper', 'ink'), so it has to
    # come off BEFORE the backfill writes 'peach'/'sky' - and the new one only
    # goes on AFTER the backfill clears every 'ink' row, or it would reject the
    # rows the backfill has not reached yet.
    op.drop_constraint("ck_notes_tone", "notes", type_="check")

    # Spread existing `ink` notes across the three non-accent tones by a hash
    # of their id. Deterministic (same input -> same output, forever) rather
    # than `random()`, so a note does not change colour again on the next
    # deploy - see docs/canvas.md's note on deterministic tilts for the same
    # reasoning applied to rotation in migration 0002.
    op.execute(
        sa.text("""
        UPDATE notes
        SET tone = CASE (('x' || substr(md5(id::text), 1, 8))::bit(32)::bigint % 3)
                WHEN 0 THEN 'paper'
                WHEN 1 THEN 'peach'
                ELSE 'sky'
            END
        WHERE tone = 'ink'
        """)
    )

    op.create_check_constraint(
        "ck_notes_tone", "notes", "tone IN ('accent', 'paper', 'peach', 'sky')"
    )

    # -- 2. strokes table -----------------------------------------------------

    op.create_table(
        "strokes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("board_id", postgresql.UUID(as_uuid=True), nullable=False),
        # A polyline in WORLD coordinates: [[x, y], ...]. Data, not a bitmap -
        # see app/models/stroke.py.
        sa.Column("points", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "color", sa.String(length=9), server_default=DEFAULT_STROKE_COLOR, nullable=False
        ),
        sa.Column("width", sa.Float(), server_default=str(DEFAULT_STROKE_WIDTH), nullable=False),
        sa.Column("z_index", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("author_name", sa.String(length=40), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_strokes"),
        sa.ForeignKeyConstraint(
            ["board_id"],
            ["boards.id"],
            name="fk_strokes_board_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            f"width BETWEEN {MIN_STROKE_WIDTH} AND {MAX_STROKE_WIDTH}",
            name="ck_strokes_width_bounds",
        ),
        sa.CheckConstraint("version >= 1", name="ck_strokes_version_positive"),
    )
    op.create_index("ix_strokes_board_id", "strokes", ["board_id"])
    # The only hot read: every stroke on a board, already in paint order.
    op.create_index("ix_strokes_board_z", "strokes", ["board_id", "z_index"])

    # -- 3. shared stacking cursor --------------------------------------------

    op.add_column(
        "boards", sa.Column("z_cursor", sa.BigInteger(), server_default="0", nullable=False)
    )
    # Backfilled from the highest z_index any existing note already holds, so
    # the next "bring to front" lands strictly above every note already on the
    # board instead of colliding with one.
    op.execute(
        sa.text("""
        UPDATE boards
        SET z_cursor = COALESCE(
            (SELECT MAX(z_index) FROM notes WHERE notes.board_id = boards.id), 0
        )
        """)
    )


def downgrade() -> None:
    op.drop_column("boards", "z_cursor")

    op.drop_index("ix_strokes_board_z", table_name="strokes")
    op.drop_index("ix_strokes_board_id", table_name="strokes")
    op.drop_table("strokes")

    op.drop_constraint("ck_notes_tone", "notes", type_="check")
    # Lossy, like every downgrade in this project that removes a value a
    # newer version could produce: `peach` and `sky` did not exist before this
    # migration, so they are folded onto `ink` rather than left to violate the
    # restored constraint.
    op.execute("UPDATE notes SET tone = 'ink' WHERE tone IN ('peach', 'sky')")
    op.create_check_constraint("ck_notes_tone", "notes", "tone IN ('accent', 'paper', 'ink')")
