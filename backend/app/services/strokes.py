"""Stroke operations: the rules, the locking, and the events.

Same shape as services/notes.py: lock the board, write, bump the revision,
build the event, commit, then publish. A stroke has a narrower lifecycle than a
note - create, restack, delete - so there is no content-update function here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import BoardFullError, NotFoundError, StrokeGoneError
from app.models.board import Board
from app.realtime.publisher import publish_event
from app.repositories import boards as board_repo
from app.repositories import strokes as stroke_repo
from app.schemas.events import (
    MutationResult,
    StrokeCreatedEvent,
    StrokeDeletedEvent,
    StrokeReorderedEvent,
)
from app.schemas.note import Actor
from app.schemas.stroke import StrokeCreate, StrokeDelete, StrokeRead, StrokeReorder


def _now() -> datetime:
    return datetime.now(UTC)


async def _lock_board(session: AsyncSession, board_id: uuid.UUID) -> Board:
    board = await board_repo.lock_for_update(session, board_id)
    if board is None:
        raise NotFoundError(f"board {board_id} does not exist")
    return board


async def create_stroke(
    session: AsyncSession, *, board_id: uuid.UUID, actor: Actor, payload: StrokeCreate
) -> MutationResult:
    board = await _lock_board(session, board_id)

    limit = get_settings().max_strokes_per_board
    if await stroke_repo.count_for_board(session, board_id) >= limit:
        raise BoardFullError(
            f"This board has reached its limit of {limit} strokes.",
            details={"limit": limit},
        )

    # Same shared cursor a note's creation uses - a stroke you just drew lands
    # above every note and every earlier stroke. See
    # repositories/boards.py:bump_z_cursor.
    z_index = await board_repo.bump_z_cursor(session, board)

    stroke = await stroke_repo.insert(
        session,
        board_id=board_id,
        points=payload.points,
        color=payload.color,
        width=payload.width,
        z_index=z_index,
        author_name=actor.name,
    )

    revision = await board_repo.bump_revision(session, board)
    event = StrokeCreatedEvent(
        id=uuid.uuid4(),
        board_id=board_id,
        revision=revision,
        occurred_at=_now(),
        op_id=payload.op_id,
        actor=actor,
        stroke=StrokeRead.model_validate(stroke),
    )
    await session.commit()
    await publish_event(event)
    return MutationResult(event=event, board_revision=revision)


async def reorder_stroke(
    session: AsyncSession, *, stroke_id: uuid.UUID, actor: Actor, payload: StrokeReorder
) -> MutationResult:
    """Raise to the front (shared cursor) or drop to the back (local minimum).

    Selecting a stroke on the canvas calls this with `to="front"`, exactly like
    grabbing a note - see services/notes.py:reorder_note for the shared
    reasoning.
    """
    stroke = await stroke_repo.get(session, stroke_id)
    if stroke is None:
        raise StrokeGoneError(f"stroke {stroke_id} no longer exists")
    board = await _lock_board(session, stroke.board_id)

    fresh = await stroke_repo.reload(session, stroke_id)
    if fresh is None:
        raise StrokeGoneError(f"stroke {stroke_id} was deleted by someone else")

    from_z = fresh.z_index
    if payload.to == "front":
        # Already on top: nothing to do. Same reasoning as
        # services/notes.py:reorder_note - selecting a stroke raises it, so
        # this is called on every selection, and re-raising the top object
        # would cost a write, a revision and a pointless broadcast.
        current_revision = board.revision
        if await board_repo.topmost_object(session, board.id) == (fresh.z_index, fresh.id):
            await session.rollback()
            return MutationResult(event=None, board_revision=current_revision)

        target = await board_repo.bump_z_cursor(session, board)
    else:
        lowest, _ = await stroke_repo.z_bounds(session, board.id)
        target = lowest - 1

    await stroke_repo.apply_z_index(session, stroke=fresh, z_index=target)

    revision = await board_repo.bump_revision(session, board)
    event = StrokeReorderedEvent(
        id=uuid.uuid4(),
        board_id=board.id,
        revision=revision,
        occurred_at=_now(),
        op_id=payload.op_id,
        actor=actor,
        stroke=StrokeRead.model_validate(fresh),
        from_z_index=from_z,
    )
    await session.commit()
    await publish_event(event)
    return MutationResult(event=event, board_revision=revision)


async def delete_stroke(
    session: AsyncSession, *, stroke_id: uuid.UUID, actor: Actor, payload: StrokeDelete
) -> MutationResult:
    stroke = await stroke_repo.get(session, stroke_id)
    if stroke is None:
        raise StrokeGoneError(f"stroke {stroke_id} no longer exists")

    board_id = stroke.board_id
    board = await _lock_board(session, board_id)

    deleted = await stroke_repo.delete_stroke(session, stroke_id)
    if not deleted:
        await session.rollback()
        raise StrokeGoneError(f"stroke {stroke_id} was already deleted")

    revision = await board_repo.bump_revision(session, board)
    event = StrokeDeletedEvent(
        id=uuid.uuid4(),
        board_id=board_id,
        revision=revision,
        occurred_at=_now(),
        op_id=payload.op_id,
        actor=actor,
        stroke_id=stroke_id,
    )
    await session.commit()
    await publish_event(event)
    return MutationResult(event=event, board_revision=revision)


__all__ = ["create_stroke", "delete_stroke", "reorder_stroke"]
