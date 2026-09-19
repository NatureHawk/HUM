"""Guest session endpoints.

Three calls, and they are the only way an identity is established or changed.
Every other endpoint reads the resulting cookie and never accepts an actor from
the caller.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from app.api.deps import current_actor
from app.core import session
from app.schemas.note import Actor

router = APIRouter(tags=["session"])


class SessionRead(BaseModel):
    actor: Actor


class RenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40)


@router.post("/session", response_model=SessionRead)
async def establish_session(request: Request, response: Response) -> SessionRead:
    """Return the caller's guest identity, creating one if they have none.

    Idempotent on purpose: the frontend calls this on every load and must not
    get a new identity each time, or a returning visitor would lose their name
    and stop being recognised as the author of their own notes.
    """
    token = request.cookies.get(session.COOKIE_NAME)
    actor = session.verify(token) if token else None

    if actor is None:
        actor = session.new_actor()
        response.set_cookie(
            session.COOKIE_NAME,
            session.sign(actor),
            **session.cookie_params(),  # type: ignore[arg-type]
        )
    return SessionRead(actor=actor)


@router.patch("/session", response_model=SessionRead)
async def rename_session(
    payload: RenameRequest, response: Response, request: Request
) -> SessionRead:
    """Change the display name, keeping the same guest id.

    The name lives inside the signed cookie, so this re-issues it. Keeping the
    id means existing notes stay attributed to the same person.
    """
    actor = current_actor(request)
    renamed = Actor(id=actor.id, name=payload.name.strip())
    response.set_cookie(
        session.COOKIE_NAME,
        session.sign(renamed),
        **session.cookie_params(),  # type: ignore[arg-type]
    )
    return SessionRead(actor=renamed)
