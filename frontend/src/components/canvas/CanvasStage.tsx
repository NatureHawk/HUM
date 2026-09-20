"use client";

/**
 * The canvas viewport.
 *
 * The viewport is fixed; the workspace moves beneath it. One transformed layer
 * holds every note at its WORLD position, so panning and zooming change a
 * single CSS variable and re-render no notes at all — the compositor does the
 * work. That is also why there is no scrollbar anywhere: the page never
 * scrolls, the camera moves.
 *
 * Pointer handling is intentionally all in here rather than spread across the
 * notes. A drag is a gesture with three phases (grab, carry, release) and two
 * durability tiers (ephemeral socket frames while carrying, one REST commit on
 * release), and splitting that across components is how you end up with two
 * half-implementations of it.
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { NoteObject } from "@/components/canvas/NoteObject";
import { RemoteCursors } from "@/components/canvas/RemoteCursors";
import { StrokeLayer } from "@/components/canvas/StrokeLayer";
import {
  INITIAL_CAMERA,
  fitToNotes,
  panByScreen,
  screenToWorld,
  viewportCenterWorld,
  zoomAround,
  type Camera,
} from "@/lib/canvas/camera";
import { cn } from "@/lib/cn";
import type { RemoteCursor, RemoteDrag, RemoteStroke } from "@/lib/board/useBoard";
import {
  DEFAULT_STROKE_COLOR,
  DEFAULT_STROKE_WIDTH,
  MAX_SIZE,
  MAX_STROKE_POINTS,
  MIN_SIZE,
  type Note,
  type NoteTone,
  type Stroke,
  type StrokePoint,
} from "@/lib/types";

/**
 * How often live positions go out over the socket while a gesture is running.
 * ~30/s is smooth to watch and is two orders of magnitude fewer messages than
 * one per pointer event — and none of them touch Postgres regardless.
 */
const STREAM_INTERVAL_MS = 33;

type Gesture =
  | { kind: "none" }
  | { kind: "pan"; lastX: number; lastY: number }
  | {
      kind: "drag";
      noteId: string;
      /** Pointer offset from the note's origin, in world units. */
      grabDX: number;
      grabDY: number;
      x: number;
      y: number;
    }
  | { kind: "resize"; noteId: string; startW: number; startH: number; originX: number; originY: number }
  | { kind: "draw"; strokeId: string; points: StrokePoint[] };

/**
 * Notes and strokes are different object kinds sharing one selection slot —
 * Delete/Backspace/Escape need to know WHICH kind without a note id and a
 * stroke id ever being compared against each other by accident.
 */
type Selection = { kind: "note"; id: string } | { kind: "stroke"; id: string } | null;

export interface CanvasStageProps {
  notes: Note[];
  strokes: Stroke[];
  remoteDrags: Record<string, RemoteDrag>;
  remoteStrokes: Record<string, RemoteStroke>;
  cursors: RemoteCursor[];
  selfActorId: string;
  editingByOthers: Map<string, string>;
  tool: "select" | "pen";
  onCreateAt: (x: number, y: number) => void;
  /** A note that should open its own editor as soon as it appears. */
  autoEditId: string | null;
  onMove: (note: Note, x: number, y: number) => void;
  onResize: (note: Note, size: { width: number; height: number }) => void;
  onCommitText: (note: Note, updates: { title: string; body: string }, baseVersion: number) => void;
  onDelete: (note: Note) => void;
  onReorder: (note: Note, to: "front" | "back") => void;
  onEditingChange: (noteId: string | null) => void;
  onToneChange: (note: Note, tone: NoteTone) => void;
  /**
   * Every grab — a plain click as much as a drag start — bids for the front
   * of the stack. The client never assigns a z-index itself; it asks the
   * server to resolve "front" against whatever the live stack is, the same
   * way an explicit reorder does. See docs/canvas.md's stacking discussion.
   */
  onGrabNote: (note: Note) => void;
  onCreateStroke: (points: StrokePoint[]) => void;
  onDeleteStroke: (stroke: Stroke) => void;
  /** Selecting a stroke also bids it to the front, exactly like onGrabNote. */
  onSelectStroke: (stroke: Stroke) => void;
  sendDrag: (noteId: string, x: number, y: number, settled?: boolean) => void;
  sendPointer: (x: number, y: number) => void;
  sendDraw: (
    strokeId: string,
    points: StrokePoint[],
    color: string,
    width: number,
    settled?: boolean,
  ) => void;
  /** Lets the toolbar drive the camera without owning it. */
  onCameraReady: (controls: CameraControls) => void;
}

