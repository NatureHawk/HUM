"""The board WebSocket endpoint, end to end.

Uses `fastapi.testclient.TestClient` rather than the `client` fixture in
conftest.py: only TestClient's `websocket_connect` can drive a real WebSocket
handshake, so these tests are plain synchronous `def`s (TestClient runs the
app in its own thread via an anyio portal).

The endpoint deliberately bypasses FastAPI's `Depends(get_session)` for its
own reads (see the module docstring on app/api/routes/ws.py) and opens
`app.db.session.SessionFactory` directly instead. That means the usual
`dependency_overrides` trick used for REST-only tests is not enough on its
own: it would leave the socket reading a different database than the one the
REST calls in the same test just wrote to. So `sync_client` below overrides
`get_session` for REST *and* monkeypatches the name the socket endpoint reads,
pointing both at the same test-database engine the other test files already
use.

Identity now rides the signed `sb_session` cookie instead of an `actor_id`
query parameter, so `sync_client` also establishes a guest session before
handing itself to a test - exactly what the `client` fixture in conftest.py
does for the async suite, just via TestClient's own (requests/httpx-backed)
cookie jar instead.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from app.main import app as fastapi_app


@pytest.fixture
def second_sync_client(sync_client: TestClient) -> TestClient:
    """A second, genuinely different guest against the same running app.

    A plain second `TestClient(fastapi_app)` would spin up its own anyio
    portal on its own thread, with its own event loop - and the app's engine
    and Redis client are already bound to the loop `sync_client`'s `with`
    block started at startup. Any await on that shared engine or Redis
    connection from a different loop raises "attached to a different loop".
    Reusing `sync_client.portal` keeps every request on the one loop the
    app's resources actually live on; only the cookie jar - and therefore the
    guest identity - differs.
    """
    second = TestClient(fastapi_app)
    second.portal = sync_client.portal  # type: ignore[attr-defined]
    second.post("/api/session")
    return second


def _make_board(client: TestClient, title: str = "Board") -> str:
    response = client.post("/api/boards", json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _connect_url(board_id: str) -> str:
    return f"/ws/boards/{board_id}"


def _read_until(
    ws: WebSocketTestSession,
    matches: Callable[[dict[str, Any]], bool],
    *,
    max_frames: int = 10,
) -> dict[str, Any]:
    """Read frames until one satisfies `matches`, discarding the rest.

    Frames on a board socket are multiplexed - a connection's own presence
    echo, another client's traffic, etc. - so the next frame received is never
    guaranteed to be the one under test. This bounds the search instead of
    assuming position, and fails with a clear message rather than hanging.
    """
    seen = []
    for _ in range(max_frames):
        frame = ws.receive_json()
        seen.append(frame.get("type"))
        if matches(frame):
            return frame
    pytest.fail(f"no frame matched within {max_frames} frames; saw types {seen}")


def test_connect_yields_sync_frame_first_with_actor_and_own_presence(
    sync_client: TestClient,
) -> None:
    """The handshake's whole point (see ws.py's docstring): `sync` is always
    the first frame, and it must already carry this connection's own actor
    and its own entry in the presence list - a client should never have to
    guess whether it is "in" the room it just joined."""
    board_id = _make_board(sync_client)
    actor = sync_client.post("/api/session").json()["actor"]

    with sync_client.websocket_connect(_connect_url(board_id)) as ws:
        frame = ws.receive_json()

        assert frame["type"] == "sync"
        assert frame["revision"] == 0
        assert frame["actor"] == actor
        assert frame["notes"] == []
        connection_id = frame["connection_id"]
        assert any(
            m["connection_id"] == connection_id and m["actor"]["id"] == actor["id"]
            for m in frame["presence"]
        )


def test_connecting_to_a_nonexistent_board_yields_board_not_found(
    sync_client: TestClient,
) -> None:
    """The access check (step 2 of the handshake) runs before anything else
    is allocated, so a bad board id must never reach the snapshot read."""
    missing_board_id = uuid.uuid4()

    with sync_client.websocket_connect(_connect_url(str(missing_board_id))) as ws:
        frame = ws.receive_json()

    assert frame["type"] == "error"
    assert frame["code"] == "board_not_found"


def test_garbage_frame_yields_invalid_event_and_the_socket_stays_open(
    sync_client: TestClient,
) -> None:
    """`ClientEnvelope` accepts exactly three shapes; anything else - even
    something that looks plausible, like a raw `card.created` payload - must
    be rejected without tearing down the connection, since one malformed
    frame from a buggy client should not cost it the whole session."""
    board_id = _make_board(sync_client)

    with sync_client.websocket_connect(_connect_url(board_id)) as ws:
        ws.receive_json()  # sync

        ws.send_json({"type": "note.created", "card": {"id": str(uuid.uuid4())}})
        error = _read_until(ws, lambda f: f["type"] == "error")
        assert error["code"] == "invalid_event"

        # Socket must still be usable - a plain ping still gets a pong.
        ws.send_json({"type": "ping"})
        pong = _read_until(ws, lambda f: f["type"] == "pong")
        assert pong["type"] == "pong"


def test_resync_returns_a_fresh_sync_frame(sync_client: TestClient) -> None:
    board_id = _make_board(sync_client)

    with sync_client.websocket_connect(_connect_url(board_id)) as ws:
        first = ws.receive_json()
        assert first["type"] == "sync"

        ws.send_json({"type": "resync"})
        second = _read_until(ws, lambda f: f["type"] == "sync")

        assert second["connection_id"] == first["connection_id"]
        assert second["revision"] == first["revision"]


def test_rest_mutation_is_observed_on_the_socket_as_note_created(
    sync_client: TestClient,
) -> None:
    """The bridge this whole layer exists for: a REST write, made while the
    socket is open, must reach it as the same envelope the REST response
    returned - same `op_id`, and `revision` exactly one past the sync frame's,
    since nothing else has mutated this board yet."""
    board_id = _make_board(sync_client)

    with sync_client.websocket_connect(_connect_url(board_id)) as ws:
        sync_frame = ws.receive_json()
        assert sync_frame["type"] == "sync"

        op_id = str(uuid.uuid4())
        response = sync_client.post(
            f"/api/boards/{board_id}/notes",
            json={
                "x": 0,
                "y": 0,
                "title": "New note",
                "op_id": op_id,
            },
        )
        assert response.status_code == 201, response.text

        event = _read_until(ws, lambda f: f["type"] == "note.created")
        assert event["op_id"] == op_id
        assert event["revision"] == sync_frame["revision"] + 1


def test_client_cannot_name_a_different_actor(sync_client: TestClient) -> None:
    """No client message has an actor field (see schemas/realtime.py's module
    docstring) - the actor is bound once, at connect time, from the session
    cookie. Smuggling one onto a permitted message must not change whose
    identity later events carry."""
    board_id = _make_board(sync_client)

    with sync_client.websocket_connect(_connect_url(board_id)) as ws:
        sync_frame = ws.receive_json()
        bound_actor = sync_frame["actor"]

        # A ping with a bogus extra field must not break the connection.
        ws.send_json({"type": "ping", "actor": {"id": "someone-else", "name": "Eve"}})
        _read_until(ws, lambda f: f["type"] == "pong")

        # `editing` is the one permitted message that produces an event
        # carrying an actor (via presence). Attempting to inject one must not
        # change whose presence entry gets updated.
        ws.send_json(
            {
                "type": "editing",
                "card_id": None,
                "actor": {"id": "someone-else", "name": "Eve"},
            }
        )
        updated = _read_until(ws, lambda f: f["type"] == "presence.updated")

        assert updated["member"]["actor"] == bound_actor
        assert updated["member"]["actor"]["id"] != "someone-else"


def test_presence_joined_and_left_are_observed_by_the_other_socket(
    sync_client: TestClient,
    second_sync_client: TestClient,
) -> None:
    """A second connection's arrival and departure must be visible to a
    socket that was already on the board - the whole reason presence exists.
    Frames are matched by the joining/leaving connection's own id, since this
    socket also receives an echo of its own `presence.joined` and the two
    must not be confused."""
    board_id = _make_board(sync_client)

    with sync_client.websocket_connect(_connect_url(board_id)) as ws1:
        sync1 = ws1.receive_json()
        assert sync1["type"] == "sync"

        with second_sync_client.websocket_connect(_connect_url(board_id)) as ws2:
            sync2 = ws2.receive_json()
            assert sync2["type"] == "sync"
            bob_connection_id = sync2["connection_id"]
            bob_actor_id = sync2["actor"]["id"]

            joined = _read_until(
                ws1,
                lambda f: (
                    f["type"] == "presence.joined"
                    and f["member"]["connection_id"] == bob_connection_id
                ),
            )
            assert joined["member"]["actor"]["id"] == bob_actor_id

            # Closed explicitly, and read for here, still inside this
            # `with` block: TestClient's own __exit__ sends the close frame
            # and then immediately cancels the server-side task, which can
            # race ahead of the endpoint's `finally` clause (presence.leave +
            # the Redis publish) before it finishes. Closing by hand and
            # waiting for the effect gives that cleanup a real chance to run,
            # the way a browser closing a tab would.
            ws2.close(1000)
            left = _read_until(
                ws1,
                lambda f: (
                    f["type"] == "presence.left"
                    and f["member"]["connection_id"] == bob_connection_id
                ),
            )
            assert left["member"]["actor"]["id"] == bob_actor_id

            # PresenceFrame carries only the one member that changed, not the
            # whole room (see its docstring: a full-list broadcast would let a
            # `left` published slightly later overwrite a newer `joined`), so
            # confirm bob is actually gone via the authoritative source - a
            # fresh sync frame's presence list.
            ws1.send_json({"type": "resync"})
            fresh_sync = _read_until(ws1, lambda f: f["type"] == "sync")
            assert all(m["connection_id"] != bob_connection_id for m in fresh_sync["presence"])


# ---------------------------------------------------------------------------
# Ephemeral frames: cursors and live drags.
#
# These are the spatial canvas's new traffic, and their defining property is
# what they do NOT do. They carry no revision, they never reach Postgres, and
# a client that misses one is no worse off a frame later.
# ---------------------------------------------------------------------------


def test_pointer_frames_are_broadcast_and_carry_no_revision(
    sync_client: TestClient,
    second_sync_client: TestClient,
) -> None:
    board_id = _make_board(sync_client)
    alice_actor = sync_client.post("/api/session").json()["actor"]

    with sync_client.websocket_connect(_connect_url(board_id)) as alice:
        _read_until(alice, lambda f: f["type"] == "sync")

        with second_sync_client.websocket_connect(_connect_url(board_id)) as bob:
            _read_until(bob, lambda f: f["type"] == "sync")

            alice.send_json({"type": "pointer", "x": 1280.5, "y": -640.25})
            frame = _read_until(bob, lambda f: f["type"] == "presence.pointer")

    assert frame["actor"]["name"] == alice_actor["name"]
    assert (frame["x"], frame["y"]) == (1280.5, -640.25)
    # Presence-class traffic must never consume a sequence number, or a moving
    # cursor would make every other client think it had missed a note change.
    assert "revision" not in frame


def test_drag_frames_are_broadcast_without_touching_postgres(
    sync_client: TestClient,
    second_sync_client: TestClient,
) -> None:
    """The reason live dragging is affordable at all.

    A drag produces a position every animation frame. These stream over the
    socket so collaborators can watch, while the database sees exactly one
    write - on release, over REST. Here there is no release, so the board's
    revision must not move at all.
    """
    board_id = _make_board(sync_client)
    alice_actor = sync_client.post("/api/session").json()["actor"]
    note = sync_client.post(
        f"/api/boards/{board_id}/notes",
        json={
            "x": 0,
            "y": 0,
            "title": "Carried",
            "op_id": str(uuid.uuid4()),
        },
    ).json()["event"]["note"]

    before = sync_client.get(f"/api/boards/{board_id}").json()["revision"]

    with sync_client.websocket_connect(_connect_url(board_id)) as alice:
        _read_until(alice, lambda f: f["type"] == "sync")

        with second_sync_client.websocket_connect(_connect_url(board_id)) as bob:
            _read_until(bob, lambda f: f["type"] == "sync")

            for step in range(12):
                alice.send_json(
                    {
                        "type": "drag",
                        "note_id": note["id"],
                        "x": 100 + step * 9,
                        "y": 250,
                        "settled": False,
                    }
                )
            moving = _read_until(bob, lambda f: f["type"] == "presence.drag", max_frames=20)

            alice.send_json(
                {
                    "type": "drag",
                    "note_id": note["id"],
                    "x": 199,
                    "y": 250,
                    "settled": True,
                }
            )
            settled = _read_until(
                bob,
                lambda f: f["type"] == "presence.drag" and f["settled"],
                max_frames=20,
            )

    assert moving["note_id"] == note["id"]
    assert moving["actor"]["name"] == alice_actor["name"]
    assert moving["settled"] is False
    assert "revision" not in moving
    assert settled["settled"] is True

    after = sync_client.get(f"/api/boards/{board_id}").json()["revision"]
    assert after == before, "streaming a drag must not write to the database"

    stored = sync_client.get(f"/api/boards/{board_id}").json()["notes"][0]
    assert (stored["x"], stored["y"]) == (0, 0), "no drag frame may move the stored note"


def test_out_of_range_pointer_is_rejected_and_the_socket_survives(
    sync_client: TestClient,
) -> None:
    """Untrusted input, validated at the edge like everything else."""
    board_id = _make_board(sync_client)

    with sync_client.websocket_connect(_connect_url(board_id)) as ws:
        _read_until(ws, lambda f: f["type"] == "sync")

        ws.send_json({"type": "pointer", "x": 9_999_999, "y": 0})
        error = _read_until(ws, lambda f: f["type"] == "error")
        assert error["code"] == "invalid_event"

        ws.send_json({"type": "ping"})
        assert _read_until(ws, lambda f: f["type"] == "pong")["type"] == "pong"


def test_sync_frame_carries_strokes_alongside_notes(sync_client: TestClient) -> None:
    board_id = _make_board(sync_client)
    stroke = sync_client.post(
        f"/api/boards/{board_id}/strokes",
        json={
            "points": [[0, 0], [10, 10]],
            "op_id": str(uuid.uuid4()),
        },
    ).json()["event"]["stroke"]

    with sync_client.websocket_connect(_connect_url(board_id)) as ws:
        frame = ws.receive_json()
        assert frame["type"] == "sync"
        assert frame["strokes"][0]["id"] == stroke["id"]


def test_draw_frames_are_broadcast_without_touching_postgres(
    sync_client: TestClient,
    second_sync_client: TestClient,
) -> None:
    """The pen's equivalent of the drag test above: a stroke in progress
    streams over the socket so collaborators can watch it being drawn, while
    the database sees nothing until the single REST create on pointer-up."""
    board_id = _make_board(sync_client)
    alice_actor = sync_client.post("/api/session").json()["actor"]
    stroke_id = "local-stroke-1"

    before = sync_client.get(f"/api/boards/{board_id}").json()["revision"]

    with sync_client.websocket_connect(_connect_url(board_id)) as alice:
        _read_until(alice, lambda f: f["type"] == "sync")

        with second_sync_client.websocket_connect(_connect_url(board_id)) as bob:
            _read_until(bob, lambda f: f["type"] == "sync")

            points: list[list[float]] = []
            for step in range(8):
                points.append([step * 9.0, 100.0])
                alice.send_json(
                    {
                        "type": "draw",
                        "stroke_id": stroke_id,
                        "points": points,
                        "color": "#15150f",
                        "width": 4,
                        "settled": False,
                    }
                )
            drawing = _read_until(bob, lambda f: f["type"] == "presence.draw", max_frames=20)

            alice.send_json(
                {
                    "type": "draw",
                    "stroke_id": stroke_id,
                    "points": points,
                    "color": "#15150f",
                    "width": 4,
                    "settled": True,
                }
            )
            settled = _read_until(
                bob,
                lambda f: f["type"] == "presence.draw" and f["settled"],
                max_frames=20,
            )

    assert drawing["stroke_id"] == stroke_id
    assert drawing["actor"]["name"] == alice_actor["name"]
    assert drawing["settled"] is False
    assert "revision" not in drawing
    assert settled["settled"] is True

    after = sync_client.get(f"/api/boards/{board_id}").json()["revision"]
    assert after == before, "streaming a stroke must not write to the database"
    assert sync_client.get(f"/api/boards/{board_id}").json()["strokes"] == []


def test_out_of_range_draw_points_are_rejected_and_the_socket_survives(
    sync_client: TestClient,
) -> None:
    """An ephemeral frame is still untrusted input. It is stamped with this
    connection's actor and rebroadcast to the whole board, so "it is never
    persisted" is not a reason to skip validating it."""
    board_id = _make_board(sync_client)

    with sync_client.websocket_connect(_connect_url(board_id)) as ws:
        _read_until(ws, lambda f: f["type"] == "sync")

        ws.send_json(
            {
                "type": "draw",
                "stroke_id": "local-1",
                "points": [[0, 0], [9_999_999, 0]],
                "color": "#15150f",
                "width": 4,
                "settled": False,
            }
        )
        error = _read_until(ws, lambda f: f["type"] == "error")
        assert error["code"] == "invalid_event"

        ws.send_json({"type": "ping"})
        assert _read_until(ws, lambda f: f["type"] == "pong")["type"] == "pong"


def test_a_client_cannot_forge_the_actor_on_an_ephemeral_frame(
    sync_client: TestClient,
    second_sync_client: TestClient,
) -> None:
    """No client message has an actor field; the server stamps its own from
    the cookie it verified at connect time.

    This matters more for cursors and drags than for anything else, because
    they are the frames that visibly attribute activity to a person.
    """
    board_id = _make_board(sync_client)
    alice_actor = sync_client.post("/api/session").json()["actor"]
    bob_actor = second_sync_client.post("/api/session").json()["actor"]

    with sync_client.websocket_connect(_connect_url(board_id)) as alice:
        _read_until(alice, lambda f: f["type"] == "sync")

        with second_sync_client.websocket_connect(_connect_url(board_id)) as bob:
            _read_until(bob, lambda f: f["type"] == "sync")

            alice.send_json(
                {
                    "type": "pointer",
                    "x": 10,
                    "y": 10,
                    "actor": bob_actor,
                }
            )
            frame = _read_until(bob, lambda f: f["type"] == "presence.pointer")

    assert frame["actor"]["id"] == alice_actor["id"]
    assert frame["actor"]["name"] == alice_actor["name"]
