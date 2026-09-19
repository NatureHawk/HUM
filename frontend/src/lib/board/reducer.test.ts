import { describe, expect, it } from "vitest";
import { boardReducer, initialBoardState, type BoardState } from "@/lib/board/reducer";
import type { Actor, Board, BoardEvent, Note } from "@/lib/types";

const actor: Actor = { id: "u-1", name: "Test User" };

const board: Board = {
  id: "board-1",
  title: "Test board",
  revision: 0,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

function makeNote(overrides: Partial<Note> = {}): Note {
  return {
    id: "note-1",
    board_id: "board-1",
    title: "First thought",
    body: "",
    x: 100,
    y: 100,
    width: 240,
    height: 180,
    rotation: 0,
    z_index: 1,
    tone: "accent",
    version: 1,
    author_name: "Test User",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function loadedState(notes: Note[] = [makeNote()], revision = 1): BoardState {
  return boardReducer(initialBoardState, {
    type: "snapshotLoaded",
    board,
    notes,
    revision,
  });
}

function event(overrides: Partial<BoardEvent> = {}): BoardEvent {
  return {
    id: "event-1",
    board_id: "board-1",
    revision: 2,
    occurred_at: "2026-01-01T00:01:00Z",
    op_id: null,
    actor,
    type: "note.created",
    note: makeNote({ id: "note-2", title: "Second thought" }),
    ...overrides,
  } as BoardEvent;
}

describe("boardReducer: snapshots", () => {
  it("normalizes notes by id and becomes ready", () => {
    const state = loadedState();
    expect(state.status).toBe("ready");
    expect(state.revision).toBe(1);
    expect(state.notes["note-1"]).toBeDefined();
    expect(state.error).toBeNull();
  });

  it("ignores a snapshot older than the state it already has", () => {
    // Two snapshot sources exist — the REST fetch and the socket's `sync`
    // frame — so a slower request started earlier can land after a newer one.
    // Applying it would move the board backwards.
    const fresh = loadedState([makeNote({ id: "note-new" })], 9);
    const stale = boardReducer(fresh, {
      type: "snapshotLoaded",
      board,
      notes: [makeNote({ id: "note-old" })],
      revision: 4,
    });

    expect(stale.revision).toBe(9);
    expect(stale.notes["note-new"]).toBeDefined();
    expect(stale.notes["note-old"]).toBeUndefined();
    expect(stale.status).toBe("ready");
  });
});

describe("boardReducer: events", () => {
  it("applies every spatial event as the same whole-note upsert", () => {
    // All five carrying events carry the complete note, which is what keeps
    // application idempotent and replay-after-resync safe.
    let state = loadedState();

    state = boardReducer(state, {
      type: "eventReceived",
      event: event({
        id: "e-move",
        revision: 2,
        type: "note.moved",
        note: makeNote({ x: 900, y: 450 }),
        from_x: 100,
        from_y: 100,
      } as Partial<BoardEvent>),
    });
    expect(state.notes["note-1"].x).toBe(900);
    expect(state.notes["note-1"].y).toBe(450);

    state = boardReducer(state, {
      type: "eventReceived",
      event: event({
        id: "e-resize",
        revision: 3,
        type: "note.resized",
        note: makeNote({ x: 900, y: 450, width: 400, height: 320 }),
      } as Partial<BoardEvent>),
    });
    expect(state.notes["note-1"].width).toBe(400);

    state = boardReducer(state, {
      type: "eventReceived",
      event: event({
        id: "e-reorder",
        revision: 4,
        type: "note.reordered",
        note: makeNote({ x: 900, y: 450, width: 400, height: 320, z_index: 12 }),
        from_z_index: 1,
      } as Partial<BoardEvent>),
    });
    expect(state.notes["note-1"].z_index).toBe(12);
    expect(state.revision).toBe(4);
  });

  it("removes a note on note.deleted", () => {
    const state = boardReducer(loadedState(), {
      type: "eventReceived",
      event: event({
        id: "e-del",
        revision: 2,
        type: "note.deleted",
        note_id: "note-1",
      } as Partial<BoardEvent>),
    });
    expect(state.notes["note-1"]).toBeUndefined();
  });

  it("drops a duplicate by event id", () => {
    let state = loadedState();
    const e = event({ id: "dup", revision: 2 });
    state = boardReducer(state, { type: "eventReceived", event: e });
    const afterFirst = state;
    state = boardReducer(state, { type: "eventReceived", event: e });
    expect(state).toBe(afterFirst);
  });

  it("drops an event whose revision it has already passed", () => {
    const state = loadedState([makeNote()], 5);
    const next = boardReducer(state, {
      type: "eventReceived",
      event: event({ id: "old", revision: 4 }),
    });
    expect(next).toBe(state);
  });

  it("marks itself desynced on a revision gap and does not apply the event", () => {
    // A gap means we provably missed something. Guessing is not an option, so
    // the client refetches the canonical snapshot instead.
    const state = loadedState([makeNote()], 1);
    const next = boardReducer(state, {
      type: "eventReceived",
      event: event({ id: "gap", revision: 5 }),
    });
    expect(next.status).toBe("desynced");
    expect(next.revision).toBe(1);
    expect(next.notes["note-2"]).toBeUndefined();
  });
});

describe("boardReducer: optimistic operations", () => {
  it("applies then rolls back a failed change", () => {
    let state = loadedState();
    const original = state.notes["note-1"];

    state = boardReducer(state, {
      type: "optimisticApply",
      opId: "op-1",
      change: { kind: "upsert", note: makeNote({ x: 5000, y: 5000 }) },
    });
    expect(state.notes["note-1"].x).toBe(5000);

    state = boardReducer(state, { type: "opFailed", opId: "op-1", message: "nope" });
    expect(state.notes["note-1"]).toEqual(original);
    expect(state.error).toBe("nope");
    expect(state.pending["op-1"]).toBeUndefined();
  });

  it("rolls an optimistic creation back out of existence", () => {
    let state = loadedState([]);
    state = boardReducer(state, {
      type: "optimisticApply",
      opId: "op-2",
      tempNoteId: "temp-1",
      change: { kind: "upsert", note: makeNote({ id: "temp-1" }) },
    });
    expect(state.notes["temp-1"]).toBeDefined();

    state = boardReducer(state, { type: "opFailed", opId: "op-2", message: "rejected" });
    expect(state.notes["temp-1"]).toBeUndefined();
  });

  it("restores a deleted note when the delete is rejected", () => {
    let state = loadedState();
    state = boardReducer(state, {
      type: "optimisticApply",
      opId: "op-3",
      change: { kind: "delete", noteId: "note-1" },
    });
    expect(state.notes["note-1"]).toBeUndefined();

    state = boardReducer(state, { type: "opFailed", opId: "op-3", message: "gone wrong" });
    expect(state.notes["note-1"]).toBeDefined();
  });

  it("retires the placeholder when the client's own event arrives", () => {
    // The realtime broadcast of your own mutation can beat the HTTP response
    // back. When it does, the authoritative note is already present under its
    // real id, so the temporary one must go immediately — otherwise the note
    // is visibly duplicated for the length of the round trip.
    let state = loadedState([]);
    state = boardReducer(state, {
      type: "optimisticApply",
      opId: "op-4",
      tempNoteId: "temp-9",
      change: { kind: "upsert", note: makeNote({ id: "temp-9" }) },
    });

    state = boardReducer(state, {
      type: "eventReceived",
      event: event({
        id: "own",
        revision: 2,
        op_id: "op-4",
        note: makeNote({ id: "note-real" }),
      }),
    });

    expect(state.notes["temp-9"]).toBeUndefined();
    expect(state.notes["note-real"]).toBeDefined();
    expect(state.pending["op-4"]).toBeUndefined();
  });

  it("leaves another client's event alone", () => {
    let state = loadedState([]);
    state = boardReducer(state, {
      type: "optimisticApply",
      opId: "op-mine",
      tempNoteId: "temp-mine",
      change: { kind: "upsert", note: makeNote({ id: "temp-mine" }) },
    });
    state = boardReducer(state, {
      type: "eventReceived",
      event: event({ id: "theirs", revision: 2, op_id: "op-theirs" }),
    });

    expect(state.pending["op-mine"]).toBeDefined();
    expect(state.notes["temp-mine"]).toBeDefined();
  });

  it("overrides a note with the server's copy on a version conflict", () => {
    // Arrives with no revision attached, so it cannot flow through the
    // gap-checked event path without corrupting the counter.
    const state = loadedState();
    const next = boardReducer(state, {
      type: "noteOverride",
      note: makeNote({ title: "server wins", version: 7 }),
    });
    expect(next.notes["note-1"].title).toBe("server wins");
    expect(next.notes["note-1"].version).toBe(7);
    expect(next.revision).toBe(state.revision);
  });
});
