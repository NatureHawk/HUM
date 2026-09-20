"""Stroke persistence. SQL only - no rules, no event construction.

Mirrors repositories/notes.py: a stroke is created and deleted, and can be
raised or dropped in the stack, but it is never edited in place, so there is no
`update_content`-shaped function here.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.stroke import Stroke


async def get(session: AsyncSession, stroke_id: uuid.UUID) -> Stroke | None:
    return await session.get(Stroke, stroke_id)


async def reload(session: AsyncSession, stroke_id: uuid.UUID) -> Stroke | None:
    """Re-read from the database, ignoring the identity map. See notes.reload."""
    return await session.get(Stroke, stroke_id, populate_existing=True)


async def list_for_board(session: AsyncSession, board_id: uuid.UUID) -> list[Stroke]:
    """Every stroke on a board, in paint order - see notes.list_for_board."""
    stmt = (
        select(Stroke)
        .where(Stroke.board_id == board_id)
        .order_by(Stroke.z_index, Stroke.id)
        .execution_options(populate_existing=True)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def count_for_board(session: AsyncSession, board_id: uuid.UUID) -> int:
    """How many strokes a board holds. Used for the public-demo ceiling."""
    stmt = select(func.count(Stroke.id)).where(Stroke.board_id == board_id)
    return int((await session.execute(stmt)).scalar_one())


async def z_bounds(session: AsyncSession, board_id: uuid.UUID) -> tuple[int, int]:
    """Current (lowest, highest) z among this board's STROKES, or (0, 0).

    Only used for "send to back" - "bring to front" instead reads the shared
    `Board.z_cursor` (see repositories/boards.py) so it stays correct across
    notes and strokes together.
    """
    stmt = select(func.min(Stroke.z_index), func.max(Stroke.z_index)).where(
        Stroke.board_id == board_id
    )
    low, high = (await session.execute(stmt)).one()
    return (low or 0, high or 0)


async def insert(
    session: AsyncSession,
    *,
    board_id: uuid.UUID,
    points: list[list[float]],
    color: str,
    width: float,
    z_index: int,
    author_name: str | None,
) -> Stroke:
    stroke = Stroke(
        board_id=board_id,
        points=points,
        color=color,
        width=width,
        z_index=z_index,
        version=1,
        author_name=author_name,
    )
    session.add(stroke)
    await session.flush()
    return stroke


async def apply_z_index(session: AsyncSession, *, stroke: Stroke, z_index: int) -> None:
    stroke.z_index = z_index
    await session.flush()


async def delete_stroke(session: AsyncSession, stroke_id: uuid.UUID) -> bool:
    result = await session.execute(delete(Stroke).where(Stroke.id == stroke_id))
    return bool(cast(CursorResult[Any], result).rowcount)
