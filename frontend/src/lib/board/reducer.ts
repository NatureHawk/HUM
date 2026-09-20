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

import type { Board, BoardEvent, Note, Stroke } from "@/lib/types";

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

/** Same shape as NoteSnapshot, for the same reason: rollback needs to know
 * what to put back, and `null` means the stroke did not exist locally before
 * the optimistic change. */
export type StrokeSnapshot = Record<string, Stroke | null>;

export interface StrokePendingOp {
  opId: string;
  snapshot: StrokeSnapshot;
  /** See PendingOp.tempNoteId — same problem, for strokes. */
  tempStrokeId?: string;
}

export interface BoardState {
  board: Board | null;
  notes: Record<string, Note>;
  strokes: Record<string, Stroke>;
  revision: number;
  /** Bounded ring buffer of recently applied event ids, for dedupe. */
  seenEventIds: string[];
  pending: Record<string, PendingOp>;
  /**
   * A separate map from `pending`, not a shared one keyed by kind: notes and
   * strokes never share an op_id, so nothing is gained by merging the maps,
   * and keeping them apart means a stroke rollback can never accidentally
   * touch note state (or vice versa).
   */
  strokePending: Record<string, StrokePendingOp>;
  status: BoardStatus;
  error: string | null;
}

export const SEEN_EVENT_CAP = 200;

export const initialBoardState: BoardState = {
  board: null,
  notes: {},
  strokes: {},
  revision: 0,
  seenEventIds: [],
  pending: {},
  strokePending: {},
  status: "loading",
  error: null,
};

export type OptimisticChange =
  | { kind: "upsert"; note: Note }
  | { kind: "delete"; noteId: string };

export type StrokeChange =
  | { kind: "upsert"; stroke: Stroke }
  | { kind: "delete"; strokeId: string };

export type BoardAction =
  | { type: "loadStarted" }
  | { type: "snapshotLoaded"; board: Board; notes: Note[]; strokes: Stroke[]; revision: number }
  | { type: "loadFailed"; message: string }
  | { type: "optimisticApply"; opId: string; change: OptimisticChange; tempNoteId?: string }
  | { type: "opSettled"; opId: string; tempNoteId?: string }
  | { type: "opFailed"; opId: string; message: string }
  | { type: "opReverted"; opId: string }
  | {
      type: "strokeOptimisticApply";
      opId: string;
      change: StrokeChange;
      tempStrokeId?: string;
    }
  | { type: "strokeOpSettled"; opId: string; tempStrokeId?: string }
  | { type: "strokeOpFailed"; opId: string; message: string }
  | { type: "strokeOpReverted"; opId: string }
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

