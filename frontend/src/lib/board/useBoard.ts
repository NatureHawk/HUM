"use client";

/**
 * Wires the board reducer to the API client and the socket.
 *
 * Every durable mutation follows one shape: optimistic apply -> call REST ->
 * feed the response's event through `applyEvent` -> settle. On failure, roll
 * back and surface the message. That is unchanged from Milestone 1.
 *
 * What Milestone 3 adds is a second, EPHEMERAL tier that never touches
 * Postgres: live drag positions and cursors, broadcast over the socket and
 * held only in React state. See docs/canvas.md section 3.
 */

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { ApiError, api } from "@/lib/api";
import {
  boardReducer,
  initialBoardState,
  type BoardState,
  type OptimisticChange,
  type StrokeChange,
} from "@/lib/board/reducer";
import { BoardSocket } from "@/lib/realtime/socket";
import {
  DEFAULT_HEIGHT,
  DEFAULT_STROKE_COLOR,
  DEFAULT_STROKE_WIDTH,
  DEFAULT_WIDTH,
  type Actor,
  type ConnectionStatus,
  type MutationResult,
  type Note,
  type NoteTone,
  type PresenceMember,
  type ServerFrame,
  type Stroke,
  type StrokePoint,
  type VersionConflictDetails,
} from "@/lib/types";

function newOpId(): string {
  return crypto.randomUUID();
}

function messageFor(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message;
  return fallback;
}

/**
 * Where somebody else is currently carrying a note.
 *
 * Deliberately NOT in the reducer: these positions are rumours, not board
 * state. They carry no revision, are never persisted, and are dropped the
 * moment the gesture settles — at which point the real `note.moved` event has
 * already updated the durable position.
 */
export interface RemoteDrag {
  noteId: string;
  x: number;
  y: number;
  actor: Actor;
}

export interface RemoteCursor {
  connectionId: string;
  actor: Actor;
  x: number;
  y: number;
  at: number;
}

/**
 * Somebody else's stroke as it is being drawn. Same rumour treatment as
 * RemoteDrag: no revision, never persisted, gone the instant the gesture
 * settles because the authoritative `stroke.created` event has landed by
 * then. Keyed by the drawing client's own `stroke_id`, not a database id —
 * that id does not exist yet.
 */
export interface RemoteStroke {
  strokeId: string;
  points: StrokePoint[];
  color: string;
  width: number;
  actor: Actor;
}

/** A cursor nobody has moved for this long is stale and stops being drawn. */
const CURSOR_TTL_MS = 8_000;

export interface UseBoardResult {
  state: BoardState;
  /** Notes in paint order, lowest z first. */
  notes: Note[];
  /** Strokes in paint order, lowest z first. */
  strokes: Stroke[];
  presence: PresenceMember[];
  connection: ConnectionStatus;
  remoteDrags: Record<string, RemoteDrag>;
  /** Ephemeral: somebody else's stroke as it is being drawn, keyed by stroke_id. */
  remoteStrokes: Record<string, RemoteStroke>;
  cursors: RemoteCursor[];
  lastSyncedAt: Date | null;
  resyncNotice: string | null;
  dismissResyncNotice: () => void;
  dismissError: () => void;

  createNote: (input: {
    x: number;
    y: number;
    title?: string;
    tone?: NoteTone;
  }) => Promise<string | null>;
  updateNote: (
    note: Note,
    updates: { title?: string; body?: string; tone?: NoteTone },
    expectedVersion?: number,
  ) => Promise<void>;
  moveNote: (note: Note, x: number, y: number) => Promise<void>;
  resizeNote: (
    note: Note,
    size: { width: number; height: number; x?: number; y?: number },
  ) => Promise<void>;
  reorderNote: (note: Note, to: "front" | "back") => Promise<void>;
  deleteNote: (note: Note) => Promise<void>;

  createStroke: (input: {
    points: StrokePoint[];
    color?: string;
    width?: number;
  }) => Promise<string | null>;
  reorderStroke: (stroke: Stroke, to: "front" | "back") => Promise<void>;
  deleteStroke: (stroke: Stroke) => Promise<void>;

