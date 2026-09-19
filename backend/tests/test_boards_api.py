"""Board lifecycle and snapshot semantics."""

from __future__ import annotations

import uuid

from httpx import AsyncClient


async def test_create_and_fetch_board(client: AsyncClient) -> None:
    response = await client.post("/api/boards", json={"title": "Launch plan"})
    assert response.status_code == 201
    board = response.json()
    assert board["title"] == "Launch plan"
    # A fresh board has seen no mutations.
    assert board["revision"] == 0

    snapshot = await client.get(f"/api/boards/{board['id']}")
    assert snapshot.status_code == 200
    body = snapshot.json()
    assert body["notes"] == []
    assert body["revision"] == 0
    assert body["board"]["id"] == board["id"]


async def test_board_title_is_validated(client: AsyncClient) -> None:
    assert (await client.post("/api/boards", json={"title": ""})).status_code == 422
    assert (await client.post("/api/boards", json={"title": "x" * 121})).status_code == 422


async def test_missing_board_is_404_with_a_code(client: AsyncClient) -> None:
    response = await client.get(f"/api/boards/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_list_includes_note_counts(client: AsyncClient) -> None:
    await client.post("/api/boards", json={"title": "Empty"})
    busy = (await client.post("/api/boards", json={"title": "Busy"})).json()
    for i in range(3):
        await client.post(
            f"/api/boards/{busy['id']}/notes",
            json={
                "x": 0,
                "y": 0,
                "title": f"Note {i}",
                "op_id": str(uuid.uuid4()),
            },
        )

    listing = {b["title"]: b for b in (await client.get("/api/boards")).json()}
    assert listing["Empty"]["note_count"] == 0
    assert listing["Busy"]["note_count"] == 3
    assert listing["Busy"]["revision"] == 3


async def test_deleting_a_board_cascades_to_its_notes(client: AsyncClient) -> None:
    board = (await client.post("/api/boards", json={"title": "Doomed"})).json()
    created = await client.post(
        f"/api/boards/{board['id']}/notes",
        json={
            "x": 0,
            "y": 0,
            "title": "Goes with it",
            "op_id": str(uuid.uuid4()),
        },
    )
    card_id = created.json()["event"]["note"]["id"]

    assert (await client.delete(f"/api/boards/{board['id']}")).status_code == 204
    assert (await client.get(f"/api/boards/{board['id']}")).status_code == 404

    # The card is gone too, via ON DELETE CASCADE rather than application code.
    orphan = await client.patch(
        f"/api/notes/{card_id}",
        json={
            "title": "still here?",
            "expected_version": 1,
            "op_id": str(uuid.uuid4()),
        },
    )
    assert orphan.status_code == 404
    assert orphan.json()["code"] == "note_gone"


async def test_snapshot_revision_matches_the_notes_beside_it(client: AsyncClient) -> None:
    """The property that makes a snapshot safe to recover from."""
    board = (await client.post("/api/boards", json={"title": "Atomic"})).json()
    last_revision = 0
    for i in range(5):
        result = await client.post(
            f"/api/boards/{board['id']}/notes",
            json={
                "x": 0,
                "y": 0,
                "title": f"Note {i}",
                "op_id": str(uuid.uuid4()),
            },
        )
        last_revision = result.json()["board_revision"]

    snapshot = (await client.get(f"/api/boards/{board['id']}")).json()
    assert snapshot["revision"] == last_revision == 5
    assert len(snapshot["notes"]) == 5


async def test_health_probes_both_dependencies(client: AsyncClient) -> None:
    body = (await client.get("/api/health")).json()
    assert body["status"] == "ok"
    assert body["postgres"]["ok"] is True
    assert body["redis"]["ok"] is True
