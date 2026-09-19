"""Note CRUD, the event envelope, and edge validation."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient


async def make_board(client: AsyncClient, title: str = "Board") -> str:
    return (await client.post("/api/boards", json={"title": title})).json()["id"]


async def add_note(client: AsyncClient, board_id: str, **overrides):  # type: ignore[no-untyped-def]
    body = {
        "x": 100,
        "y": 100,
        "title": "A thought",
        "op_id": str(uuid.uuid4()),
        **overrides,
    }
    return await client.post(f"/api/boards/{board_id}/notes", json=body)


async def test_created_event_envelope_is_complete(client: AsyncClient, alice: dict) -> None:
    """The envelope is the REST/WebSocket contract, so its shape is asserted."""
    board_id = await make_board(client)
    op_id = str(uuid.uuid4())
    response = await client.post(
        f"/api/boards/{board_id}/notes",
        json={"x": 320.5, "y": 180.25, "title": "First", "op_id": op_id},
    )
    assert response.status_code == 201
    body = response.json()
    event = body["event"]

    assert event["type"] == "note.created"
    assert event["board_id"] == board_id
    assert event["revision"] == 1 == body["board_revision"]
    # op_id is echoed so a client recognises the broadcast of its own change.
    assert event["op_id"] == op_id
    # The actor comes from the session cookie, not anything the request sent.
    assert event["actor"] == alice
    assert event["id"] and event["occurred_at"]

    note = event["note"]
    assert note["title"] == "First"
    assert (note["x"], note["y"]) == (320.5, 180.25)
    assert note["width"] == 240 and note["height"] == 180
    assert note["version"] == 1
    assert note["tone"] == "accent"
    assert note["author_name"] == alice["name"]
    # The server assigns the stacking position; the client never sends one.
    assert note["z_index"] == 1


async def test_revision_increments_by_exactly_one_per_mutation(client: AsyncClient) -> None:
    """Gaplessness is what lets a client tell 'duplicate' from 'I missed one'."""
    board_id = await make_board(client)
    revisions = []

    created = await add_note(client, board_id)
    revisions.append(created.json()["board_revision"])
    note = created.json()["event"]["note"]
    op = lambda: str(uuid.uuid4())  # noqa: E731

    edited = await client.patch(
        f"/api/notes/{note['id']}",
        json={"title": "edited", "expected_version": 1, "op_id": op()},
    )
    revisions.append(edited.json()["board_revision"])

    moved = await client.post(
        f"/api/notes/{note['id']}/position",
        json={"x": 500, "y": 500, "op_id": op()},
    )
    revisions.append(moved.json()["board_revision"])

    resized = await client.post(
        f"/api/notes/{note['id']}/size",
        json={"width": 300, "height": 240, "op_id": op()},
    )
    revisions.append(resized.json()["board_revision"])

    reordered = await client.post(
        f"/api/notes/{note['id']}/order",
        json={"to": "back", "op_id": op()},
    )
    revisions.append(reordered.json()["board_revision"])

    deleted = await client.request("DELETE", f"/api/notes/{note['id']}", json={"op_id": op()})
    revisions.append(deleted.json()["board_revision"])

    assert revisions == [1, 2, 3, 4, 5, 6]


async def test_every_mutation_emits_its_own_event_type(client: AsyncClient) -> None:
    board_id = await make_board(client)
    note = (await add_note(client, board_id)).json()["event"]["note"]
    op = lambda: str(uuid.uuid4())  # noqa: E731

    moved = await client.post(
        f"/api/notes/{note['id']}/position",
        json={"x": 640, "y": 480, "op_id": op()},
    )
    event = moved.json()["event"]
    assert event["type"] == "note.moved"
    # The event records where it came FROM, which a diff-free payload otherwise
    # loses - useful for a future undo, and for explaining what changed.
    assert (event["from_x"], event["from_y"]) == (100, 100)

    resized = await client.post(
        f"/api/notes/{note['id']}/size",
        json={"width": 360, "height": 280, "op_id": op()},
    )
    assert resized.json()["event"]["type"] == "note.resized"

    reordered = await client.post(
        f"/api/notes/{note['id']}/order",
        json={"to": "back", "op_id": op()},
    )
    assert reordered.json()["event"]["type"] == "note.reordered"
    assert reordered.json()["event"]["from_z_index"] == 1

    deleted = await client.request("DELETE", f"/api/notes/{note['id']}", json={"op_id": op()})
    assert deleted.json()["event"]["type"] == "note.deleted"
    assert deleted.json()["event"]["note_id"] == note["id"]


@pytest.mark.parametrize(
    "payload",
    [
        {"x": 200_000, "y": 0},  # beyond the world bound
        {"x": 0, "y": -200_000},
        {"x": 0, "y": 0, "width": 10},  # below the minimum extent
        {"x": 0, "y": 0, "height": 5000},  # above the maximum
        {"x": 0, "y": 0, "rotation": 90},  # beyond the rotation limit
        {"x": 0, "y": 0, "tone": "chartreuse"},  # not a known tone
    ],
)
async def test_invalid_note_input_is_rejected_at_the_edge(
    client: AsyncClient, payload: dict
) -> None:
    board_id = await make_board(client)
    response = await client.post(
        f"/api/boards/{board_id}/notes",
        json={**payload, "op_id": str(uuid.uuid4())},
    )
    assert response.status_code == 422


async def test_patch_requires_something_to_change(client: AsyncClient) -> None:
    board_id = await make_board(client)
    note = (await add_note(client, board_id)).json()["event"]["note"]
    response = await client.patch(
        f"/api/notes/{note['id']}",
        json={"expected_version": 1, "op_id": str(uuid.uuid4())},
    )
    assert response.status_code == 422


async def test_reorder_only_accepts_front_or_back(client: AsyncClient) -> None:
    board_id = await make_board(client)
    note = (await add_note(client, board_id)).json()["event"]["note"]
    response = await client.post(
        f"/api/notes/{note['id']}/order",
        json={"to": "sideways", "op_id": str(uuid.uuid4())},
    )
    assert response.status_code == 422


async def test_cannot_add_a_note_to_a_missing_board(client: AsyncClient) -> None:
    response = await add_note(client, str(uuid.uuid4()))
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_deleting_a_board_cascades_to_its_notes(client: AsyncClient) -> None:
    board_id = await make_board(client, "Doomed")
    note = (await add_note(client, board_id)).json()["event"]["note"]

    assert (await client.delete(f"/api/boards/{board_id}")).status_code == 204

    orphan = await client.post(
        f"/api/notes/{note['id']}/position",
        json={"x": 1, "y": 1, "op_id": str(uuid.uuid4())},
    )
    assert orphan.status_code == 404
    assert orphan.json()["code"] == "note_gone"


async def test_board_list_reports_note_counts(client: AsyncClient) -> None:
    await client.post("/api/boards", json={"title": "Empty"})
    busy = await make_board(client, "Busy")
    for i in range(3):
        await add_note(client, busy, x=i * 100, y=0)

    listing = {b["title"]: b for b in (await client.get("/api/boards")).json()}
    assert listing["Empty"]["note_count"] == 0
    assert listing["Busy"]["note_count"] == 3
    assert listing["Busy"]["revision"] == 3
