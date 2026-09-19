/**
 * The board reducer — the only place durable board state changes.
 *
 * `applyEvent` (exposed here as the `eventReceived` action) is the single
 * function that advances state from a `BoardEvent`. That event arrives either
 * as the direct response to an HTTP mutation or over the WebSocket, and this
 * reducer cannot tell the difference. There is deliberately no second code
 * path, and Milestone 3 changed only which events exist — not how they are
 * applied.
 *
 * What is NOT in here, on purpose:
 *   - the camera, which is per-browser viewport state (see lib/canvas/camera.ts)
 *   - presence, cursors and in-flight drag positions, which are ephemeral and
 *     carry no revision
 *
 * See docs/architecture.md sections 8-9 and docs/canvas.md section 4.
 */

import type { Board, BoardEvent, Note } from "@/lib/types";

export type BoardStatus = "loading" | "ready" | "desynced" | "error";

/**
 * What to restore a note to if its optimistic op is rolled back.
 * `null` means the note did not exist locally before the optimistic change
 * (i.e. it was an optimistic creation, so rollback deletes it).
 */
export type NoteSnapshot = Record<string, Note | null>;

export interface PendingOp {
  opId: string;
  snapshot: NoteSnapshot;
  /**
   * The placeholder id an optimistic creation rendered under, if any. The real
   * id is only known once the server answers, so the authoritative event
   * arrives under a different id and the placeholder has to be retired.
   */
  tempNoteId?: string;
}

export interface BoardState {
  board: Board | null;
  notes: Record<string, Note>;
  revision: number;
  /** Bounded ring buffer of recently applied event ids, for dedupe. */
  seenEventIds: string[];
  pending: Record<string, PendingOp>;
  status: BoardStatus;
  error: string | null;
}

export const SEEN_EVENT_CAP = 200;

export const initialBoardState: BoardState = {
  board: null,
  notes: {},
  revision: 0,
  seenEventIds: [],
  pending: {},
  status: "loading",
  error: null,
};

export type OptimisticChange =
  | { kind: "upsert"; note: Note }
  | { kind: "delete"; noteId: string };

export type BoardAction =
  | { type: "loadStarted" }
  | { type: "snapshotLoaded"; board: Board; notes: Note[]; revision: number }
  | { type: "loadFailed"; message: string }
  | { type: "optimisticApply"; opId: string; change: OptimisticChange; tempNoteId?: string }
  | { type: "opSettled"; opId: string; tempNoteId?: string }
  | { type: "opFailed"; opId: string; message: string }
  | { type: "eventReceived"; event: BoardEvent }
  | { type: "noteOverride"; note: Note }
  | { type: "errorCleared" };

function normalize(notes: Note[]): Record<string, Note> {
  const out: Record<string, Note> = {};
  for (const note of notes) {
    out[note.id] = note;
  }
  return out;
}

function pushSeen(seen: string[], eventId: string): string[] {
  const next = [...seen, eventId];
  if (next.length > SEEN_EVENT_CAP) {
    return next.slice(next.length - SEEN_EVENT_CAP);
  }
  return next;
}

function applyEventToNotes(
  notes: Record<string, Note>,
  event: BoardEvent,
): Record<string, Note> {
  switch (event.type) {
    // Every one of these carries the WHOLE note, so all five are one upsert.
    // Whole-note payloads are what keep application idempotent, which is what
    // makes replay-after-resync safe. They stay correct even though each
    // operation writes only its own columns, because the payload is assembled
    // from committed server state after the write.
    case "note.created":
    case "note.updated":
    case "note.moved":
    case "note.resized":
    case "note.reordered": {
      return { ...notes, [event.note.id]: event.note };
    }
    case "note.deleted": {
      if (!(event.note_id in notes)) return notes;
      const next = { ...notes };
      delete next[event.note_id];
      return next;
    }
    default: {
      // Exhaustiveness guard — if a new event type is ever added to the
      // discriminated union without updating this switch, fail loudly rather
      // than silently ignoring it.
      const _exhaustive: never = event;
      return _exhaustive;
    }
  }
}

