"""Guest sessions: the property the whole redesign exists for.

Milestones 1-3 let a request body say who it was. That made two things
possible that should never be possible: attributing a note to somebody else,
and driving somebody else's cursor. This file is the direct test of the fix -
a signed, HTTP-only cookie is now the *only* source of identity - covering
just the session endpoints themselves and the one invariant they exist to
protect (see app/core/session.py's module docstring).

`test_notes_api.py` and friends already exercise ordinary authenticated
mutations via the `client`/`second_client` fixtures; nothing here repeats
that.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from starlette.websockets import WebSocketDisconnect

from app.core.config import get_settings
from app.core.session import COOKIE_NAME
from app.main import app as fastapi_app


@pytest_asyncio.fixture
async def bare_client(_db_override: None) -> AsyncIterator[AsyncClient]:  # type: ignore[no-untyped-def]
    """An AsyncClient that has NOT called `POST /api/session` yet.

    The `client` fixture in conftest.py establishes a session before handing
    itself to a test, which is exactly what these tests need to not have
    happened yet - they are testing the handshake itself.
    """
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _tamper(client: AsyncClient) -> None:
    """Flip one character of the session cookie already in the jar.

    Mutating the `Cookie` object in place, rather than `client.cookies.set(...)`
    again, matters: `.set()` on a fresh name/value pair can add a SECOND cookie
    with a different default domain instead of replacing the first, and a
    client that then sends both leaves the server free to read the original,
    untampered one - silently passing a test that should fail.
    """
    for cookie in client.cookies.jar:
        if cookie.name == COOKIE_NAME:
            assert cookie.value is not None
            last = cookie.value[-1]
            cookie.value = cookie.value[:-1] + ("a" if last != "a" else "b")
            return
    pytest.fail("no session cookie to tamper with")


# ---------------------------------------------------------------------------
# The handshake
# ---------------------------------------------------------------------------


async def test_post_session_establishes_a_guest_and_sets_the_cookie(
    bare_client: AsyncClient,
) -> None:
    response = await bare_client.post("/api/session")
    assert response.status_code == 200
    actor = response.json()["actor"]
    assert actor["id"] and actor["name"]
    # The whole point: identity now lives in a cookie, not in anything the
    # test read back from the response body alone.
    assert bare_client.cookies.get(COOKIE_NAME) is not None


async def test_post_session_is_idempotent_for_a_returning_visitor(
    bare_client: AsyncClient,
) -> None:
    """A second call must return the SAME actor, not mint a new guest.

    If this were not true, a frontend that calls `POST /api/session` on every
    page load - the documented, expected usage - would rename and re-id every
    visitor on every visit, and nobody would ever be recognised as the author
    of their own notes.
    """
    first = (await bare_client.post("/api/session")).json()["actor"]
    second = (await bare_client.post("/api/session")).json()["actor"]
    assert second == first


async def test_patch_session_renames_but_keeps_the_id(bare_client: AsyncClient) -> None:
    original = (await bare_client.post("/api/session")).json()["actor"]

    renamed = (
        await bare_client.patch("/api/session", json={"name": "Captain Sticky Note"})
    ).json()["actor"]

    assert renamed["id"] == original["id"]
    assert renamed["name"] == "Captain Sticky Note"
    assert renamed["name"] != original["name"]


# ---------------------------------------------------------------------------
# No identity, no mutation
# ---------------------------------------------------------------------------


async def test_a_mutation_with_no_cookie_is_401_no_session(bare_client: AsyncClient) -> None:
    """Creating a board takes no actor, so it is the one write a session-less
    caller can still make - use it to reach a note mutation without ever
    calling `POST /api/session`."""
    board_id = (await bare_client.post("/api/boards", json={"title": "orphaned"})).json()["id"]

    response = await bare_client.post(
        f"/api/boards/{board_id}/notes",
        json={"x": 0, "y": 0, "op_id": str(uuid.uuid4())},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "no_session"


async def test_a_forged_or_tampered_cookie_is_rejected(bare_client: AsyncClient) -> None:
    """The property the whole change exists for: a cookie the server did not
    sign - or signed and someone then edited - must not authenticate anybody.

    `session.verify()` collapses every failure mode (missing, expired,
    corrupt, forged) to the same "no session" outcome on purpose (see its
    docstring), so this is indistinguishable from having no cookie at all.
    """
    await bare_client.post("/api/session")
    _tamper(bare_client)

    board_id = (await bare_client.post("/api/boards", json={"title": "tampered"})).json()["id"]
    response = await bare_client.post(
        f"/api/boards/{board_id}/notes",
        json={"x": 0, "y": 0, "op_id": str(uuid.uuid4())},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "no_session"


def test_websocket_with_no_cookie_is_refused(sync_client: TestClient) -> None:
    """The socket enforces the same rule as REST, before it accepts anything.

    `board_socket` checks `socket_actor()` before `websocket.accept()` (see
    ws.py), so a missing cookie closes the handshake with code 4401 - CLOSE_
    UNAUTHORIZED - and never reaches the board-existence check or Postgres at
    all. With `starlette.testclient.TestClient`, an ASGI app that answers
    `websocket.close` before ever accepting causes the client-side connect
    itself to raise `WebSocketDisconnect` (there is no server-error path
    here: the close is deliberate, not a crash), rather than yielding a
    session whose first frame is an error - so that is what this asserts.
    A real browser sees this as the socket's `onclose` firing with
    `event.code === 4401`.
    """
    # Reuse the shared client rather than building a bare TestClient: a fresh
    # one runs the app's lifespan on its own event loop, and that lifespan
    # disposes the module-level async engine - which by then belongs to the
    # loop the async tests ran on. The result is "Event loop is closed" on
    # teardown, and only when the whole suite runs, which is a miserable thing
    # to debug. The fixture already has a live app; just drop its cookie.
    sync_client.cookies.clear()

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        sync_client.websocket_connect(f"/ws/boards/{uuid.uuid4()}"),
    ):
        pass  # pragma: no cover - never reached; the connect itself raises

    assert excinfo.value.code == 4401


# ---------------------------------------------------------------------------
# Impersonation
# ---------------------------------------------------------------------------


async def test_a_note_cannot_be_authored_as_somebody_the_caller_names(
    client: AsyncClient, alice: dict
) -> None:
    """No request body carries an actor any more (see schemas/note.py), so an
    `"actor"` key some old client still sends is just an unknown field the
    schema ignores - not a name that reaches the database. This is the
    concrete proof: claim to be "Eve" and still get authored as yourself.
    """
    board_id = (await client.post("/api/boards", json={"title": "board"})).json()["id"]

    response = await client.post(
        f"/api/boards/{board_id}/notes",
        json={
            "x": 0,
            "y": 0,
            "title": "hello",
            "op_id": str(uuid.uuid4()),
            # A plausible-looking claim the server must never honour.
            "actor": {"id": "u-eve", "name": "Eve"},
        },
    )
    assert response.status_code == 201, response.text
    note = response.json()["event"]["note"]

    assert note["author_name"] == alice["name"]
    assert note["author_name"] != "Eve"


# ---------------------------------------------------------------------------
# The board ceiling
# ---------------------------------------------------------------------------


async def test_creating_past_the_board_ceiling_is_409_board_full(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A public demo cannot let one board grow without bound.

    `get_settings()` is `lru_cache`d to one Settings instance per process, so
    the limit is lowered by mutating that instance directly rather than
    creating 500 real notes to reach the default ceiling.
    """
    monkeypatch.setattr(get_settings(), "max_notes_per_board", 2)

    board_id = (await client.post("/api/boards", json={"title": "tiny"})).json()["id"]
    for i in range(2):
        response = await client.post(
            f"/api/boards/{board_id}/notes",
            json={"x": i, "y": i, "op_id": str(uuid.uuid4())},
        )
        assert response.status_code == 201, response.text

    full = await client.post(
        f"/api/boards/{board_id}/notes",
        json={"x": 0, "y": 0, "op_id": str(uuid.uuid4())},
    )
    assert full.status_code == 409
    assert full.json()["code"] == "board_full"
