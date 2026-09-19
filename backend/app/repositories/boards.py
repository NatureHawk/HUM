"""Board persistence. SQL only - no rules, no event construction."""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.board import Board
from app.models.note import Note


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
