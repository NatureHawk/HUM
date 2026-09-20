"""Concurrency behaviour.

Every rule in docs/canvas.md section 2 has a test here. If one of these
changes, that document is wrong and must be updated with it.

The headline rule, and the reason the canvas redesign needed new concurrency
semantics at all: `version` guards CONTENT ONLY. Moving, resizing and
restacking a note write disjoint columns and do not touch it, so Alice dragging
a note past Bob does not invalidate the sentence Bob is part-way through typing.

"Alice" and "Bob" are now two different `AsyncClient`s (`client` and
`second_client`), each with its own cookie jar and therefore its own
server-issued identity - not two dicts a caller could put words into. See
conftest.py.
"""

from __future__ import annotations

import asyncio
import json as _json
import uuid

from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import get_settings
from app.db.session import get_session
from app.main import app as fastapi_app
from app.realtime.hub import events_channel, init_hub, shutdown_hub


async def make_board(client: AsyncClient) -> str:
    return (await client.post("/api/boards", json={"title": "Board"})).json()["id"]


async def add_note(
    client: AsyncClient,
    board_id: str,
    title: str = "Note",
    x: float = 100,
    y: float = 100,
):  # type: ignore[no-untyped-def]
    response = await client.post(
        f"/api/boards/{board_id}/notes",
        json={"x": x, "y": y, "title": title, "op_id": str(uuid.uuid4())},
    )
    assert response.status_code == 201, response.text
    return response.json()["event"]["note"]


# ---------------------------------------------------------------------------
# Field-aware versioning - the substantive new rule
# ---------------------------------------------------------------------------


async def test_moving_a_note_does_not_bump_its_content_version(client: AsyncClient) -> None:
    """The point of the redesign, in one assertion.

    If a move bumped `version`, every drag would invalidate every open editor
    on that note and produce conflicts for a change that touched nothing the
    editor cares about.
    """
    board_id = await make_board(client)
    note = await add_note(client, board_id)
    assert note["version"] == 1

    moved = await client.post(
        f"/api/notes/{note['id']}/position",
        json={"x": 900, "y": 640, "op_id": str(uuid.uuid4())},
    )
    assert moved.status_code == 200
    assert moved.json()["event"]["note"]["version"] == 1


async def test_resize_and_reorder_do_not_bump_the_content_version(client: AsyncClient) -> None:
    board_id = await make_board(client)
    note = await add_note(client, board_id)

    resized = await client.post(
        f"/api/notes/{note['id']}/size",
        json={"width": 400, "height": 300, "op_id": str(uuid.uuid4())},
    )
    assert resized.json()["event"]["note"]["version"] == 1

    # A second note, so the first one is genuinely NOT on top and the reorder
    # below is a real restack rather than the no-op that raising an
    # already-topmost object now is.
    await add_note(client, board_id, title="on top")

    reordered = await client.post(
        f"/api/notes/{note['id']}/order",
        json={"to": "front", "op_id": str(uuid.uuid4())},
    )
    assert reordered.json()["event"]["note"]["version"] == 1


async def test_a_drag_does_not_invalidate_an_in_progress_text_edit(
    client: AsyncClient, second_client: AsyncClient
) -> None:
    """Alice drags the note while Bob is typing in it. Both must succeed.

    Bob opened his editor when the note was at version 1. By the time he saves,
    Alice has moved and resized it. Under a naive "any change bumps the
    version" rule his edit would be rejected for changes that never touched the
    text.
    """
    board_id = await make_board(client)
    note = await add_note(client, board_id, title="Original")

    await client.post(
        f"/api/notes/{note['id']}/position",
        json={"x": 1500, "y": 900, "op_id": str(uuid.uuid4())},
    )
    await client.post(
        f"/api/notes/{note['id']}/size",
        json={"width": 380, "height": 300, "op_id": str(uuid.uuid4())},
    )

    edit = await second_client.patch(
        f"/api/notes/{note['id']}",
        json={
            "title": "Bob typed this",
            "expected_version": 1,  # the version his editor opened at
            "op_id": str(uuid.uuid4()),
        },
    )
    assert edit.status_code == 200, edit.text

    # And the edit must not have undone Alice's work.
    saved = edit.json()["event"]["note"]
    assert saved["title"] == "Bob typed this"
    assert saved["x"] == 1500
    assert saved["y"] == 900
    assert saved["width"] == 380
    assert saved["version"] == 2