function normalizeStrokes(strokes: Stroke[]): Record<string, Stroke> {
  const out: Record<string, Stroke> = {};
  for (const stroke of strokes) {
    out[stroke.id] = stroke;
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
    case "stroke.created":
    case "stroke.reordered":
    case "stroke.deleted": {
      return notes;
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

function applyEventToStrokes(
  strokes: Record<string, Stroke>,
  event: BoardEvent,
): Record<string, Stroke> {
  switch (event.type) {
    // Whole-stroke payloads, same reasoning as applyEventToNotes: idempotent
    // application and safe replay after a resync.
    case "stroke.created":
    case "stroke.reordered": {
      return { ...strokes, [event.stroke.id]: event.stroke };
    }
    case "stroke.deleted": {
      if (!(event.stroke_id in strokes)) return strokes;
      const next = { ...strokes };
      delete next[event.stroke_id];
      return next;
    }
    case "note.created":
    case "note.updated":
    case "note.moved":
    case "note.resized":
    case "note.reordered":
    case "note.deleted": {
      return strokes;
    }
    default: {
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
        strokes: normalizeStrokes(action.strokes),
        revision: action.revision,
        seenEventIds: [],
        pending: {},
        strokePending: {},
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

    case "opReverted": {
      // The server succeeded and changed nothing - see MutationResult in
      // lib/types.ts. That is NOT the same as success-with-an-event: no
      // authoritative note is coming, so an optimistic guess left in place
      // here would never be corrected. Restoring the snapshot is what keeps
      // the board honest, and unlike `opFailed` there is nothing to report:
      // the user asked for a state the board was already in.
      const op = state.pending[action.opId];
      if (!op) return state;
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
      return { ...state, notes, pending };
    }

    case "strokeOptimisticApply": {
      const { change, opId, tempStrokeId } = action;
      const snapshot: StrokeSnapshot = {};

      if (change.kind === "upsert") {
        snapshot[change.stroke.id] = state.strokes[change.stroke.id] ?? null;
        return {
          ...state,
          strokes: { ...state.strokes, [change.stroke.id]: change.stroke },
          strokePending: { ...state.strokePending, [opId]: { opId, snapshot, tempStrokeId } },
        };
      }

      snapshot[change.strokeId] = state.strokes[change.strokeId] ?? null;
      const nextStrokes = { ...state.strokes };
      delete nextStrokes[change.strokeId];
      return {
        ...state,
        strokes: nextStrokes,
        strokePending: { ...state.strokePending, [opId]: { opId, snapshot, tempStrokeId } },
      };
    }

    case "strokeOpSettled": {
      if (!(action.opId in state.strokePending)) return state;
      const strokePending = { ...state.strokePending };
      delete strokePending[action.opId];

      // Same reasoning as opSettled: the authoritative stroke has already
      // been upserted under its real id by the eventReceived this settle
      // follows, so the temp entry is now a stale duplicate.
      if (action.tempStrokeId && action.tempStrokeId in state.strokes) {
        const strokes = { ...state.strokes };
        delete strokes[action.tempStrokeId];
        return { ...state, strokes, strokePending };
      }

      return { ...state, strokePending };
    }

    case "strokeOpFailed": {
      const op = state.strokePending[action.opId];
      if (!op) {
        return { ...state, error: action.message };
      }
      const strokes = { ...state.strokes };
      for (const [strokeId, priorStroke] of Object.entries(op.snapshot)) {
        if (priorStroke === null) {
          delete strokes[strokeId];
        } else {
          strokes[strokeId] = priorStroke;
        }
      }
      const strokePending = { ...state.strokePending };
      delete strokePending[action.opId];
      return { ...state, strokes, strokePending, error: action.message };
    }

    case "strokeOpReverted": {
      // The stroke half of `opReverted`, for the same reason.
      const op = state.strokePending[action.opId];
      if (!op) return state;
      const strokes = { ...state.strokes };
      for (const [strokeId, priorStroke] of Object.entries(op.snapshot)) {
        if (priorStroke === null) {
          delete strokes[strokeId];
        } else {
          strokes[strokeId] = priorStroke;
        }
      }
      const strokePending = { ...state.strokePending };
      delete strokePending[action.opId];
      return { ...state, strokes, strokePending };
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
      let strokes = applyEventToStrokes(state.strokes, event);
      let pending = state.pending;
      let strokePending = state.strokePending;

      // op_id reconciliation.
      //
      // A client can receive the broadcast of its OWN mutation before the HTTP
      // response that caused it. When that happens the authoritative note is
      // already in place under its real id, so the optimistic placeholder must
      // be retired here rather than waiting for `opSettled` — otherwise the
      // note appears twice for the length of the round trip. Notes and
      // strokes each mint their own op_ids, so only one of these two maps
      // will ever actually contain a given event's op_id.
      const settled = event.op_id !== null ? state.pending[event.op_id] : undefined;
      if (settled !== undefined) {
        if (settled.tempNoteId !== undefined && settled.tempNoteId in notes) {
          notes = { ...notes };
          delete notes[settled.tempNoteId];
        }
        pending = { ...pending };
        delete pending[settled.opId];
      }

      const strokeSettled =
        event.op_id !== null ? state.strokePending[event.op_id] : undefined;
      if (strokeSettled !== undefined) {
        if (strokeSettled.tempStrokeId !== undefined && strokeSettled.tempStrokeId in strokes) {
          strokes = { ...strokes };
          delete strokes[strokeSettled.tempStrokeId];
        }
        strokePending = { ...strokePending };
        delete strokePending[strokeSettled.opId];
      }

      return {
        ...state,
        notes,
        strokes,
        pending,
        strokePending,
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
