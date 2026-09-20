"""Board operations."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.repositories import boards as board_repo
from app.repositories import notes as note_repo
from app.repositories import strokes as stroke_repo
from app.schemas.board import BoardRead, BoardSnapshot, BoardSummary
from app.schemas.note import NoteRead
from app.schemas.stroke import StrokeRead


async def create_board(session: AsyncSession, *, title: str) -> BoardRead:
    board = await board_repo.create(session, title=title)
    await session.commit()
    return BoardRead.model_validate(board)


async def list_boards(session: AsyncSession) -> list[BoardSummary]:
    rows = await board_repo.list_with_counts(session)
    return [
        BoardSummary(**BoardRead.model_validate(board).model_dump(), note_count=count)
        for board, count in rows
    ]


async def ensure_exists(session: AsyncSession, board_id: uuid.UUID) -> None:
    """Access gate for the WebSocket handshake.

    Separate from `get_snapshot` because the gate runs before a connection is
    registered and should not pay for reading every card. This is also the one
    place to add a permission check when real authentication arrives - the
    socket endpoint calls it, so no connection can bypass it.
    """
    if await board_repo.get(session, board_id) is None:
        raise NotFoundError(f"board {board_id} does not exist")


async def get_snapshot(session: AsyncSession, board_id: uuid.UUID) -> BoardSnapshot:
    """The board's complete durable state at one revision.

    The board row and its notes are read in the same transaction, so the
    returned `revision` describes exactly the note list beside it. That
    atomicity is the whole point: this is the state a desynced client rebuilds
    from, and a snapshot stitched together from two separate reads could hand it
    a board that never actually existed.
    """
    board = await board_repo.get(session, board_id)
    if board is None:
        raise NotFoundError(f"board {board_id} does not exist")

    notes = await note_repo.list_for_board(session, board_id)
    strokes = await stroke_repo.list_for_board(session, board_id)
    return BoardSnapshot(
        board=BoardRead.model_validate(board),
        notes=[NoteRead.model_validate(n) for n in notes],
        strokes=[StrokeRead.model_validate(s) for s in strokes],
        revision=board.revision,
    )


async def delete_board(session: AsyncSession, board_id: uuid.UUID) -> None:
    deleted = await board_repo.delete_board(session, board_id)
    if not deleted:
        raise NotFoundError(f"board {board_id} does not exist")
    await session.commit()