export interface CameraControls {
  zoomBy: (factor: number) => void;
  fit: () => void;
  reset: () => void;
  centerWorld: () => { x: number; y: number };
  zoom: number;
}

export function CanvasStage({
  notes,
  strokes,
  remoteDrags,
  remoteStrokes,
  cursors,
  selfActorId,
  editingByOthers,
  tool,
  onCreateAt,
  autoEditId,
  onMove,
  onResize,
  onCommitText,
  onDelete,
  onReorder,
  onEditingChange,
  onToneChange,
  onGrabNote,
  onCreateStroke,
  onDeleteStroke,
  onSelectStroke,
  sendDrag,
  sendPointer,
  sendDraw,
  onCameraReady,
}: CanvasStageProps) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const [camera, setCamera] = useState<Camera>(INITIAL_CAMERA);
  const [viewport, setViewport] = useState({ width: 0, height: 0 });
  const [selection, setSelection] = useState<Selection>(null);
  const [gesture, setGesture] = useState<Gesture>({ kind: "none" });
  const [spaceHeld, setSpaceHeld] = useState(false);

  // Live local position of the note THIS client is carrying. Kept out of board
  // state so a drag never enters the reducer, never gets a revision, and never
  // has to be rolled back.
  const [dragPosition, setDragPosition] = useState<{ x: number; y: number } | null>(null);

  const lastStreamedAt = useRef(0);
  const [hasFitted, setHasFitted] = useState(false);

  // -- viewport size -------------------------------------------------------

  useLayoutEffect(() => {
    const node = viewportRef.current;
    if (node === null) return;
    const observer = new ResizeObserver(([entry]) => {
      setViewport({
        width: entry.contentRect.width,
        height: entry.contentRect.height,
      });
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  // Frame the board once, on first load, so an existing board opens showing its
  // contents rather than an arbitrary corner of an empty field.
  //
  // Adjusted during render rather than in an effect: it is derived from the
  // first (notes, viewport) pair that is measurable, and doing it in an effect
  // would paint one frame at the wrong camera first.
  if (!hasFitted && viewport.width > 0 && notes.length > 0) {
    setHasFitted(true);
    setCamera((current) => fitToNotes(notes, viewport, current));
  }

  // -- camera controls for the toolbar -------------------------------------

  const zoomBy = useCallback(
    (factor: number) => {
      setCamera((current) =>
        zoomAround(
          current,
          { x: viewport.width / 2, y: viewport.height / 2 },
          current.zoom * factor,
        ),
      );
    },
    [viewport.height, viewport.width],
  );

  const fit = useCallback(() => {
    setCamera((current) => fitToNotes(notes, viewport, current));
  }, [notes, viewport]);

  const reset = useCallback(() => setCamera(INITIAL_CAMERA), []);

  const centerWorld = useCallback(
    () => viewportCenterWorld(camera, viewport),
    [camera, viewport],
  );

  useEffect(() => {
    onCameraReady({ zoomBy, fit, reset, centerWorld, zoom: camera.zoom });
  }, [onCameraReady, zoomBy, fit, reset, centerWorld, camera.zoom]);

  // -- wheel: zoom, or pan with a trackpad ---------------------------------

  useEffect(() => {
    const node = viewportRef.current;
    if (node === null) return;

    function onWheel(event: WheelEvent) {
      event.preventDefault();
      const rect = node!.getBoundingClientRect();
      const point = { x: event.clientX - rect.left, y: event.clientY - rect.top };

      // Ctrl/⌘ + wheel is the pinch gesture browsers synthesise on trackpads,
      // and is also the conventional zoom modifier for a mouse wheel.
      if (event.ctrlKey || event.metaKey) {
        setCamera((current) =>
          zoomAround(current, point, current.zoom * Math.exp(-event.deltaY * 0.01)),
        );
      } else {
        setCamera((current) => panByScreen(current, -event.deltaX, -event.deltaY));
      }
    }

    // Not passive: the whole point is to stop the browser zooming the page.
    node.addEventListener("wheel", onWheel, { passive: false });
    return () => node.removeEventListener("wheel", onWheel);
  }, []);

  // -- keyboard ------------------------------------------------------------

  useEffect(() => {
    function isTyping(target: EventTarget | null): boolean {
      const el = target as HTMLElement | null;
      return el?.tagName === "TEXTAREA" || el?.tagName === "INPUT";
    }

    function onKeyDown(event: KeyboardEvent) {
      if (event.code === "Space" && !isTyping(event.target)) {
        event.preventDefault();
        setSpaceHeld(true);
        return;
      }
      if (isTyping(event.target)) return;

      const selectedNote = selection?.kind === "note" ? notes.find((n) => n.id === selection.id) : undefined;
      const selectedStroke =
        selection?.kind === "stroke" ? strokes.find((s) => s.id === selection.id) : undefined;

      if (event.key === "Escape") setSelection(null);

      // Delete/Backspace works on whichever kind of object is selected.
      if ((selectedNote || selectedStroke) && (event.key === "Delete" || event.key === "Backspace")) {
        event.preventDefault();
        setSelection(null);
        if (selectedNote) onDelete(selectedNote);
        else if (selectedStroke) onDeleteStroke(selectedStroke);
      }

      // Reorder and arrow-nudge stay note-only: a stroke has no z shortcut
      // (selecting it already bids it to front) and no position to nudge —
      // its points are the drawing, not a box that can be moved.
      if (selectedNote && (event.key === "]" || (event.key === "ArrowUp" && event.shiftKey))) {
        event.preventDefault();
        onReorder(selectedNote, "front");
      }
      if (selectedNote && (event.key === "[" || (event.key === "ArrowDown" && event.shiftKey))) {
        event.preventDefault();
        onReorder(selectedNote, "back");
      }

      // Arrow keys nudge the selected note: the keyboard equivalent of a drag,
      // committed immediately since there is no gesture to stream.
      if (selectedNote && !event.shiftKey && event.key.startsWith("Arrow")) {
        event.preventDefault();
        const step = event.altKey ? 1 : 16;
        const dx = event.key === "ArrowLeft" ? -step : event.key === "ArrowRight" ? step : 0;
        const dy = event.key === "ArrowUp" ? -step : event.key === "ArrowDown" ? step : 0;
        onMove(selectedNote, selectedNote.x + dx, selectedNote.y + dy);
      }
    }

    function onKeyUp(event: KeyboardEvent) {
      if (event.code === "Space") setSpaceHeld(false);
    }

    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
    };
  }, [notes, strokes, onDelete, onDeleteStroke, onMove, onReorder, selection]);

  // -- pointer -------------------------------------------------------------

  /** Any event with client coordinates -> a point in world space. */
  const pointerToWorld = useCallback((event: { clientX: number; clientY: number }) => {
    const rect = viewportRef.current?.getBoundingClientRect();
    if (!rect) return { x: 0, y: 0 };
    return screenToWorld(
      { x: event.clientX - rect.left, y: event.clientY - rect.top },
      camera,
    );
  }, [camera]);

  const handleStagePointerDown = useCallback(
    (event: React.PointerEvent) => {
      // Middle mouse, or space-drag, or an empty-canvas drag: pan. This check
      // comes first regardless of tool, which is what keeps panning working
      // in PEN mode via space-drag / middle mouse.
      const wantsPan = spaceHeld || event.button === 1;
      if (event.button !== 0 && !wantsPan) return;

      (event.target as Element).setPointerCapture?.(event.pointerId);
      if (wantsPan) {
        setGesture({ kind: "pan", lastX: event.clientX, lastY: event.clientY });
        return;
      }

      if (tool === "pen") {
        const world = pointerToWorld(event);
        setGesture({ kind: "draw", strokeId: crypto.randomUUID(), points: [[world.x, world.y]] });
        return;
      }

      // Plain click on the background clears the selection.
      setSelection(null);
      setGesture({ kind: "pan", lastX: event.clientX, lastY: event.clientY });
    },
    [pointerToWorld, spaceHeld, tool],
  );

  const handleNotePointerDown = useCallback(
    (event: React.PointerEvent, note: Note) => {
      if (spaceHeld || event.button !== 0) return;
      // In PEN mode a note must not intercept the gesture: returning without
      // stopping propagation lets the pointer-down bubble to the stage handler
      // above, which starts a stroke — drawing over a note works exactly like
      // drawing over open canvas.
      if (tool === "pen") return;
      event.stopPropagation();
      (event.currentTarget as Element).setPointerCapture?.(event.pointerId);

      setSelection({ kind: "note", id: note.id });
      // Every grab — click or drag — bids for front. No optimistic z-index
      // here: the note is already lifted visually via the 10_000 zIndex
      // below while the gesture runs, so there is nothing to flicker while
      // this round-trips to the server.
      onGrabNote(note);
      const world = pointerToWorld(event);
      setGesture({
        kind: "drag",
        noteId: note.id,
        grabDX: world.x - note.x,
        grabDY: world.y - note.y,
        x: note.x,
        y: note.y,
      });
      setDragPosition({ x: note.x, y: note.y });
    },
    [onGrabNote, pointerToWorld, spaceHeld, tool],
  );

  const handleStrokePointerDown = useCallback(
    (event: React.PointerEvent, stroke: Stroke) => {
      // Defensive: StrokeLayer already gates pointer-events to SELECT mode via
      // CSS, but a stray event (e.g. a queued one from just before a tool
      // switch) should not be able to select through it.
      if (tool !== "select") return;
      event.stopPropagation();
      (event.currentTarget as Element).setPointerCapture?.(event.pointerId);
      setSelection({ kind: "stroke", id: stroke.id });
      onSelectStroke(stroke);
    },
    [onSelectStroke, tool],
  );

  const handleResizePointerDown = useCallback(
    (event: React.PointerEvent, note: Note) => {
      (event.currentTarget as Element).setPointerCapture?.(event.pointerId);
      const world = pointerToWorld(event);
      setGesture({
        kind: "resize",
        noteId: note.id,
        startW: note.width,
        startH: note.height,
        originX: world.x,
        originY: world.y,
      });
    },
    [pointerToWorld],
  );

  const [resizePreview, setResizePreview] = useState<{ w: number; h: number } | null>(null);

  const handlePointerMove = useCallback(
    (event: React.PointerEvent) => {
      const world = pointerToWorld(event);

      // Cursors are streamed regardless of gesture, at the same cadence as
      // drags. Ephemeral: no write, no revision.
      const now = performance.now();
      const due = now - lastStreamedAt.current >= STREAM_INTERVAL_MS;

      if (gesture.kind === "pan") {
        const dx = event.clientX - gesture.lastX;
        const dy = event.clientY - gesture.lastY;
        setCamera((current) => panByScreen(current, dx, dy));
        setGesture({ kind: "pan", lastX: event.clientX, lastY: event.clientY });
      } else if (gesture.kind === "drag") {
        const x = world.x - gesture.grabDX;
        const y = world.y - gesture.grabDY;
        // Local position updates on EVERY pointer event: the person doing the
        // dragging must never wait for a network round trip.
        setDragPosition({ x, y });
        setGesture({ ...gesture, x, y });
        if (due) sendDrag(gesture.noteId, x, y);
      } else if (gesture.kind === "resize") {
        const note = notes.find((n) => n.id === gesture.noteId);
        if (note) {
          setResizePreview({
            w: clamp(gesture.startW + (world.x - gesture.originX), MIN_SIZE, MAX_SIZE),
            h: clamp(gesture.startH + (world.y - gesture.originY), MIN_SIZE, MAX_SIZE),
          });
        }
      } else if (gesture.kind === "draw") {
        // The local path grows on EVERY pointer event — the person drawing
        // must never wait on the network to see their own line. Only the
        // OUTGOING frame is throttled, below.
        if (gesture.points.length < MAX_STROKE_POINTS) {
          const nextPoints: StrokePoint[] = [...gesture.points, [world.x, world.y]];
          setGesture({ kind: "draw", strokeId: gesture.strokeId, points: nextPoints });
          if (due) {
            sendDraw(gesture.strokeId, nextPoints, DEFAULT_STROKE_COLOR, DEFAULT_STROKE_WIDTH);
          }
        }
      }

      if (due) {
        lastStreamedAt.current = now;
        sendPointer(world.x, world.y);
      }
    },
    [gesture, notes, pointerToWorld, sendDraw, sendDrag, sendPointer],
  );

  const handlePointerUp = useCallback(() => {
    if (gesture.kind === "drag") {
      const note = notes.find((n) => n.id === gesture.noteId);
      // The one durable write of the whole gesture.
      if (note && (note.x !== gesture.x || note.y !== gesture.y)) {
        onMove(note, gesture.x, gesture.y);
      }
      // Tell observers to drop the rumour; the committed event is on its way.
      sendDrag(gesture.noteId, gesture.x, gesture.y, true);
      setDragPosition(null);
    }

    if (gesture.kind === "resize" && resizePreview) {
      const note = notes.find((n) => n.id === gesture.noteId);
      if (note && (note.width !== resizePreview.w || note.height !== resizePreview.h)) {
        onResize(note, { width: resizePreview.w, height: resizePreview.h });
      }
      setResizePreview(null);
    }

    if (gesture.kind === "draw") {
      // A stray click is not a stroke: fewer than two points commits nothing.
      if (gesture.points.length >= 2) {
        onCreateStroke(gesture.points);
      }
      // Settle regardless of point count, so an observer who saw an earlier
      // (unsettled) frame for this gesture drops the rumour either way.
      sendDraw(gesture.strokeId, gesture.points, DEFAULT_STROKE_COLOR, DEFAULT_STROKE_WIDTH, true);
    }

    setGesture({ kind: "none" });
  }, [gesture, notes, onCreateStroke, onMove, onResize, resizePreview, sendDraw, sendDrag]);

  const handleDoubleClick = useCallback(
    (event: React.MouseEvent) => {
      // PEN mode already owns double-click's pointer-down/up pair as two
      // strokes (or one continued one); it must not also drop a note.
      if (tool !== "select") return;
      const world = pointerToWorld(event);
      onCreateAt(world.x, world.y);
    },
    [onCreateAt, pointerToWorld, tool],
  );

  const panning = gesture.kind === "pan";

  return (
    <div
      ref={viewportRef}
      data-testid="canvas"
      className={cn(
        "relative h-full w-full touch-none overflow-hidden select-none",
        spaceHeld || panning
          ? "cursor-grabbing"
          : tool === "pen"
            ? "cursor-crosshair"
            : "cursor-default",
      )}
      style={
        {
          "--cam-x": camera.x,
          "--cam-y": camera.y,
          "--zoom": camera.zoom,
        } as React.CSSProperties
      }
      onPointerDown={handleStagePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      onPointerCancel={handlePointerUp}
      onDoubleClick={handleDoubleClick}
    >
      {/* The ground: grain plus a dot field anchored to the world, so it pans
          and zooms with the notes and the surface reads as a place. */}
      <div className="canvas-substrate" />

      {/*
       * The world layer. One transform for everything on the board.
       *
       *   screen = (world - camera) * zoom
       *
       * expressed as scale-then-translate with a top-left origin. Notes are
       * positioned in world units inside it and never see the camera.
       */}
      <div
        className="absolute top-0 left-0 origin-top-left will-change-transform"
        style={{
          transform: `scale(${camera.zoom}) translate(${-camera.x}px, ${-camera.y}px)`,
        }}
      >
        {notes.map((note) => {
          const remote = remoteDrags[note.id];
          const isDraggingThis = gesture.kind === "drag" && gesture.noteId === note.id;
          const isResizingThis = gesture.kind === "resize" && gesture.noteId === note.id;

          // Three sources of truth for where this note is drawn, in priority
          // order: my own live gesture, somebody else's live gesture, then the
          // committed board state.
          const shown: Note = {
            ...note,
            ...(isDraggingThis && dragPosition ? dragPosition : {}),
            ...(!isDraggingThis && remote ? { x: remote.x, y: remote.y } : {}),
            ...(isResizingThis && resizePreview
              ? { width: resizePreview.w, height: resizePreview.h }
              : {}),
          };

          return (
            <NoteObject
              key={note.id}
              note={shown}
              selected={selection?.kind === "note" && selection.id === note.id}
              dragging={isDraggingThis}
              carriedBy={!isDraggingThis && remote ? remote.actor.name : null}
              editedBy={editingByOthers.get(note.id) ?? null}
              optimistic={note.id.startsWith("temp-")}
              autoEdit={autoEditId === note.id}
              zoom={camera.zoom}
              onPointerDown={handleNotePointerDown}
              onResizePointerDown={handleResizePointerDown}
              onCommitText={onCommitText}
              onEditingChange={onEditingChange}
              onToneChange={onToneChange}
            />
          );
        })}

        {/*
         * Strokes and notes share one z-index sequence server-side. StrokeLayer
         * renders each stroke as its own positioned wrapper div (not a shared
         * svg — see its header comment for why that doesn't interleave with
         * NoteObject's divs), so the two genuinely interleave by z-index
         * instead of strokes sitting in one flat layer above or below every
         * note.
         */}
        <StrokeLayer
          strokes={strokes}
          remoteStrokes={remoteStrokes}
          localStroke={
            gesture.kind === "draw"
              ? { points: gesture.points, color: DEFAULT_STROKE_COLOR, width: DEFAULT_STROKE_WIDTH }
              : null
          }
          selectedStrokeId={selection?.kind === "stroke" ? selection.id : null}
          tool={tool}
          zoom={camera.zoom}
          onStrokePointerDown={handleStrokePointerDown}
        />

        <RemoteCursors cursors={cursors} selfActorId={selfActorId} zoom={camera.zoom} />
      </div>
    </div>
  );
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}