async def test_a_move_writes_only_position(client: AsyncClient, second_client: AsyncClient) -> None:
    """Each operation writes exactly the fields it owns, and no others."""
    board_id = await make_board(client)
    note = await add_note(client, board_id, title="Keep me")
    await client.patch(
        f"/api/notes/{note['id']}",
        json={
            "body": "and me",
            "tone": "sky",
            "expected_version": 1,
            "op_id": str(uuid.uuid4()),
        },
    )

    moved = (
        await second_client.post(
            f"/api/notes/{note['id']}/position",
            json={"x": 42, "y": 84, "op_id": str(uuid.uuid4())},
        )
    ).json()["event"]["note"]

    assert (moved["x"], moved["y"]) == (42, 84)
    assert moved["title"] == "Keep me"
    assert moved["body"] == "and me"
    assert moved["tone"] == "sky"
    assert moved["width"] == note["width"]
    assert moved["z_index"] == note["z_index"]


async def test_a_content_edit_writes_only_content(client: AsyncClient) -> None:
    board_id = await make_board(client)
    note = await add_note(client, board_id)
    await client.post(
        f"/api/notes/{note['id']}/position",
        json={"x": 777, "y": 555, "op_id": str(uuid.uuid4())},
    )

    edited = (
        await client.patch(
            f"/api/notes/{note['id']}",
            json={
                "title": "New text",
                "expected_version": 1,
                "op_id": str(uuid.uuid4()),
            },
        )
    ).json()["event"]["note"]

    assert edited["title"] == "New text"
    assert (edited["x"], edited["y"]) == (777, 555)
    assert edited["z_index"] == note["z_index"]


# ---------------------------------------------------------------------------
# Content conflicts - unchanged rules, new object
# ---------------------------------------------------------------------------


async def test_second_editor_of_the_same_version_is_rejected(
    client: AsyncClient, second_client: AsyncClient
) -> None:
    """First committer wins, and the loser is told."""
    board_id = await make_board(client)
    note = await add_note(client, board_id, title="Contested")

    winner = await client.patch(
        f"/api/notes/{note['id']}",
        json={
            "title": "Alice's text",
            "expected_version": 1,
            "op_id": str(uuid.uuid4()),
        },
    )
    assert winner.status_code == 200
    assert winner.json()["event"]["note"]["version"] == 2

    loser = await second_client.patch(
        f"/api/notes/{note['id']}",
        json={
            "title": "Bob's text",
            "expected_version": 1,
            "op_id": str(uuid.uuid4()),
        },
    )
    assert loser.status_code == 409
    body = loser.json()
    assert body["code"] == "version_conflict"

    # The rejection carries the canonical note, or the client has to guess.
    canonical = body["details"]["note"]
    assert canonical["title"] == "Alice's text"
    assert canonical["version"] == 2

    snapshot = (await client.get(f"/api/boards/{board_id}")).json()
    assert snapshot["notes"][0]["title"] == "Alice's text"
    assert snapshot["revision"] == 2  # the rejected write consumed nothing


async def test_retrying_with_the_canonical_version_succeeds(
    client: AsyncClient, second_client: AsyncClient
) -> None:
    board_id = await make_board(client)
    note = await add_note(client, board_id)

    await client.patch(
        f"/api/notes/{note['id']}",
        json={
            "title": "first",
            "expected_version": 1,
            "op_id": str(uuid.uuid4()),
        },
    )
    conflict = await second_client.patch(
        f"/api/notes/{note['id']}",
        json={
            "title": "second",
            "expected_version": 1,
            "op_id": str(uuid.uuid4()),
        },
    )
    current = conflict.json()["details"]["note"]["version"]

    retry = await second_client.patch(
        f"/api/notes/{note['id']}",
        json={
            "title": "second",
            "expected_version": current,
            "op_id": str(uuid.uuid4()),
        },
    )
    assert retry.status_code == 200
    assert retry.json()["event"]["note"]["version"] == 3


