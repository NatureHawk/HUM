"""Freehand strokes: the event envelope, persistence, and edge validation.

Mirrors test_notes_api.py's shape - strokes are a smaller object (no content
to edit, just create/restack/delete), so the surface here is smaller too.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient


async def make_board(client: AsyncClient, title: str = "Board") -> str:
    return (await client.post("/api/boards", json={"title": title})).json()["id"]


async def add_stroke(client: AsyncClient, board_id: str, **overrides):  # type: ignore[no-untyped-def]
    body = {
        "points": [[0, 0], [10, 10], [20, 5]],
        "op_id": str(uuid.uuid4()),
        **overrides,
    }
    return await client.post(f"/api/boards/{board_id}/strokes", json=body)


async def test_created_stroke_event_envelope_is_complete(client: AsyncClient, alice: dict) -> None:
    board_id = await make_board(client)
    op_id = str(uuid.uuid4())
    response = await client.post(
        f"/api/boards/{board_id}/strokes",
        json={
            "points": [[0, 0], [50.5, 25.25], [100, 0]],
            "color": "#15150f",
            "width": 6,
            "op_id": op_id,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    event = body["event"]

    assert event["type"] == "stroke.created"
    assert event["board_id"] == board_id
    assert event["revision"] == 1 == body["board_revision"]
    assert event["op_id"] == op_id
    assert event["actor"] == alice

    stroke = event["stroke"]
    assert stroke["points"] == [[0, 0], [50.5, 25.25], [100, 0]]
    assert stroke["color"] == "#15150f"
    assert stroke["width"] == 6
    assert stroke["version"] == 1
    assert stroke["author_name"] == alice["name"]
    # The server assigns the stacking position; the client never sends one.
    assert stroke["z_index"] == 1


async def test_stroke_persists_and_appears_in_the_snapshot(client: AsyncClient) -> None:
    board_id = await make_board(client)
    created = (await add_stroke(client, board_id)).json()["event"]["stroke"]

    snapshot = (await client.get(f"/api/boards/{board_id}")).json()
    assert len(snapshot["strokes"]) == 1
    assert snapshot["strokes"][0]["id"] == created["id"]
    assert snapshot["notes"] == []


async def test_stroke_points_round_trip_in_world_coordinates(client: AsyncClient) -> None:
    """No screen coordinates, no lossy transform - what was sent is what comes
    back, at every subsequent read. That is what lets a stroke stay put on the
    canvas under an arbitrary pan/zoom: the camera never touches this data."""
    board_id = await make_board(client)
    points = [[-4200.5, 1875.25], [-4180.0, 1900.75], [-4100.25, 1850.0]]
    created = (await add_stroke(client, board_id, points=points)).json()["event"]["stroke"]
    assert created["points"] == points

    reloaded = (await client.get(f"/api/boards/{board_id}")).json()["strokes"][0]
    assert reloaded["points"] == points


async def test_deleting_a_stroke(client: AsyncClient) -> None:
    board_id = await make_board(client)
    stroke = (await add_stroke(client, board_id)).json()["event"]["stroke"]

    response = await client.request(
        "DELETE", f"/api/strokes/{stroke['id']}", json={"op_id": str(uuid.uuid4())}
    )
    assert response.status_code == 200
    assert response.json()["event"]["type"] == "stroke.deleted"
    assert response.json()["event"]["stroke_id"] == stroke["id"]

    snapshot = (await client.get(f"/api/boards/{board_id}")).json()
    assert snapshot["strokes"] == []


async def test_deleting_a_stroke_twice_is_reported_as_gone(client: AsyncClient) -> None:
    board_id = await make_board(client)
    stroke = (await add_stroke(client, board_id)).json()["event"]["stroke"]
    op = lambda: str(uuid.uuid4())  # noqa: E731

    await client.request("DELETE", f"/api/strokes/{stroke['id']}", json={"op_id": op()})
    second = await client.request("DELETE", f"/api/strokes/{stroke['id']}", json={"op_id": op()})

    assert second.status_code == 404
    assert second.json()["code"] == "stroke_gone"


async def test_deleting_a_board_cascades_to_its_strokes(client: AsyncClient) -> None:
    board_id = await make_board(client, "Doomed")
    stroke = (await add_stroke(client, board_id)).json()["event"]["stroke"]

    assert (await client.delete(f"/api/boards/{board_id}")).status_code == 204

    orphan = await client.request(
        "DELETE", f"/api/strokes/{stroke['id']}", json={"op_id": str(uuid.uuid4())}
    )
    assert orphan.status_code == 404
    assert orphan.json()["code"] == "stroke_gone"


async def test_stroke_reorder_front_and_back(client: AsyncClient) -> None:
    board_id = await make_board(client)
    bottom = (await add_stroke(client, board_id)).json()["event"]["stroke"]
    top = (await add_stroke(client, board_id)).json()["event"]["stroke"]
    assert top["z_index"] > bottom["z_index"]

    raised = await client.post(
        f"/api/strokes/{bottom['id']}/order",
        json={"to": "front", "op_id": str(uuid.uuid4())},
    )
    assert raised.json()["event"]["stroke"]["z_index"] > top["z_index"]

    dropped = await client.post(
        f"/api/strokes/{top['id']}/order",
        json={"to": "back", "op_id": str(uuid.uuid4())},
    )
    snapshot = (await client.get(f"/api/boards/{board_id}")).json()["strokes"]
    lowest = min(s["z_index"] for s in snapshot)
    assert dropped.json()["event"]["stroke"]["z_index"] == lowest


async def test_notes_and_strokes_share_one_stacking_order(client: AsyncClient) -> None:
    """The whole point of the shared board cursor: a note and a stroke can be
    compared on one z axis, because there is only one stack, not two that
    happen to overlap on screen. See app/repositories/boards.py:bump_z_cursor."""
    board_id = await make_board(client)

    note = (
        await client.post(
            f"/api/boards/{board_id}/notes",
            json={"x": 0, "y": 0, "title": "under", "op_id": str(uuid.uuid4())},
        )
    ).json()["event"]["note"]
    stroke = (await add_stroke(client, board_id)).json()["event"]["stroke"]
    assert stroke["z_index"] > note["z_index"], "the stroke drawn after the note is on top"

    # Grabbing the note (bring to front) must outrank the stroke drawn after it.
    raised = await client.post(
        f"/api/notes/{note['id']}/order",
        json={"to": "front", "op_id": str(uuid.uuid4())},
    )
    assert raised.json()["event"]["note"]["z_index"] > stroke["z_index"]

    # And a freshly created note lands above everything, stroke included.
    second_note = (
        await client.post(
            f"/api/boards/{board_id}/notes",
            json={"x": 10, "y": 10, "title": "newest", "op_id": str(uuid.uuid4())},
        )
    ).json()["event"]["note"]
    assert second_note["z_index"] > raised.json()["event"]["note"]["z_index"]
    assert second_note["z_index"] > stroke["z_index"]


@pytest.mark.parametrize(
    "payload",
    [
        {"points": [[0, 0]]},  # only one point - not a line
        {"points": [[0, 0]] * 5000},  # absurdly many points
        {"points": [[200_000, 0], [0, 0]]},  # a point beyond the world bound
        {"points": [[0, 0], [10, 10]], "color": "not-a-color"},
        {"points": [[0, 0], [10, 10]], "width": 0.1},  # below the minimum
        {"points": [[0, 0], [10, 10]], "width": 100},  # above the maximum
    ],
)
async def test_invalid_stroke_input_is_rejected_at_the_edge(
    client: AsyncClient, payload: dict
) -> None:
    board_id = await make_board(client)
    response = await client.post(
        f"/api/boards/{board_id}/strokes",
        json={**payload, "op_id": str(uuid.uuid4())},
    )
    assert response.status_code == 422


async def test_cannot_add_a_stroke_to_a_missing_board(client: AsyncClient) -> None:
    response = await add_stroke(client, str(uuid.uuid4()))
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_reorder_only_accepts_front_or_back(client: AsyncClient) -> None:
    board_id = await make_board(client)
    stroke = (await add_stroke(client, board_id)).json()["event"]["stroke"]
    response = await client.post(
        f"/api/strokes/{stroke['id']}/order",
        json={"to": "sideways", "op_id": str(uuid.uuid4())},
    )
    assert response.status_code == 422
