"""Note persistence. SQL only - no rules, no event construction.

Every mutating function here writes exactly the fields its operation owns.
There is deliberately no `update_note(note, **everything)`: a function that
takes a whole note is a function through which a stale title can ride in on the
back of a move. See docs/canvas.md section 2.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.note import Note, NoteTone


async def get(session: AsyncSession, note_id: uuid.UUID) -> Note | None:
    return await session.get(Note, note_id)


async def reload(session: AsyncSession, note_id: uuid.UUID) -> Note | None:
    """Re-read from the database, ignoring the identity map.

    Plain `session.get()` returns the already-loaded instance without issuing a
    query, which would silently defeat the "re-read after acquiring the lock"
    step in the service layer.
    """
    return await session.get(Note, note_id, populate_existing=True)


async def list_for_board(session: AsyncSession, board_id: uuid.UUID) -> list[Note]:
    """Every note on a board, in paint order.

    Ordered by `(z_index, id)`. The `id` tiebreak makes the order *total*:
    z-indexes are not unique by construction, and without a deterministic
    tiebreak two clients could paint an overlap in opposite orders.
    """
    stmt = (
        select(Note)
        .where(Note.board_id == board_id)
        .order_by(Note.z_index, Note.id)
        .execution_options(populate_existing=True)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def count_for_board(session: AsyncSession, board_id: uuid.UUID) -> int:
    """How many notes a board holds. Used for the public-demo ceiling."""
    stmt = select(func.count(Note.id)).where(Note.board_id == board_id)
    return int((await session.execute(stmt)).scalar_one())


async def z_bounds(session: AsyncSession, board_id: uuid.UUID) -> tuple[int, int]:
    """Current (lowest, highest) z on the board, or (0, 0) when it is empty."""
    stmt = select(func.min(Note.z_index), func.max(Note.z_index)).where(Note.board_id == board_id)
    low, high = (await session.execute(stmt)).one()
    return (low or 0, high or 0)


async def insert(
    session: AsyncSession,
    *,
    board_id: uuid.UUID,
    title: str,
    body: str,
    x: float,
    y: float,
    width: float,
    height: float,
    rotation: float,
    z_index: int,
    tone: NoteTone,
    author_name: str | None,
) -> Note:
    note = Note(
        board_id=board_id,
        title=title,
        body=body,
        x=x,
        y=y,
        width=width,
        height=height,
        rotation=rotation,
        z_index=z_index,
        tone=tone,
        version=1,
        author_name=author_name,
    )
    session.add(note)
    await session.flush()
    return note


async def update_content_if_version(
    session: AsyncSession,
    *,
    note_id: uuid.UUID,
    expected_version: int,
    title: str | None,
    body: str | None,
    tone: NoteTone | None,
) -> bool:
    """Compare-and-swap on `version`, touching only content fields.

    The version check is part of the UPDATE's WHERE clause rather than a
    separate SELECT, so there is no window in which another transaction can
    commit between the read and the write.

    This is the ONLY function that increments `version`: position, size and
    stacking are not part of what it describes.
    """
    values: dict[str, object] = {"version": Note.version + 1}
    if title is not None:
        values["title"] = title
    if body is not None:
        values["body"] = body
    if tone is not None:
        values["tone"] = tone

    result = await session.execute(
        update(Note).where(Note.id == note_id, Note.version == expected_version).values(**values)
    )
    return bool(cast(CursorResult[Any], result).rowcount)


async def apply_move(
    session: AsyncSession, *, note: Note, x: float, y: float, rotation: float | None
) -> None:
    """Write position only. Deliberately does not touch `version`."""
    note.x = x
    note.y = y
    if rotation is not None:
        note.rotation = rotation
    await session.flush()


async def apply_resize(
    session: AsyncSession,
    *,
    note: Note,
    width: float,
    height: float,
    x: float | None,
    y: float | None,
) -> None:
    """Write extent, and the origin when resizing from a top or left handle."""
    note.width = width
    note.height = height
    if x is not None:
        note.x = x
    if y is not None:
        note.y = y
    await session.flush()


async def apply_z_index(session: AsyncSession, *, note: Note, z_index: int) -> None:
    note.z_index = z_index
    await session.flush()


async def delete_note(session: AsyncSession, note_id: uuid.UUID) -> bool:
    result = await session.execute(delete(Note).where(Note.id == note_id))
    return bool(cast(CursorResult[Any], result).rowcount)
