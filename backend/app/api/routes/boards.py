"""Board and note HTTP endpoints.

Routes do three things and nothing else: parse, delegate, return. Every rule,
every lock and every event lives in the service layer, so there is no way to
reach the database while skipping the rules.

The note operations are deliberately separate endpoints rather than one generic
patch. Moving, resizing, restacking and editing text are different operations
with different arguments, different failure modes and - crucially - different
concurrency rules. Only the text edit is guarded by `version`. See
docs/canvas.md section 2.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor
from app.db.session import get_session
from app.schemas.board import BoardCreate, BoardRead, BoardSnapshot, BoardSummary
from app.schemas.events import MutationResult
from app.schemas.note import (
    NoteContentUpdate,
    NoteCreate,
    NoteDelete,
    NoteMove,
    NoteReorder,
    NoteResize,
)
from app.services import boards as board_service
from app.services import notes as note_service

# One alias instead of repeating Depends() in every signature. Also keeps
# the call out of a default argument, which is evaluated once at import.
SessionDep = Annotated[AsyncSession, Depends(get_session)]

router = APIRouter(tags=["boards"])


@router.post("/boards", response_model=BoardRead, status_code=status.HTTP_201_CREATED)
async def create_board(payload: BoardCreate, session: SessionDep) -> BoardRead:
    return await board_service.create_board(session, title=payload.title)


@router.get("/boards", response_model=list[BoardSummary])
async def list_boards(session: SessionDep) -> list[BoardSummary]:
    return await board_service.list_boards(session)


@router.get("/boards/{board_id}", response_model=BoardSnapshot)
async def get_board(board_id: uuid.UUID, session: SessionDep) -> BoardSnapshot:
    """Full snapshot. Used for first load and for recovery from desync only -
    never as a response to an event."""
    return await board_service.get_snapshot(session, board_id)


@router.delete("/boards/{board_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_board(board_id: uuid.UUID, session: SessionDep) -> None:
    await board_service.delete_board(session, board_id)


@router.post(
    "/boards/{board_id}/notes",
    response_model=MutationResult,
    status_code=status.HTTP_201_CREATED,
)
async def create_note(
    board_id: uuid.UUID, payload: NoteCreate, session: SessionDep, actor: CurrentActor
) -> MutationResult:
    """Place a note at a world position. The server assigns the z-index."""
    return await note_service.create_note(session, board_id=board_id, actor=actor, payload=payload)


@router.patch("/notes/{note_id}", response_model=MutationResult)
async def update_note(
    note_id: uuid.UUID, payload: NoteContentUpdate, session: SessionDep, actor: CurrentActor
) -> MutationResult:
    """Edit text or tone.

    The only note operation that requires `expected_version`, and the only one
    that increments it - so a collaborator dragging this note past you does not
    invalidate the edit you are part-way through typing.
    """
    return await note_service.update_note(session, note_id=note_id, actor=actor, payload=payload)


@router.post("/notes/{note_id}/position", response_model=MutationResult)
async def move_note(
    note_id: uuid.UUID, payload: NoteMove, session: SessionDep, actor: CurrentActor
) -> MutationResult:
    """Commit a note's position - once per completed drag, not once per frame.

    Intermediate positions travel over the WebSocket as ephemeral `drag`
    frames and are never written down. See docs/canvas.md section 3.
    """
    return await note_service.move_note(session, note_id=note_id, actor=actor, payload=payload)


@router.post("/notes/{note_id}/size", response_model=MutationResult)
async def resize_note(
    note_id: uuid.UUID, payload: NoteResize, session: SessionDep, actor: CurrentActor
) -> MutationResult:
    """Resize, and move the origin too when dragging a top or left handle."""
    return await note_service.resize_note(session, note_id=note_id, actor=actor, payload=payload)


@router.post("/notes/{note_id}/order", response_model=MutationResult)
async def reorder_note(
    note_id: uuid.UUID, payload: NoteReorder, session: SessionDep, actor: CurrentActor
) -> MutationResult:
    """Raise to the front or drop to the back.

    The body names an end, not a number: the server resolves it against the
    board's current stack, because the client's view of it is already stale.
    """
    return await note_service.reorder_note(session, note_id=note_id, actor=actor, payload=payload)


@router.delete("/notes/{note_id}", response_model=MutationResult)
async def delete_note(
    note_id: uuid.UUID, payload: NoteDelete, session: SessionDep, actor: CurrentActor
) -> MutationResult:
    """Delete a note.

    DELETE carries a body because the actor and op_id are needed to build the
    event, exactly as for every other mutation. That is legal HTTP and works in
    browsers, though some intermediaries strip bodies from DELETE; if that ever
    bites, this becomes POST /notes/{id}/delete with no change below the route.
    """
    return await note_service.delete_note(session, note_id=note_id, actor=actor, payload=payload)