  /** Ephemeral: tell everyone where a note is being carried right now. */
  sendDrag: (noteId: string, x: number, y: number, settled?: boolean) => void;
  /** Ephemeral: tell everyone where this cursor is, in world coordinates. */
  sendPointer: (x: number, y: number) => void;
  /** Ephemeral: tell everyone what is being drawn right now, in world coords. */
  sendDraw: (
    strokeId: string,
    points: StrokePoint[],
    color: string,
    width: number,
    settled?: boolean,
  ) => void;
  setEditing: (noteId: string | null) => void;
  resync: () => Promise<void>;
}

export function useBoard(boardId: string, actor: Actor): UseBoardResult {
  const [state, dispatch] = useReducer(boardReducer, initialBoardState);
  const [lastSyncedAt, setLastSyncedAt] = useState<Date | null>(null);
  const [presence, setPresence] = useState<PresenceMember[]>([]);
  const [connection, setConnection] = useState<ConnectionStatus>("connecting");
  const [remoteDrags, setRemoteDrags] = useState<Record<string, RemoteDrag>>({});
  const [remoteStrokes, setRemoteStrokes] = useState<Record<string, RemoteStroke>>({});
  const [cursors, setCursors] = useState<RemoteCursor[]>([]);
  const [resyncNotice, setResyncNotice] = useState<string | null>(null);

  const socketRef = useRef<BoardSocket | null>(null);
  const hasLoadedOnce = useRef(false);
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const dismissResyncNotice = useCallback(() => setResyncNotice(null), []);
  const dismissError = useCallback(() => dispatch({ type: "errorCleared" }), []);

  const showNotice = useCallback((message: string) => {
    setResyncNotice(message);
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
    noticeTimer.current = setTimeout(() => setResyncNotice(null), 5000);
  }, []);

  const loadSnapshot = useCallback(
    async (notice?: string) => {
      // Deliberately no synchronous dispatch before the await: this runs from
      // an effect, and a synchronous setState there cascades renders.
      try {
        const snapshot = await api.getBoard(boardId);
        dispatch({
          type: "snapshotLoaded",
          board: snapshot.board,
          notes: snapshot.notes,
          strokes: snapshot.strokes,
          revision: snapshot.revision,
        });
        setLastSyncedAt(new Date());
        if (notice && hasLoadedOnce.current) showNotice(notice);
        hasLoadedOnce.current = true;
      } catch (err) {
        dispatch({
          type: "loadFailed",
          message: messageFor(err, "Failed to load this board."),
        });
      }
    },
    [boardId, showNotice],
  );

  // REST load on mount as well as over the socket. Not redundant: the `sync`
  // frame is the fast path, but this is what still renders the board if
  // WebSockets are blocked or the server is mid-restart. The reducer ignores
  // whichever snapshot turns out to be older, so they cannot race backwards.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadSnapshot();
  }, [loadSnapshot]);

  const handleFrame = useCallback((frame: ServerFrame) => {
    switch (frame.type) {
      case "sync": {
        setPresence(frame.presence);
        dispatch({
          type: "snapshotLoaded",
          board: frame.board,
          notes: frame.notes,
          strokes: frame.strokes,
          revision: frame.revision,
        });
        setLastSyncedAt(new Date());
        hasLoadedOnce.current = true;
        break;
      }

      case "presence.joined":
      case "presence.updated": {
        setPresence((current) => {
          const rest = current.filter(
            (m) => m.connection_id !== frame.member.connection_id,
          );
          return [...rest, frame.member].sort((a, b) =>
            a.connected_at === b.connected_at
              ? a.connection_id < b.connection_id
                ? -1
                : 1
              : a.connected_at < b.connected_at
                ? -1
                : 1,
          );
        });
        break;
      }

      case "presence.left": {
        setPresence((current) =>
          current.filter((m) => m.connection_id !== frame.member.connection_id),
        );
        // Their cursor goes with them, rather than waiting out the TTL.
        setCursors((current) =>
          current.filter((c) => c.connectionId !== frame.member.connection_id),
        );
        break;
      }

      case "presence.pointer": {
        setCursors((current) => {
          const rest = current.filter((c) => c.connectionId !== frame.connection_id);
          return [
            ...rest,
            {
              connectionId: frame.connection_id,
              actor: frame.actor,
              x: frame.x,
              y: frame.y,
              at: Date.now(),
            },
          ];
        });
        break;
      }

      case "presence.drag": {
        // A rumour about where a note is being carried. Held until the gesture
        // settles, at which point the authoritative `note.moved` event has
        // already landed and board state is correct again.
        setRemoteDrags((current) => {
          if (frame.settled) {
            if (!(frame.note_id in current)) return current;
            const next = { ...current };
            delete next[frame.note_id];
            return next;
          }
          return {
            ...current,
            [frame.note_id]: {
              noteId: frame.note_id,
              x: frame.x,
              y: frame.y,
              actor: frame.actor,
            },
          };
        });
        break;
      }

      case "presence.draw": {
        // A rumour about an in-progress stroke, exactly like presence.drag for
        // notes: held until the gesture settles, at which point the
        // authoritative `stroke.created` event has already landed.
        setRemoteStrokes((current) => {
          if (frame.settled) {
            if (!(frame.stroke_id in current)) return current;
            const next = { ...current };
            delete next[frame.stroke_id];
            return next;
          }
          return {
            ...current,
            [frame.stroke_id]: {
              strokeId: frame.stroke_id,
              points: frame.points,
              color: frame.color,
              width: frame.width,
              actor: frame.actor,
            },
          };
        });
        break;
      }

      case "pong":
        break;

      case "error": {
        if (frame.code === "board_not_found") {
          dispatch({ type: "loadFailed", message: "This board no longer exists." });
        }
        break;
      }

      default:
        // A note event. Straight into the same action REST responses use.
        dispatch({ type: "eventReceived", event: frame });
    }
  }, []);

  useEffect(() => {
    const socket = new BoardSocket(boardId, {
      onFrame: handleFrame,
      onStatusChange: setConnection,
    });
    socketRef.current = socket;
    socket.connect();

    return () => {
      // Closing on unmount prevents a second mount (React's development
      // double-invoke, or a route change) leaving a stale socket delivering
      // events into a dead reducer.
      socket.close();
      socketRef.current = null;
    };
  }, [boardId, handleFrame]);

  // A cursor whose owner stopped moving, or whose tab froze, should fade out
  // rather than sit on the canvas forever. The socket's `presence.left` covers
  // a clean disconnect; this covers everything else.
  useEffect(() => {
    const timer = setInterval(() => {
      const cutoff = Date.now() - CURSOR_TTL_MS;
      setCursors((current) =>
        current.some((c) => c.at < cutoff) ? current.filter((c) => c.at >= cutoff) : current,
      );
    }, 2000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (state.status === "desynced") {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      void loadSnapshot("Resynced — someone else had changed this board.");
    }
  }, [state.status, loadSnapshot]);

  useEffect(
    () => () => {
      if (noticeTimer.current) clearTimeout(noticeTimer.current);
    },
    [],
  );

  const runMutation = useCallback(
    async (
      change: OptimisticChange,
      mutate: (opId: string) => Promise<MutationResult>,
      tempNoteId?: string,
    ): Promise<MutationResult | null> => {
      // The SAME op_id goes into the optimistic record and the request body.
      // That is what lets this client recognise the broadcast of its own
      // mutation when it arrives over the socket, possibly before the HTTP
      // response does.
      const opId = newOpId();
      dispatch({ type: "optimisticApply", opId, change, tempNoteId });
      try {
        const result = await mutate(opId);
        if (result.event === null) {
          // The server succeeded and changed nothing - see MutationResult in
          // lib/types.ts. No authoritative note is coming, so the optimistic
          // guess must be undone here rather than left to be corrected by an
          // event that will never arrive.
          dispatch({ type: "opReverted", opId });
          return result;
        }
        dispatch({ type: "eventReceived", event: result.event });
        dispatch({ type: "opSettled", opId, tempNoteId });
        return result;
      } catch (err) {
        dispatch({
          type: "opFailed",
          opId,
          message: messageFor(err, "That change didn't go through."),
        });

        if (err instanceof ApiError) {
          if (err.code === "version_conflict") {
            const details = err.details as VersionConflictDetails | null | undefined;
            if (details?.note) dispatch({ type: "noteOverride", note: details.note });
          } else if (err.code === "note_gone") {
            void loadSnapshot("Resynced — a note you touched had already changed.");
          }
        }
        return null;
      }
    },
    [loadSnapshot],
  );

  // Mirrors runMutation exactly, one level down: a separate function rather
  // than a generic `runMutation<T>` over both entity kinds, because the two
  // already diverge in their rollback state (pending vs strokePending,
  // noteOverride has no stroke equivalent) and forcing them through one
  // generic would just move that branching inside it instead of removing it.
  const runStrokeMutation = useCallback(
    async (
      change: StrokeChange,
      mutate: (opId: string) => Promise<MutationResult>,
      tempStrokeId?: string,
    ): Promise<MutationResult | null> => {
      const opId = newOpId();
      dispatch({ type: "strokeOptimisticApply", opId, change, tempStrokeId });
      try {
        const result = await mutate(opId);
        if (result.event === null) {
          dispatch({ type: "strokeOpReverted", opId });
          return result;
        }
        dispatch({ type: "eventReceived", event: result.event });
        dispatch({ type: "strokeOpSettled", opId, tempStrokeId });
        return result;
      } catch (err) {
        dispatch({
          type: "strokeOpFailed",
          opId,
          message: messageFor(err, "That change didn't go through."),
        });

        if (err instanceof ApiError && err.code === "stroke_gone") {
          void loadSnapshot("Resynced — a stroke you touched had already changed.");
        }
        return null;
      }
    },
    [loadSnapshot],
  );

  const createNote = useCallback<UseBoardResult["createNote"]>(
    async ({ x, y, title = "", tone = "accent" }) => {
      const tempId = `temp-${newOpId()}`;
      const now = new Date().toISOString();
      const highest = Object.values(state.notes).reduce(
        (max, n) => Math.max(max, n.z_index),
        0,
      );
      const optimistic: Note = {
        id: tempId,
        board_id: boardId,
        title,
        body: "",
        x,
        y,
        width: DEFAULT_WIDTH,
        height: DEFAULT_HEIGHT,
        // A small resting tilt, so a fresh note lands on the board rather than
        // snapping to a grid. The server keeps whatever we send.
        rotation: Math.round((Math.random() * 3 - 1.5) * 10) / 10,
        z_index: highest + 1,
        tone,
        version: 1,
        author_name: actor.name,
        created_at: now,
        updated_at: now,
      };

      const result = await runMutation(
        { kind: "upsert", note: optimistic },
        (opId) =>
          api.createNote(boardId, {
            title,
            x,
            y,
            rotation: optimistic.rotation,
            tone,
            op_id: opId,
          }),
        tempId,
      );

      // The REAL id, so the caller can open the editor on the note that will
      // still be there a moment from now. The optimistic placeholder is gone by
      // the time this resolves.
      if (result === null || result.event === null) return null;
      return "note" in result.event ? result.event.note.id : null;
    },
    [actor, boardId, runMutation, state.notes],
  );

  const updateNote = useCallback<UseBoardResult["updateNote"]>(
    async (note, updates, expectedVersion) => {
      const optimistic: Note = { ...note, ...updates, updated_at: new Date().toISOString() };
      await runMutation({ kind: "upsert", note: optimistic }, (opId) =>
        api.updateNote(note.id, {
          ...updates,
          // The version the edit was BASED ON, not the note's latest. Sending
          // the latest would claim to have seen a change the user never saw.
          expected_version: expectedVersion ?? note.version,
          op_id: opId,
        }),
      );
    },
    [runMutation],
  );

  const moveNote = useCallback<UseBoardResult["moveNote"]>(
    async (note, x, y) => {
      const optimistic: Note = { ...note, x, y };
      await runMutation({ kind: "upsert", note: optimistic }, (opId) =>
        api.moveNote(note.id, { x, y, op_id: opId }),
      );
    },
    [runMutation],
  );

  const resizeNote = useCallback<UseBoardResult["resizeNote"]>(
    async (note, size) => {
      const optimistic: Note = {
        ...note,
        width: size.width,
        height: size.height,
        x: size.x ?? note.x,
        y: size.y ?? note.y,
      };
      await runMutation({ kind: "upsert", note: optimistic }, (opId) =>
        api.resizeNote(note.id, { ...size, op_id: opId }),
      );
    },
    [runMutation],
  );

  const reorderNote = useCallback<UseBoardResult["reorderNote"]>(
    async (note, to) => {
      // No optimistic z: the server resolves "front"/"back" against the live
      // stack, and guessing a number here could disagree with it.
      await runMutation({ kind: "upsert", note }, (opId) =>
        api.reorderNote(note.id, { to, op_id: opId }),
      );
    },
    [runMutation],
  );

  const deleteNote = useCallback<UseBoardResult["deleteNote"]>(
    async (note) => {
      await runMutation({ kind: "delete", noteId: note.id }, (opId) =>
        api.deleteNote(note.id, { op_id: opId }),
      );
    },
    [runMutation],
  );

  const createStroke = useCallback<UseBoardResult["createStroke"]>(
    async ({ points, color = DEFAULT_STROKE_COLOR, width = DEFAULT_STROKE_WIDTH }) => {
      const tempId = `temp-${newOpId()}`;
      const now = new Date().toISOString();
      // Shares one stacking sequence with notes, so a fresh stroke has to
      // clear the highest z_index across BOTH — a stroke drawn over a note
      // must land above it, and vice versa.
      const highest = Math.max(
        Object.values(state.notes).reduce((max, n) => Math.max(max, n.z_index), 0),
        Object.values(state.strokes).reduce((max, s) => Math.max(max, s.z_index), 0),
      );
      const optimistic: Stroke = {
        id: tempId,
        board_id: boardId,
        points,
        color,
        width,
        z_index: highest + 1,
        version: 1,
        author_name: actor.name,
        created_at: now,
        updated_at: now,
      };

      const result = await runStrokeMutation(
        { kind: "upsert", stroke: optimistic },
        (opId) => api.createStroke(boardId, { points, color, width, op_id: opId }),
        tempId,
      );

      if (result === null || result.event === null) return null;
      return "stroke" in result.event ? result.event.stroke.id : null;
    },
    [actor, boardId, runStrokeMutation, state.notes, state.strokes],
  );

  const reorderStroke = useCallback<UseBoardResult["reorderStroke"]>(
    async (stroke, to) => {
      // No optimistic z here either — same reasoning as reorderNote.
      await runStrokeMutation({ kind: "upsert", stroke }, (opId) =>
        api.reorderStroke(stroke.id, { to, op_id: opId }),
      );
    },
    [runStrokeMutation],
  );

  const deleteStroke = useCallback<UseBoardResult["deleteStroke"]>(
    async (stroke) => {
      await runStrokeMutation({ kind: "delete", strokeId: stroke.id }, (opId) =>
        api.deleteStroke(stroke.id, { op_id: opId }),
      );
    },
    [runStrokeMutation],
  );

  const sendDrag = useCallback<UseBoardResult["sendDrag"]>(
    (noteId, x, y, settled = false) => {
      socketRef.current?.send({ type: "drag", note_id: noteId, x, y, settled });
    },
    [],
  );

  const sendPointer = useCallback<UseBoardResult["sendPointer"]>((x, y) => {
    socketRef.current?.send({ type: "pointer", x, y });
  }, []);

  const sendDraw = useCallback<UseBoardResult["sendDraw"]>(
    (strokeId, points, color, width, settled = false) => {
      socketRef.current?.send({
        type: "draw",
        stroke_id: strokeId,
        points,
        color,
        width,
        settled,
      });
    },
    [],
  );

  const setEditing = useCallback((noteId: string | null) => {
    socketRef.current?.send({ type: "editing", note_id: noteId });
  }, []);

  const resync = useCallback(async () => {
    dispatch({ type: "loadStarted" });
    await loadSnapshot("Resynced.");
  }, [loadSnapshot]);

  // Paint order: z first, then id, so every client stacks an overlap the same.
  const notes = useMemo(
    () =>
      Object.values(state.notes).sort((a, b) =>
        a.z_index !== b.z_index ? a.z_index - b.z_index : a.id < b.id ? -1 : 1,
      ),
    [state.notes],
  );

  const strokes = useMemo(
    () =>
      Object.values(state.strokes).sort((a, b) =>
        a.z_index !== b.z_index ? a.z_index - b.z_index : a.id < b.id ? -1 : 1,
      ),
    [state.strokes],
  );

  return {
    state,
    notes,
    strokes,
    presence,
    connection,
    remoteDrags,
    remoteStrokes,
    cursors,
    lastSyncedAt,
    resyncNotice,
    dismissResyncNotice,
    dismissError,
    createNote,
    updateNote,
    moveNote,
    resizeNote,
    reorderNote,
    deleteNote,
    createStroke,
    reorderStroke,
    deleteStroke,
    sendDrag,
    sendPointer,
    sendDraw,
    setEditing,
    resync,
  };
}