async def test_editing_a_deleted_note_reports_it_as_gone(
    client: AsyncClient, second_client: AsyncClient
) -> None:
    """Delete is terminal: drop the note, do not show a conflict."""
    board_id = await make_board(client)
    note = await add_note(client, board_id)

    await second_client.request(
        "DELETE",
        f"/api/notes/{note['id']}",
        json={"op_id": str(uuid.uuid4())},
    )
    response = await client.patch(
        f"/api/notes/{note['id']}",
        json={
            "title": "ghost",
            "expected_version": 1,
            "op_id": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 404
    assert response.json()["code"] == "note_gone"


async def test_moving_a_deleted_note_reports_it_as_gone(
    client: AsyncClient, second_client: AsyncClient
) -> None:
    board_id = await make_board(client)
    note = await add_note(client, board_id)
    await second_client.request(
        "DELETE",
        f"/api/notes/{note['id']}",
        json={"op_id": str(uuid.uuid4())},
    )
    response = await client.post(
        f"/api/notes/{note['id']}/position",
        json={"x": 10, "y": 10, "op_id": str(uuid.uuid4())},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "note_gone"


async def test_double_delete_does_not_emit_a_second_event(
    client: AsyncClient, second_client: AsyncClient
) -> None:
    """A second delete must not consume a revision, or every client's gap
    detection would fire on a change that never happened."""
    board_id = await make_board(client)
    note = await add_note(client, board_id)

    first = await client.request(
        "DELETE",
        f"/api/notes/{note['id']}",
        json={"op_id": str(uuid.uuid4())},
    )
    assert first.status_code == 200
    revision_after = first.json()["board_revision"]

    second = await second_client.request(
        "DELETE",
        f"/api/notes/{note['id']}",
        json={"op_id": str(uuid.uuid4())},
    )
    assert second.status_code == 404
    assert second.json()["code"] == "note_gone"

    snapshot = (await client.get(f"/api/boards/{board_id}")).json()
    assert snapshot["revision"] == revision_after


# ---------------------------------------------------------------------------
# Position: last-writer-wins, on purpose
# ---------------------------------------------------------------------------


async def test_concurrent_moves_are_last_writer_wins_and_converge(
    client: AsyncClient, second_client: AsyncClient
) -> None:
    """Two people dragging one note are fighting over a physical object.

    The right outcome is that it rests where the last person let go - not an
    error, and not a merge. The board lock makes "last" well defined, so every
    client converges on the same value.
    """
    board_id = await make_board(client)
    note = await add_note(client, board_id)

    first = await client.post(
        f"/api/notes/{note['id']}/position",
        json={"x": 100, "y": 100, "op_id": str(uuid.uuid4())},
    )
    second = await second_client.post(
        f"/api/notes/{note['id']}/position",
        json={"x": 800, "y": 250, "op_id": str(uuid.uuid4())},
    )
    assert first.status_code == 200
    assert second.status_code == 200  # not an error case

    snapshot = (await client.get(f"/api/boards/{board_id}")).json()
    assert (snapshot["notes"][0]["x"], snapshot["notes"][0]["y"]) == (800, 250)


async def test_the_last_object_grabbed_is_the_one_on_top(
    client: AsyncClient, second_client: AsyncClient
) -> None:
    """The physical rule, which is what "grab" now means on the canvas.

    The frontend calls this endpoint on every pointer-down on a note, not only
    from an explicit "bring to front" command, so this is the API-level shape
    of "pick a thing up and it is in front". Alice grabbing note 1, then note
    2, then Bob grabbing note 1 again must leave all three observers agreeing
    that note 1 is on top - and they do, because nobody ever sends a z-index:
    the server resolves every "front" against its own serialized order.
    """
    board_id = await make_board(client)
    one = await add_note(client, board_id, title="one")
    two = await add_note(client, board_id, title="two")

    async def grab(caller: AsyncClient, note: dict) -> dict:
        response = await caller.post(
            f"/api/notes/{note['id']}/order",
            json={"to": "front", "op_id": str(uuid.uuid4())},
        )
        assert response.status_code == 200, response.text
        return response.json()["event"]["note"]

    # Alice grabs the lower note: it comes to the front.
    raised_one = await grab(client, one)
    assert raised_one["z_index"] > two["z_index"]

    # Alice grabs the other one: now that one is in front.
    raised_two = await grab(client, two)
    assert raised_two["z_index"] > raised_one["z_index"]

    # Bob grabs the first note again - and both clients read the same stack,
    # because the stack is board state, not either browser's render order.
    regrabbed_one = await grab(second_client, one)
    assert regrabbed_one["z_index"] > raised_two["z_index"]

    for reader in (client, second_client):
        stack = (await reader.get(f"/api/boards/{board_id}")).json()["notes"]
        assert [n["title"] for n in stack] == ["two", "one"], "both must agree on the order"


async def test_raising_an_already_topmost_object_is_a_no_op(client: AsyncClient) -> None:
    """Grabbing the thing that is already on top must cost nothing.

    Grabbing is what raises, so this endpoint is hit on every pointer-down -
    and the most common grab of all is picking up the note you are already
    looking at. Without this, each one would be a row write, a board revision
    and a broadcast ordering every other client to restack something that
    never moved.

    "Already on top" is judged across BOTH tables, because notes and strokes
    share one stacking sequence: a note at the highest note-z is NOT topmost
    if a stroke sits above it.
    """
    board_id = await make_board(client)
    note = await add_note(client, board_id, title="lower")
    top = await add_note(client, board_id, title="upper")
    settled = (await client.get(f"/api/boards/{board_id}")).json()["revision"]

    # The top note, grabbed repeatedly: success every time, nothing written.
    for _ in range(3):
        response = await client.post(
            f"/api/notes/{top['id']}/order",
            json={"to": "front", "op_id": str(uuid.uuid4())},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["event"] is None, "a no-op must not invent an event"
        assert body["board_revision"] == settled

    after = (await client.get(f"/api/boards/{board_id}")).json()
    assert after["revision"] == settled, "no revision may be consumed"
    assert [n["title"] for n in after["notes"]] == ["lower", "upper"]
    assert [n["z_index"] for n in after["notes"]] == [
        note["z_index"],
        top["z_index"],
    ], "no z_index may change"

    # The one that is NOT on top still raises, exactly as before.
    raised = await client.post(
        f"/api/notes/{note['id']}/order",
        json={"to": "front", "op_id": str(uuid.uuid4())},
    )
    assert raised.json()["event"]["type"] == "note.reordered"
    assert raised.json()["board_revision"] == settled + 1
    assert raised.json()["event"]["note"]["z_index"] > top["z_index"]

    # And having just been raised, IT is now the no-op case.
    again = await client.post(
        f"/api/notes/{note['id']}/order",
        json={"to": "front", "op_id": str(uuid.uuid4())},
    )
    assert again.json()["event"] is None
    assert again.json()["board_revision"] == settled + 1


async def test_a_note_under_a_stroke_is_not_treated_as_topmost(client: AsyncClient) -> None:
    """The cross-table half of the check, which is the half that can silently
    rot: if "topmost" were computed per table, the highest note would look
    topmost while a stroke sat visibly on top of it, and grabbing that note
    would do nothing at all."""
    board_id = await make_board(client)
    note = await add_note(client, board_id, title="under the drawing")
    stroke = (
        await client.post(
            f"/api/boards/{board_id}/strokes",
            json={"points": [[0, 0], [50, 50]], "op_id": str(uuid.uuid4())},
        )
    ).json()["event"]["stroke"]
    assert stroke["z_index"] > note["z_index"]

    # The note IS the highest note. It is not the highest object.
    raised = await client.post(
        f"/api/notes/{note['id']}/order",
        json={"to": "front", "op_id": str(uuid.uuid4())},
    )
    assert raised.json()["event"] is not None, "a note under a stroke must really raise"
    assert raised.json()["event"]["note"]["z_index"] > stroke["z_index"]

    # Now it is topmost across both tables, so grabbing it again is free.
    assert (
        await client.post(
            f"/api/notes/{note['id']}/order",
            json={"to": "front", "op_id": str(uuid.uuid4())},
        )
    ).json()["event"] is None

    # And the stroke, now underneath, still raises for real.
    stroke_raise = await client.post(
        f"/api/strokes/{stroke['id']}/order",
        json={"to": "front", "op_id": str(uuid.uuid4())},
    )
    assert stroke_raise.json()["event"]["type"] == "stroke.reordered"
    assert (
        await client.post(
            f"/api/strokes/{stroke['id']}/order",
            json={"to": "front", "op_id": str(uuid.uuid4())},
        )
    ).json()["event"] is None


async def test_simultaneous_grabs_of_different_objects_converge(
    client: AsyncClient, second_client: AsyncClient
) -> None:
    """Two people grabbing two different objects at the same instant.

    There is no correct answer to "who is on top" here other than "whichever
    the database committed last" - the point is that there IS one answer, the
    same one for everybody, and that the two z-indexes never collide. Both
    objects raise through the one shared board cursor, so a note and a stroke
    compete on a single axis rather than in two stacks that only coincidentally
    agree.
    """
    board_id = await make_board(client)
    note = await add_note(client, board_id, title="note")
    stroke = (
        await client.post(
            f"/api/boards/{board_id}/strokes",
            json={"points": [[0, 0], [10, 10]], "op_id": str(uuid.uuid4())},
        )
    ).json()["event"]["stroke"]

    note_raise, stroke_raise = await asyncio.gather(
        client.post(
            f"/api/notes/{note['id']}/order",
            json={"to": "front", "op_id": str(uuid.uuid4())},
        ),
        second_client.post(
            f"/api/strokes/{stroke['id']}/order",
            json={"to": "front", "op_id": str(uuid.uuid4())},
        ),
    )
    assert note_raise.status_code == 200
    assert stroke_raise.status_code == 200

    snapshot = (await client.get(f"/api/boards/{board_id}")).json()
    final_note_z = snapshot["notes"][0]["z_index"]
    final_stroke_z = snapshot["strokes"][0]["z_index"]

    # Serialized by the board lock: one of them is strictly on top, and the
    # revisions are gapless, so neither raise was lost.
    assert final_note_z != final_stroke_z
    assert snapshot["revision"] == 4  # 2 creations + 2 raises, nothing dropped


# ---------------------------------------------------------------------------
# The board lock
# ---------------------------------------------------------------------------


async def test_parallel_writers_produce_a_gapless_revision_sequence(
    engine,  # type: ignore[no-untyped-def]
    clean_tables,  # type: ignore[no-untyped-def]
) -> None:
    """The real test of the board lock.

    Twenty concurrent note creations on one board, from independent sessions.
    Because every mutation serialises on `SELECT ... FOR UPDATE` of the board
    row, the revisions handed out must be exactly 1..20 with no gaps, no
    duplicates and no lost writes - even though the requests overlap.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def _override():  # type: ignore[no-untyped-def]
        async with factory() as s:
            try:
                yield s
            except Exception:
                await s.rollback()
                raise

    fastapi_app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=fastapi_app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/api/session")
            board_id = await make_board(client)

            async def create(index: int) -> int:
                response = await client.post(
                    f"/api/boards/{board_id}/notes",
                    json={
                        "x": index * 40,
                        "y": index * 25,
                        "title": f"Note {index}",
                        "op_id": str(uuid.uuid4()),
                    },
                )
                assert response.status_code == 201, response.text
                return int(response.json()["board_revision"])

            revisions = await asyncio.gather(*(create(i) for i in range(20)))
            assert sorted(revisions) == list(range(1, 21)), (
                f"revisions were not gapless: {sorted(revisions)}"
            )

            snapshot = (await client.get(f"/api/boards/{board_id}")).json()
            assert snapshot["revision"] == 20
            assert len(snapshot["notes"]) == 20

            # Every note survived and every z is distinct, so the stack is a
            # total order even though the inserts raced.
            zs = [n["z_index"] for n in snapshot["notes"]]
            assert len(set(zs)) == 20, "concurrent creations collided on z_index"
            assert zs == sorted(zs), "snapshot was not returned in stacking order"
    finally:
        fastapi_app.dependency_overrides.clear()


async def test_a_rejected_mutation_publishes_nothing_to_redis(client: AsyncClient) -> None:
    """An event must never describe a change that did not commit.

    The `client` fixture uses httpx's ASGITransport, which never runs FastAPI's
    lifespan, so no hub exists in this suite by default and `publish_event` is
    silently a no-op. This test stands one up against the real Redis.
    """
    board_id = await make_board(client)
    note = await add_note(client, board_id)

    redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    hub_redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    hub = init_hub(hub_redis)
    await hub.start()

    pubsub = redis.pubsub()
    await pubsub.subscribe(events_channel(uuid.UUID(board_id)))
    # redis-py parses exactly one wire message per call, so the first read after
    # subscribing consumes the SUBSCRIBE acknowledgement itself.
    await pubsub.get_message(timeout=1.0)

    try:
        # Control: an accepted edit really does publish.
        accepted = await client.patch(
            f"/api/notes/{note['id']}",
            json={
                "title": "committed",
                "expected_version": 1,
                "op_id": str(uuid.uuid4()),
            },
        )
        assert accepted.status_code == 200

        published = None
        for _ in range(40):
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.25)
            if message and message.get("type") == "message":
                published = _json.loads(message["data"])
                break
        assert published is not None and published["type"] == "note.updated"

        # The real assertion: a rejected edit publishes nothing.
        rejected = await client.patch(
            f"/api/notes/{note['id']}",
            json={
                "title": "stale",
                "expected_version": 1,
                "op_id": str(uuid.uuid4()),
            },
        )
        assert rejected.status_code == 409

        for _ in range(8):
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.25)
            assert message is None or message.get("type") != "message", (
                f"a rejected mutation published {message}"
            )
    finally:
        await pubsub.aclose()
        await redis.aclose()
        await shutdown_hub()
        await hub_redis.aclose()