export function boardReducer(state: BoardState, action: BoardAction): BoardState {
  switch (action.type) {
    case "loadStarted": {
      return { ...state, status: "loading", error: null };
    }

    case "snapshotLoaded": {
      // Snapshots arrive from two places - the REST fetch on mount or resync,
      // and the WebSocket `sync` frame - so they can race. A slower request
      // started earlier must never overwrite newer state with older, which
      // would silently move the board backwards. Revisions are monotonic per
      // board, so comparing them settles it.
      if (state.board !== null && action.revision < state.revision) {
        return { ...state, status: "ready", error: null };
      }

      return {
        ...state,
        board: action.board,
        notes: normalize(action.notes),
        revision: action.revision,
        seenEventIds: [],
        pending: {},
        status: "ready",
        error: null,
      };
    }

    case "loadFailed": {
      return { ...state, status: "error", error: action.message };
    }

    case "optimisticApply": {
      const { change, opId, tempNoteId } = action;
      const snapshot: NoteSnapshot = {};

      if (change.kind === "upsert") {
        snapshot[change.note.id] = state.notes[change.note.id] ?? null;
        return {
          ...state,
          notes: { ...state.notes, [change.note.id]: change.note },
          pending: { ...state.pending, [opId]: { opId, snapshot, tempNoteId } },
        };
      }

      snapshot[change.noteId] = state.notes[change.noteId] ?? null;
      const nextNotes = { ...state.notes };
      delete nextNotes[change.noteId];
      return {
        ...state,
        notes: nextNotes,
        pending: { ...state.pending, [opId]: { opId, snapshot, tempNoteId } },
      };
    }

    case "opSettled": {
      if (!(action.opId in state.pending)) return state;
      const pending = { ...state.pending };
      delete pending[action.opId];

      // Creations render under a temporary client-side id, since the real one
      // is not known until the server answers. The authoritative note has
      // already been upserted under its real id by the `eventReceived` this
      // settle follows, so the temp entry is now a stale duplicate.
      if (action.tempNoteId && action.tempNoteId in state.notes) {
        const notes = { ...state.notes };
        delete notes[action.tempNoteId];
        return { ...state, notes, pending };
      }

      return { ...state, pending };
    }

    case "opFailed": {
      const op = state.pending[action.opId];
      if (!op) {
        return { ...state, error: action.message };
      }
      const notes = { ...state.notes };
      for (const [noteId, priorNote] of Object.entries(op.snapshot)) {
        if (priorNote === null) {
          delete notes[noteId];
        } else {
          notes[noteId] = priorNote;
        }
      }
      const pending = { ...state.pending };
      delete pending[action.opId];
      return { ...state, notes, pending, error: action.message };
    }

    case "eventReceived": {
      const { event } = action;

      // Duplicate by id: already applied, ignore.
      if (state.seenEventIds.includes(event.id)) {
        return state;
      }

      // Duplicate/stale by revision: describes a state we have moved past.
      if (event.revision <= state.revision) {
        return state;
      }

      // Gap: we provably missed at least one event. Mark desynced and do NOT
      // apply — the caller refetches the canonical snapshot when it sees this.
      if (event.revision > state.revision + 1) {
        return { ...state, status: "desynced" };
      }

      let notes = applyEventToNotes(state.notes, event);
      let pending = state.pending;

      // op_id reconciliation.
      //
      // A client can receive the broadcast of its OWN mutation before the HTTP
      // response that caused it. When that happens the authoritative note is
      // already in place under its real id, so the optimistic placeholder must
      // be retired here rather than waiting for `opSettled` — otherwise the
      // note appears twice for the length of the round trip.
      const settled = event.op_id !== null ? state.pending[event.op_id] : undefined;
      if (settled !== undefined) {
        if (settled.tempNoteId !== undefined && settled.tempNoteId in notes) {
          notes = { ...notes };
          delete notes[settled.tempNoteId];
        }
        pending = { ...pending };
        delete pending[settled.opId];
      }

      return {
        ...state,
        notes,
        pending,
        revision: event.revision,
        seenEventIds: pushSeen(state.seenEventIds, event.id),
        status: "ready",
      };
    }

    case "noteOverride": {
      // Used only for `version_conflict` responses: the server has told us the
      // canonical current note, but that arrives with no board revision
      // attached (unlike a real event), so it cannot flow through the
      // gap-checked `eventReceived` path without corrupting the revision
      // counter. Overwrite the note directly and leave revision/seen untouched.
      return { ...state, notes: { ...state.notes, [action.note.id]: action.note } };
    }

    case "errorCleared": {
      return { ...state, error: null };
    }

    default: {
      const _exhaustive: never = action;
      return _exhaustive;
    }
  }
}
