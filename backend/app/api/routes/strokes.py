"""Stroke HTTP endpoints. Same shape as the note endpoints in boards.py:
parse, delegate, return - every rule lives in the service layer."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor
from app.db.session import get_session
from app.schemas.events import MutationResult
from app.schemas.stroke import StrokeCreate, StrokeDelete, StrokeReorder
from app.services import strokes as stroke_service

SessionDep = Annotated[AsyncSession, Depends(get_session)]

router = APIRouter(tags=["strokes"])


@router.post(
    "/boards/{board_id}/strokes",
    response_model=MutationResult,
    status_code=status.HTTP_201_CREATED,
)
async def create_stroke(
    board_id: uuid.UUID, payload: StrokeCreate, session: SessionDep, actor: CurrentActor
) -> MutationResult:
    """Commit a finished pen stroke. The in-progress drawing travels over the
    WebSocket as ephemeral `draw` frames and is never written down - this is
    the one durable write per stroke, made on pointer-up."""
    return await stroke_service.create_stroke(
        session, board_id=board_id, actor=actor, payload=payload
    )


@router.post("/strokes/{stroke_id}/order", response_model=MutationResult)
async def reorder_stroke(
    stroke_id: uuid.UUID, payload: StrokeReorder, session: SessionDep, actor: CurrentActor
) -> MutationResult:
    """Raise to the front or drop to the back - the same "grab it and it comes
    to the front" mechanism notes use, sharing the same stacking cursor."""
    return await stroke_service.reorder_stroke(
        session, stroke_id=stroke_id, actor=actor, payload=payload
    )


@router.delete("/strokes/{stroke_id}", response_model=MutationResult)
async def delete_stroke(
    stroke_id: uuid.UUID, payload: StrokeDelete, session: SessionDep, actor: CurrentActor
) -> MutationResult:
    return await stroke_service.delete_stroke(
        session, stroke_id=stroke_id, actor=actor, payload=payload
    )
