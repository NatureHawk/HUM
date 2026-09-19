"""Request-scoped dependencies.

`CurrentActor` is the single place identity enters the system. Nothing below
this line ever reads an actor from a request body or a query string, which is
what makes impersonation impossible rather than merely discouraged.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request, WebSocket

from app.core import session
from app.core.errors import UnauthorizedError
from app.schemas.note import Actor


def current_actor(request: Request) -> Actor:
    """The guest identity from the signed cookie.

    Raises rather than minting a new guest: a mutation arriving without a
    session means the client skipped the handshake or its cookie expired, and
    silently inventing an identity would attribute someone's work to a stranger.
    The frontend answers 401 by calling `POST /api/session` and retrying.
    """
    token = request.cookies.get(session.COOKIE_NAME)
    actor = session.verify(token) if token else None
    if actor is None:
        raise UnauthorizedError("No guest session. Call POST /api/session first.")
    return actor


def socket_actor(websocket: WebSocket) -> Actor | None:
    """The guest identity for a WebSocket, or None to refuse the connection.

    Cookies ride along with the WebSocket handshake, which is the whole reason
    the identity can live in one: the socket needs no query parameters and the
    client has nothing to forge.
    """
    token = websocket.cookies.get(session.COOKIE_NAME)
    return session.verify(token) if token else None


CurrentActor = Annotated[Actor, Depends(current_actor)]
