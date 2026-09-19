"""Note operations: the rules, the locking, and the events.

Every public function follows the shape established in Milestone 1, unchanged:

    1. Lock the board row (serialises all mutations on this board).
    2. Re-read the note under the lock, against *current* state.
    3. Write only the fields this operation owns.
    4. Increment the board revision.
    5. Build the event.
    6. Commit, then publish.

What Milestone 3 changed is step 3. There is no longer a single "update the
note" path: content, position, size and stacking are separate operations that
write disjoint columns, which is what lets Alice drag a note while Bob edits its
text without either clobbering the other.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import (
    BoardFullError,
    NoteGoneError,
    NotFoundError,
    VersionConflictError,
)
from app.models.board import Board
from app.models.note import DEFAULT_HEIGHT, DEFAULT_WIDTH, Note
from app.realtime.publisher import publish_event
from app.repositories import boards as board_repo
from app.repositories import notes as note_repo
from app.schemas.events import (
    MutationResult,
    NoteCreatedEvent,
    NoteDeletedEvent,
    NoteMovedEvent,
    NoteReorderedEvent,
    NoteResizedEvent,
    NoteUpdatedEvent,
)
from app.schemas.note import (
    Actor,
    NoteContentUpdate,
    NoteCreate,
    NoteDelete,
    NoteMove,
    NoteRead,
    NoteReorder,
    NoteResize,
)


async def _lock_board(session: AsyncSession, board_id: uuid.UUID) -> Board:
    board = await board_repo.lock_for_update(session, board_id)
    if board is None:
        raise NotFoundError(f"board {board_id} does not exist")
    return board


def _now() -> datetime:
    return datetime.now(UTC)


async def _load_under_lock(session: AsyncSession, note_id: uuid.UUID) -> tuple[Board, Note]:
    """Lock the note's board, then re-read the note against current state.

    The note is fetched twice on purpose. The first read only answers "which
    board do I lock?"; the second happens *after* the lock is granted, because
    another transaction may have committed - including a delete - while we were
    waiting for it. A re-get rather than `session.refresh()`, since refresh
    raises on a row that has since disappeared and "deleted" is an outcome.
    """
    note = await note_repo.get(session, note_id)
    if note is None:
        raise NoteGoneError(f"note {note_id} no longer exists")

    board = await _lock_board(session, note.board_id)

    fresh = await note_repo.reload(session, note_id)
    if fresh is None:
        raise NoteGoneError(f"note {note_id} was deleted by someone else")
    return board, fresh


async def create_note(
    session: AsyncSession, *, board_id: uuid.UUID, actor: Actor, payload: NoteCreate
) -> MutationResult:
    board = await _lock_board(session, board_id)

    # Public-demo ceiling. Checked inside the board lock, so concurrent
    # creations cannot race past it, and enforced here rather than at the edge
    # because only the server knows how full the board already is.
    limit = get_settings().max_notes_per_board
    if await note_repo.count_for_board(session, board_id) >= limit:
        raise BoardFullError(
            f"This board has reached its limit of {limit} notes.",
            details={"limit": limit},
        )

    # A new note lands on top of whatever is already there. The client never
    # picks a z-index; the server derives one from the board's current stack.
    _, highest = await note_repo.z_bounds(session, board_id)

    note = await note_repo.insert(
        session,
        board_id=board_id,
        title=payload.title,
        body=payload.body,
        x=payload.x,
        y=payload.y,
        width=payload.width or DEFAULT_WIDTH,
        height=payload.height or DEFAULT_HEIGHT,
        rotation=payload.rotation,
        z_index=highest + 1,
        tone=payload.tone,
        author_name=actor.name,
    )

    revision = await board_repo.bump_revision(session, board)
    event = NoteCreatedEvent(
        id=uuid.uuid4(),
        board_id=board_id,
        revision=revision,
        occurred_at=_now(),
        op_id=payload.op_id,
        actor=actor,
        note=NoteRead.model_validate(note),
    )
    await session.commit()
    await publish_event(event)
    return MutationResult(event=event, board_revision=revision)


async def update_note(
    session: AsyncSession, *, note_id: uuid.UUID, actor: Actor, payload: NoteContentUpdate
) -> MutationResult:
    """Edit text or tone. The only operation guarded by `version`."""
    # The row itself is not needed here - the compare-and-swap below works off
    # `expected_version` - but the lock and the existence check are.
    board, _ = await _load_under_lock(session, note_id)

    changed = await note_repo.update_content_if_version(
        session,
        note_id=note_id,
        expected_version=payload.expected_version,
        title=payload.title,
        body=payload.body,
        tone=payload.tone,
    )
    if not changed:
        # The compare-and-swap failed. Distinguish "deleted" from "someone else
        # got there first": the client's recovery differs.
        current = await note_repo.reload(session, note_id)
        # Serialise before rolling back - a rollback expires every ORM
        # instance, and reading an attribute afterwards would try to lazy-load
        # it on a dead transaction.
        snapshot = (
            NoteRead.model_validate(current).model_dump(mode="json")
            if current is not None
            else None
        )
        await session.rollback()
        if snapshot is None:
            raise NoteGoneError(f"note {note_id} was deleted by someone else")
        raise VersionConflictError(
            "someone else edited this note first",
            details={"note": snapshot},
        )

    fresh = await note_repo.reload(session, note_id)
    assert fresh is not None
    revision = await board_repo.bump_revision(session, board)
    event = NoteUpdatedEvent(
        id=uuid.uuid4(),
        board_id=board.id,
        revision=revision,
        occurred_at=_now(),
        op_id=payload.op_id,
        actor=actor,
        note=NoteRead.model_validate(fresh),
    )
    await session.commit()
    await publish_event(event)
    return MutationResult(event=event, board_revision=revision)


async def move_note(
    session: AsyncSession, *, note_id: uuid.UUID, actor: Actor, payload: NoteMove
) -> MutationResult:
    """Commit a note's position - one write per completed drag, not per frame.

    No version check: this is last-writer-wins by design. Two people dragging
    the same note are fighting over it, and the right outcome is that it rests
    where the last one let go. The board lock makes "last" well defined, so
    every client converges on the same value.
    """
    board, note = await _load_under_lock(session, note_id)
    from_x, from_y = note.x, note.y

    await note_repo.apply_move(
        session,
        note=note,
        x=payload.x,
        y=payload.y,
        rotation=payload.rotation,
    )

    revision = await board_repo.bump_revision(session, board)
    event = NoteMovedEvent(
        id=uuid.uuid4(),
        board_id=board.id,
        revision=revision,
        occurred_at=_now(),
        op_id=payload.op_id,
        actor=actor,
        note=NoteRead.model_validate(note),
        from_x=from_x,
        from_y=from_y,
    )
    await session.commit()
    await publish_event(event)
    return MutationResult(event=event, board_revision=revision)


async def resize_note(
    session: AsyncSession, *, note_id: uuid.UUID, actor: Actor, payload: NoteResize
) -> MutationResult:
    board, note = await _load_under_lock(session, note_id)

    await note_repo.apply_resize(
        session,
        note=note,
        width=payload.width,
        height=payload.height,
        x=payload.x,
        y=payload.y,
    )

    revision = await board_repo.bump_revision(session, board)
    event = NoteResizedEvent(
        id=uuid.uuid4(),
        board_id=board.id,
        revision=revision,
        occurred_at=_now(),
        op_id=payload.op_id,
        actor=actor,
        note=NoteRead.model_validate(note),
    )
    await session.commit()
    await publish_event(event)
    return MutationResult(event=event, board_revision=revision)


async def reorder_note(
    session: AsyncSession, *, note_id: uuid.UUID, actor: Actor, payload: NoteReorder
) -> MutationResult:
    """Raise to the front or drop to the back.

    The client asks for an end, not a number. The server resolves it against
    the board's current stack under the lock - the same reason the Kanban
    version never accepted an array index: the client's view is already stale
    by the time the request lands.
    """
    board, note = await _load_under_lock(session, note_id)
    from_z = note.z_index

    lowest, highest = await note_repo.z_bounds(session, board.id)
    target = highest + 1 if payload.to == "front" else lowest - 1

    await note_repo.apply_z_index(session, note=note, z_index=target)

    revision = await board_repo.bump_revision(session, board)
    event = NoteReorderedEvent(
        id=uuid.uuid4(),
        board_id=board.id,
        revision=revision,
        occurred_at=_now(),
        op_id=payload.op_id,
        actor=actor,
        note=NoteRead.model_validate(note),
        from_z_index=from_z,
    )
    await session.commit()
    await publish_event(event)
    return MutationResult(event=event, board_revision=revision)


async def delete_note(
    session: AsyncSession, *, note_id: uuid.UUID, actor: Actor, payload: NoteDelete
) -> MutationResult:
    note = await note_repo.get(session, note_id)
    if note is None:
        raise NoteGoneError(f"note {note_id} no longer exists")

    board_id = note.board_id
    board = await _lock_board(session, board_id)

    deleted = await note_repo.delete_note(session, note_id)
    if not deleted:
        # Lost the race to another delete. Deleting twice is not an error worth
        # surfacing, but it must not emit a second event or every client's gap
        # detection would fire on a change that never happened.
        await session.rollback()
        raise NoteGoneError(f"note {note_id} was already deleted")

    revision = await board_repo.bump_revision(session, board)
    event = NoteDeletedEvent(
        id=uuid.uuid4(),
        board_id=board_id,
        revision=revision,
        occurred_at=_now(),
        op_id=payload.op_id,
        actor=actor,
        note_id=note_id,
    )
    await session.commit()
    await publish_event(event)
    return MutationResult(event=event, board_revision=revision)


__all__ = [
    "create_note",
    "delete_note",
    "move_note",
    "reorder_note",
    "resize_note",
    "update_note",
]
