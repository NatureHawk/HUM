/**
 * Wire types for the SyncBoard API. Mirrors backend/app/schemas exactly —
 * see backend/app/schemas/{board,note,events,realtime}.py. Do not add fields
 * the backend does not send; do not rename fields to be "more idiomatic".
 */

export type NoteTone = "accent" | "paper" | "ink";

export const TONES: NoteTone[] = ["accent", "paper", "ink"];

/** Mirrors the bounds enforced by app/models/note.py and its CHECK constraints. */
export const WORLD_LIMIT = 100_000;
export const MIN_SIZE = 120;
export const MAX_SIZE = 1200;
export const DEFAULT_WIDTH = 240;
export const DEFAULT_HEIGHT = 180;
export const MAX_ROTATION = 20;

/** A self-declared display identity — not an authentication boundary. */
export interface Actor {
  id: string;
  name: string;
}

/**
 * A sticky note.
 *
 * `x`/`y`/`width`/`height` are WORLD coordinates: the board's own space,
 * identical for every viewer at every zoom level. The camera that turns them
 * into screen pixels is per-browser and is never sent anywhere.
 */
export interface Note {
  id: string;
  board_id: string;
  title: string;
  body: string;
  x: number;
  y: number;
  width: number;
  height: number;
  rotation: number;
  z_index: number;
  tone: NoteTone;
  /** Guards CONTENT only. Moving or resizing a note does not change it. */
  version: number;
  author_name: string | null;
  created_at: string;
  updated_at: string;
}

export interface Board {
  id: string;
  title: string;
  revision: number;
  created_at: string;
  updated_at: string;
}

export type BoardSummary = Board & { note_count: number };

export interface BoardSnapshot {
  board: Board;
  notes: Note[];
  revision: number;
}

// ---------------------------------------------------------------------------
// Request bodies — one per operation, each naming only the fields it owns.
// ---------------------------------------------------------------------------

export interface NoteCreate {
  title?: string;
  body?: string;
  x: number;
  y: number;
  width?: number | null;
  height?: number | null;
  rotation?: number;
  tone?: NoteTone;
  op_id: string;
}

/** The only request carrying `expected_version`, and the only one that bumps it. */
export interface NoteContentUpdate {
  title?: string;
  body?: string;
  tone?: NoteTone;
  expected_version: number;
  op_id: string;
}

export interface NoteMove {
  x: number;
  y: number;
  rotation?: number | null;
  op_id: string;
}

export interface NoteResize {
  width: number;
  height: number;
  x?: number | null;
  y?: number | null;
  op_id: string;
}

export interface NoteReorder {
  /** An end, not a number — the server resolves it against the live stack. */
  to: "front" | "back";
  op_id: string;
}

export interface NoteDelete {
  op_id: string;
}

export interface BoardCreate {
  title: string;
}

// ---------------------------------------------------------------------------
// Events — the only thing that changes durable board state.
// ---------------------------------------------------------------------------

interface EventBase {
  id: string;
  board_id: string;
  revision: number;
  occurred_at: string;
  op_id: string | null;
  actor: Actor;
}

export interface NoteCreatedEvent extends EventBase {
  type: "note.created";
  note: Note;
}
export interface NoteUpdatedEvent extends EventBase {
  type: "note.updated";
  note: Note;
}
export interface NoteMovedEvent extends EventBase {
  type: "note.moved";
  note: Note;
  from_x: number;
  from_y: number;
}
export interface NoteResizedEvent extends EventBase {
  type: "note.resized";
  note: Note;
}
export interface NoteReorderedEvent extends EventBase {
  type: "note.reordered";
  note: Note;
  from_z_index: number;
}
export interface NoteDeletedEvent extends EventBase {
  type: "note.deleted";
  note_id: string;
}

export type BoardEvent =
  | NoteCreatedEvent
  | NoteUpdatedEvent
  | NoteMovedEvent
  | NoteResizedEvent
  | NoteReorderedEvent
  | NoteDeletedEvent;

export interface MutationResult {
  event: BoardEvent;
  board_revision: number;
}

// ---------------------------------------------------------------------------
// Realtime
// ---------------------------------------------------------------------------

export interface PresenceMember {
  connection_id: string;
  actor: Actor;
  connected_at: string;
  editing_note_id: string | null;
}

export interface SyncFrame {
  type: "sync";
  connection_id: string;
  actor: Actor;
  board: Board;
  notes: Note[];
  revision: number;
  presence: PresenceMember[];
}

/** A change to ONE connection's presence — a delta, keyed by connection_id. */
export interface PresenceFrame {
  type: "presence.joined" | "presence.left" | "presence.updated";
  board_id: string;
  occurred_at: string;
  member: PresenceMember;
}

/**
 * Somebody's cursor, in world coordinates. Ephemeral: no revision, never
 * stored. World rather than screen because every receiver looks through their
 * own camera, so a screen position would land somewhere different for each.
 */
export interface PointerFrame {
  type: "presence.pointer";
  board_id: string;
  connection_id: string;
  actor: Actor;
  x: number;
  y: number;
}

/**
 * Somebody is carrying a note and it is currently here. Also ephemeral: this
 * is how a drag is watched at pointer speed without anyone writing to Postgres
 * at pointer speed. `settled` marks the final frame of the gesture.
 */
export interface DragFrame {
  type: "presence.drag";
  board_id: string;
  connection_id: string;
  actor: Actor;
  note_id: string;
  x: number;
  y: number;
  settled: boolean;
}

export type WsErrorCode =
  | "unauthorized"
  | "board_not_found"
  | "invalid_event"
  | "stale_operation"
  | "rate_limited"
  | "message_too_large"
  | "server_error";

export interface WsErrorFrame {
  type: "error";
  code: WsErrorCode;
  message: string;
}

export interface PongFrame {
  type: "pong";
  server_time: string;
}

export type ServerFrame =
  | SyncFrame
  | PresenceFrame
  | PointerFrame
  | DragFrame
  | WsErrorFrame
  | PongFrame
  | BoardEvent;

/** No actor field anywhere: the actor is bound once, at the handshake. */
export type ClientMessage =
  | { type: "ping" }
  | { type: "resync" }
  | { type: "editing"; note_id: string | null }
  | { type: "pointer"; x: number; y: number }
  | { type: "drag"; note_id: string; x: number; y: number; settled: boolean };

export type ConnectionStatus = "connecting" | "open" | "reconnecting" | "closed";

export type ApiErrorCode =
  | "version_conflict"
  | "note_gone"
  | "not_found"
  | "invalid_operation"
  | "validation_error"
  | "no_session"
  | "board_full"
  | "payload_too_large"
  | "unknown_error";

export interface VersionConflictDetails {
  note: Note;
}

export interface ApiErrorBody {
  code: ApiErrorCode;
  message: string;
  details: unknown;
}
