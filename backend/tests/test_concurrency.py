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
            "tone": "ink",
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
    assert moved["tone"] == "ink"
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
