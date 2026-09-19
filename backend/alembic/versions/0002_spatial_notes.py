"""cards become spatial notes

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-19

The board stops being three ordered lists and becomes a canvas. That makes
`column_key` and `rank` meaningless, so they are removed rather than left as
invisible required fields.

Nothing is destroyed. Every existing card keeps its id, text, author and
timestamps, and is given a deterministic position derived from the Kanban slot
it already occupied - so a migrated board opens looking like the board you left
(three loose clusters) and is immediately draggable out of that shape. The
layout is a starting position, not a structure.

The order of operations matters: the new columns are added nullable, backfilled
from `column_key`/`rank` while those still exist, and only then made NOT NULL
and the old columns dropped.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kept in step with app/models/note.py.
DEFAULT_WIDTH = 240.0
DEFAULT_HEIGHT = 180.0
MIN_SIZE = 120.0
MAX_SIZE = 1200.0
WORLD_LIMIT = 100_000.0

# Layout constants for the backfill: wide enough that the three old columns
# read as separate clusters, tight enough to fit a laptop viewport at a
# comfortable zoom.
COLUMN_GAP = 430
ROW_GAP = 232
ORIGIN = 96


def upgrade() -> None:
    op.rename_table("cards", "notes")

    # Indexes and constraints keep the old table's name after a rename.
    op.execute("ALTER INDEX pk_cards RENAME TO pk_notes")
    op.execute("ALTER INDEX ix_cards_board_id RENAME TO ix_notes_board_id")
    op.drop_index("ix_cards_board_column_rank", table_name="notes")
    op.execute("ALTER TABLE notes RENAME CONSTRAINT fk_cards_board_id TO fk_notes_board_id")
    op.execute(
        "ALTER TABLE notes RENAME CONSTRAINT ck_cards_version_positive TO ck_notes_version_positive"
    )
    op.drop_constraint("ck_cards_column_key", "notes", type_="check")
    # The title limit grows AND loses its lower bound: a sticky note is created
    # empty and typed into, so requiring a character up front is a Kanban
    # assumption that does not survive the canvas.
    op.drop_constraint("ck_cards_title_length", "notes", type_="check")

    op.add_column("notes", sa.Column("x", sa.Float(), nullable=True))
    op.add_column("notes", sa.Column("y", sa.Float(), nullable=True))
    op.add_column("notes", sa.Column("z_index", sa.Integer(), nullable=True))
    op.add_column(
        "notes",
        sa.Column("width", sa.Float(), nullable=False, server_default=str(DEFAULT_WIDTH)),
    )
    op.add_column(
        "notes",
        sa.Column("height", sa.Float(), nullable=False, server_default=str(DEFAULT_HEIGHT)),
    )
    op.add_column("notes", sa.Column("rotation", sa.Float(), nullable=False, server_default="0"))
    op.add_column(
        "notes",
        sa.Column("tone", sa.String(length=16), nullable=False, server_default="accent"),
    )

    # Backfill. One pass, window functions rather than a Python loop, so a board
    # with thousands of cards is still a single statement.
    #
    #   x  <- which of the three columns it was in
    #   y  <- its position within that column, by the fractional rank
    #   z  <- a global order within the board, so nothing starts co-planar
    op.execute(
        sa.text(f"""
        UPDATE notes AS n
        SET x = {ORIGIN} + placed.column_index * {COLUMN_GAP},
            y = {ORIGIN} + placed.row_index * {ROW_GAP},
            z_index = placed.stack_index,
            -- A small resting tilt, derived from the id so it is stable across
            -- re-runs and identical for every client. Notes that all sit
            -- perfectly square read as a table; the point is a pinboard.
            rotation = ROUND(((('x' || substr(md5(n.id::text), 1, 8))::bit(32)::bigint
                              % 240) - 120) / 100.0, 2)
        FROM (
            SELECT
                id,
                CASE column_key
                    WHEN 'todo' THEN 0
                    WHEN 'in_progress' THEN 1
                    ELSE 2
                END AS column_index,
                ROW_NUMBER() OVER (
                    PARTITION BY board_id, column_key ORDER BY rank, id
                ) - 1 AS row_index,
                ROW_NUMBER() OVER (
                    PARTITION BY board_id ORDER BY column_key, rank, id
                ) - 1 AS stack_index
            FROM notes
        ) AS placed
        WHERE n.id = placed.id
        """)
    )

    op.alter_column("notes", "x", nullable=False)
    op.alter_column("notes", "y", nullable=False)
    op.alter_column("notes", "z_index", nullable=False)

    op.drop_column("notes", "column_key")
    op.drop_column("notes", "rank")

    op.create_check_constraint("ck_notes_tone", "notes", "tone IN ('accent', 'paper', 'ink')")
    op.create_check_constraint("ck_notes_title_length", "notes", "length(title) <= 500")
    op.create_check_constraint(
        "ck_notes_size_bounds",
        "notes",
        f"width BETWEEN {MIN_SIZE} AND {MAX_SIZE} AND height BETWEEN {MIN_SIZE} AND {MAX_SIZE}",
    )
    op.create_check_constraint(
        "ck_notes_world_bounds",
        "notes",
        f"x BETWEEN -{WORLD_LIMIT} AND {WORLD_LIMIT} "
        f"AND y BETWEEN -{WORLD_LIMIT} AND {WORLD_LIMIT}",
    )

    # The only hot read: every note on a board, already in paint order.
    op.create_index("ix_notes_board_z", "notes", ["board_id", "z_index"])


def downgrade() -> None:
    """Back to Kanban.

    Positions cannot round-trip - a canvas holds strictly more information than
    three ordered lists - so notes are folded back by their x position and
    re-ranked by y. Content, ids, authors and timestamps survive.
    """
    op.drop_index("ix_notes_board_z", table_name="notes")
    for name in (
        "ck_notes_world_bounds",
        "ck_notes_size_bounds",
        "ck_notes_title_length",
        "ck_notes_tone",
    ):
        op.drop_constraint(name, "notes", type_="check")

    op.add_column("notes", sa.Column("column_key", sa.String(length=20), nullable=True))
    op.add_column("notes", sa.Column("rank", sa.String(length=128, collation="C"), nullable=True))

    op.execute(
        sa.text(f"""
        UPDATE notes AS n
        SET column_key = CASE
                WHEN n.x < {ORIGIN + COLUMN_GAP // 2} THEN 'todo'
                WHEN n.x < {ORIGIN + COLUMN_GAP + COLUMN_GAP // 2} THEN 'in_progress'
                ELSE 'done'
            END,
            -- Base-36 of the row order, left-padded so it sorts lexicographically
            -- under COLLATE "C" the way the fractional index did.
            rank = lpad(to_hex(ordered.row_index::int), 8, '0')
        FROM (
            SELECT id, ROW_NUMBER() OVER (PARTITION BY board_id ORDER BY y, id) AS row_index
            FROM notes
        ) AS ordered
        WHERE n.id = ordered.id
        """)
    )

    op.alter_column("notes", "column_key", nullable=False)
    op.alter_column("notes", "rank", nullable=False)

    for column in ("tone", "rotation", "height", "width", "z_index", "y", "x"):
        op.drop_column("notes", column)

    op.create_check_constraint(
        "ck_cards_column_key", "notes", "column_key IN ('todo', 'in_progress', 'done')"
    )
    op.create_check_constraint("ck_cards_title_length", "notes", "length(title) BETWEEN 1 AND 200")
    op.execute(
        "ALTER TABLE notes RENAME CONSTRAINT ck_notes_version_positive TO ck_cards_version_positive"
    )
    op.execute("ALTER TABLE notes RENAME CONSTRAINT fk_notes_board_id TO fk_cards_board_id")
    op.execute("ALTER INDEX ix_notes_board_id RENAME TO ix_cards_board_id")
    op.execute("ALTER INDEX pk_notes RENAME TO pk_cards")
    op.rename_table("notes", "cards")
    op.create_index("ix_cards_board_column_rank", "cards", ["board_id", "column_key", "rank"])
