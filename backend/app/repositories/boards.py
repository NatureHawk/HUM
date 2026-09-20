"""Board persistence. SQL only - no rules, no event construction."""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.board import Board
from app.models.note import Note
from app.models.stroke import Stroke


async def create(session: AsyncSession, *, title: str) -> Board:
    board = Board(title=title, revision=0)
    session.add(board)
    await session.flush()
    return board


async def get(session: AsyncSession, board_id: uuid.UUID) -> Board | None:
    return await session.get(Board, board_id)


async def lock_for_update(session: AsyncSession, board_id: uuid.UUID) -> Board | None:
    """Take a row lock on the board for the rest of the transaction.

    This is the serialization point for the whole board. Every mutation goes
    through it, so mutations to one board acquire a total order from the
    database, and `revision` can be incremented without a read-modify-write
    race. Boards do not contend with each other.
    """
    result = await session.execute(select(Board).where(Board.id == board_id).with_for_update())
    return result.scalar_one_or_none()


async def bump_revision(session: AsyncSession, board: Board) -> int:
    """Advance the board's revision. Caller must already hold the row lock."""
    board.revision += 1
    await session.flush()
    return board.revision


async def bump_z_cursor(session: AsyncSession, board: Board) -> int:
    """Advance the board's shared stacking cursor. Caller must hold the row lock.

    Notes and strokes both call this - never their own table's MAX(z_index) -
    for every "bring to front": creating an object, or grabbing an existing
    one. One counter is what makes "the last thing you touched is on top" a
    true statement about the whole canvas: two independent per-table maxima
    would each be locally correct and still disagree about which object is
    actually topmost where they overlap. Monotonic and serialized by the same
    board-row lock as `revision`, so concurrent grabs on different objects
    resolve deterministically and every client converges on the same order.
    """
    board.z_cursor += 1
    await session.flush()
    return board.z_cursor


async def topmost_object(
    session: AsyncSession, board_id: uuid.UUID
) -> tuple[int, uuid.UUID] | None:
    """The `(z_index, id)` of whatever paints last on this board, or None if empty.

    Spans BOTH tables, because notes and strokes share one stacking sequence -
    asking either table alone would happily report a note as topmost while a
    stroke sat above it.

    The pair, rather than just the maximum z, is what makes the answer exact:
    paint order is `(z_index, id)` everywhere in this system (see
    notes.list_for_board), so an object that merely ties the highest z is not
    necessarily the one on top. Comparing the whole tuple means a caller can
    ask "is this specific object the top one?" and get an answer that agrees
    with what a person is actually looking at.

    Two indexed reads, one per table, both served by `ix_*_board_z`.
    """
    note_top = (
        await session.execute(
            select(Note.z_index, Note.id)
            .where(Note.board_id == board_id)
            .order_by(Note.z_index.desc(), Note.id.desc())
            .limit(1)
        )
    ).first()
    stroke_top = (
        await session.execute(
            select(Stroke.z_index, Stroke.id)
            .where(Stroke.board_id == board_id)
            .order_by(Stroke.z_index.desc(), Stroke.id.desc())
            .limit(1)
        )
    ).first()

    candidates = [(row[0], row[1]) for row in (note_top, stroke_top) if row is not None]
    if not candidates:
        return None
    return max(candidates)


async def list_with_counts(session: AsyncSession) -> list[tuple[Board, int]]:
    """All boards, newest first, each with its note count.

    An outer join with GROUP BY rather than a per-board COUNT: one query instead
    of N+1.
    """
    stmt = (
        select(Board, func.count(Note.id))
        .outerjoin(Note, Note.board_id == Board.id)
        .group_by(Board.id)
        .order_by(Board.created_at.desc())
    )
    result = await session.execute(stmt)
    return [(board, count) for board, count in result.all()]


async def delete_board(session: AsyncSession, board_id: uuid.UUID) -> bool:
    """Delete a board. Cards go with it via ON DELETE CASCADE."""
    result = await session.execute(delete(Board).where(Board.id == board_id))
    return bool(cast(CursorResult[Any], result).rowcount)
