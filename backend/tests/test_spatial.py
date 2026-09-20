"""The spatial model: stacking, geometry, and paint order.

These are the invariants the canvas renders against. Get z-order wrong and two
people looking at the same overlap see different notes on top, which is exactly
the silent divergence the whole project exists to avoid.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient


async def make_board(client: AsyncClient) -> str:
    return (await client.post("/api/boards", json={"title": "Canvas"})).json()["id"]


async def add(client: AsyncClient, board_id: str, x: float, y: float, title: str):  # type: ignore[no-untyped-def]
    response = await client.post(
        f"/api/boards/{board_id}/notes",
        json={"x": x, "y": y, "title": title, "op_id": str(uuid.uuid4())},
    )
    assert response.status_code == 201, response.text
    return response.json()["event"]["note"]


async def snapshot_notes(client: AsyncClient, board_id: str) -> list[dict]:
    return (await client.get(f"/api/boards/{board_id}")).json()["notes"]


async def test_a_new_note_lands_on_top(client: AsyncClient) -> None:
    """The thing you just made should be the thing you can see."""
    board_id = await make_board(client)
    first = await add(client, board_id, 0, 0, "first")
    second = await add(client, board_id, 20, 20, "second")
    third = await add(client, board_id, 40, 40, "third")

    assert first["z_index"] < second["z_index"] < third["z_index"]


async def test_notes_may_overlap_at_identical_coordinates(client: AsyncClient) -> None:
    """Overlap is a feature of a canvas, not an error to prevent."""
    board_id = await make_board(client)
    a = await add(client, board_id, 500, 500, "under")
    b = await add(client, board_id, 500, 500, "over")

    assert (a["x"], a["y"]) == (b["x"], b["y"])
    assert b["z_index"] > a["z_index"]


async def test_bring_to_front_and_send_to_back(client: AsyncClient) -> None:
    """The client names an END; the server resolves it against the live stack."""
    board_id = await make_board(client)
    bottom = await add(client, board_id, 0, 0, "bottom")
    await add(client, board_id, 10, 10, "middle")
    top = await add(client, board_id, 20, 20, "top")

    raised = await client.post(
        f"/api/notes/{bottom['id']}/order",
        json={"to": "front", "op_id": str(uuid.uuid4())},
    )
    assert raised.json()["event"]["note"]["z_index"] > top["z_index"]

    dropped = await client.post(
        f"/api/notes/{top['id']}/order",
        json={"to": "back", "op_id": str(uuid.uuid4())},
    )
    lowest = min(n["z_index"] for n in await snapshot_notes(client, board_id))
    assert dropped.json()["event"]["note"]["z_index"] == lowest


async def test_snapshot_comes_back_in_paint_order(client: AsyncClient) -> None:
    """Ordered by (z_index, id), so every client paints the stack identically."""
    board_id = await make_board(client)
    notes = [await add(client, board_id, i * 30, i * 30, f"n{i}") for i in range(6)]

    # Shuffle the stack around a bit.
    for note in (notes[0], notes[4], notes[2]):
        await client.post(
            f"/api/notes/{note['id']}/order",
            json={"to": "front", "op_id": str(uuid.uuid4())},
        )
    await client.post(
        f"/api/notes/{notes[5]['id']}/order",
        json={"to": "back", "op_id": str(uuid.uuid4())},
    )

    stack = await snapshot_notes(client, board_id)
    zs = [n["z_index"] for n in stack]
    assert zs == sorted(zs)
    assert len(set(zs)) == len(zs), "stacking positions collided"
    # Last raised is on top; last lowered is on the bottom.
    assert stack[-1]["title"] == "n2"
    assert stack[0]["title"] == "n5"


async def test_resize_can_move_the_origin_atomically(client: AsyncClient) -> None:
    """Dragging a top-left handle changes size AND position.

    Sending them as one operation keeps that atomic, instead of letting the
    note flicker through an intermediate shape on everybody else's screen.
    """
    board_id = await make_board(client)
    note = await add(client, board_id, 400, 400, "resize me")

    resized = await client.post(
        f"/api/notes/{note['id']}/size",
        json={
            "width": 340,
            "height": 260,
            "x": 300,
            "y": 320,
            "op_id": str(uuid.uuid4()),
        },
    )
    assert resized.status_code == 200
    updated = resized.json()["event"]["note"]
    assert (updated["width"], updated["height"]) == (340, 260)
    assert (updated["x"], updated["y"]) == (300, 320)


async def test_a_move_records_where_it_came_from(client: AsyncClient) -> None:
    board_id = await make_board(client)
    note = await add(client, board_id, 250, 175, "travelling")

    event = (
        await client.post(
            f"/api/notes/{note['id']}/position",
            json={"x": -820.5, "y": 1440.75, "op_id": str(uuid.uuid4())},
        )
    ).json()["event"]

    assert (event["from_x"], event["from_y"]) == (250, 175)
    assert (event["note"]["x"], event["note"]["y"]) == (-820.5, 1440.75)


async def test_negative_world_coordinates_are_valid(client: AsyncClient) -> None:
    """The world has no origin corner: (0, 0) is a point, not a boundary."""
    board_id = await make_board(client)
    note = await add(client, board_id, -5000.25, -3200.5, "north-west")
    assert (note["x"], note["y"]) == (-5000.25, -3200.5)

    stored = (await snapshot_notes(client, board_id))[0]
    assert (stored["x"], stored["y"]) == (-5000.25, -3200.5)


async def test_tone_round_trips(client: AsyncClient) -> None:
    board_id = await make_board(client)
    for tone in ("accent", "paper", "peach", "sky"):
        response = await client.post(
            f"/api/boards/{board_id}/notes",
            json={
                "x": 0,
                "y": 0,
                "title": tone,
                "tone": tone,
                "op_id": str(uuid.uuid4()),
            },
        )
        assert response.json()["event"]["note"]["tone"] == tone
