"use client";

/**
 * The board screen: a full-bleed canvas with a quiet header floating over it.
 *
 * The header is an overlay rather than a layout row, because the canvas wants
 * the whole window — a workspace that gives back a strip of itself to chrome
 * feels like a web page, not a place.
 */

import Link from "next/link";
import { useCallback, useMemo, useRef, useState } from "react";
import { CanvasStage, type CameraControls } from "@/components/canvas/CanvasStage";
import { CanvasToolbar } from "@/components/canvas/CanvasToolbar";
import { PresenceRail } from "@/components/presence/PresenceRail";
import { Spinner } from "@/components/ui/Spinner";
import { useBoard } from "@/lib/board/useBoard";
import { cn } from "@/lib/cn";
import { useSession } from "@/lib/session";
import { DEFAULT_HEIGHT, DEFAULT_WIDTH, type Note, type NoteTone } from "@/lib/types";

export function BoardCanvas({ boardId }: { boardId: string }) {
  const { actor, failed, rename } = useSession();

  if (actor === null) {
    if (failed) {
      // The session handshake is a prerequisite for everything else on this
      // screen, so a failure here gets the same calm, centred treatment as a
      // board that would not load — not a new error design.
      return (
        <div className="flex h-dvh flex-col items-center justify-center gap-3 px-6 text-center">
          <p className="font-display text-3xl tracking-tight">This board would not load.</p>
          <p className="max-w-sm text-sm text-ink-soft">
            Could not reach the server. Check your connection and try again.
          </p>
        </div>
      );
    }
    return (
      <div className="flex h-dvh items-center justify-center">
        <Spinner />
      </div>
    );
  }

  return <Workspace boardId={boardId} actor={actor} onRename={rename} />;
}

function Workspace({
  boardId,
  actor,
  onRename,
}: {
  boardId: string;
  actor: { id: string; name: string };
  onRename: (name: string) => void;
}) {
  const board = useBoard(boardId, actor);
  const { state, notes } = board;

  const [tone, setTone] = useState<NoteTone>("accent");
  // The note whose editor should open by itself. Creation is meant to be one
  // gesture - place it and start typing - not place it, then go back and
  // click it.
  const [autoEditId, setAutoEditId] = useState<string | null>(null);
  const cameraRef = useRef<CameraControls | null>(null);
  const [zoom, setZoom] = useState(1);

  const onCameraReady = useCallback((controls: CameraControls) => {
    cameraRef.current = controls;
    setZoom(controls.zoom);
  }, []);

  /** note id -> the name of a DIFFERENT person with it open. */
  const editingByOthers = useMemo(() => {
    const map = new Map<string, string>();
    for (const member of board.presence) {
      if (member.editing_note_id === null) continue;
      if (member.actor.id === actor.id) continue;
      map.set(member.editing_note_id, member.actor.name);
    }
    return map;
  }, [board.presence, actor.id]);

  const createAt = useCallback(
    async (x: number, y: number) => {
      // The pointer lands at the note's centre, which is where people expect
      // the thing they just asked for to appear.
      const id = await board.createNote({
        x: x - DEFAULT_WIDTH / 2,
        y: y - DEFAULT_HEIGHT / 2,
        tone,
      });
      // The REAL id, not the optimistic placeholder: the placeholder is gone
      // by now, replaced by the authoritative note.
      if (id !== null) setAutoEditId(id);
    },
    [board, tone],
  );

  const createAtCenter = useCallback(() => {
    const centre = cameraRef.current?.centerWorld() ?? { x: 0, y: 0 };
    void createAt(centre.x, centre.y);
  }, [createAt]);

  const commitText = useCallback(
    (note: Note, updates: { title: string; body: string }, baseVersion: number) => {
      // Cleared here rather than by the note itself: this is an event handler,
      // so it costs no extra render, and it stops a remounted note (after a
      // resync, say) from re-opening an editor nobody asked for.
      setAutoEditId(null);
      void board.updateNote(note, updates, baseVersion);
    },
    [board],
  );

  const loading = state.status === "loading" && state.board === null;
  const failed = state.status === "error" && state.board === null;

  return (
    <main className="relative h-dvh w-screen overflow-hidden">
      {failed ? (
        <div className="flex h-full flex-col items-center justify-center gap-3 px-6 text-center">
          <p className="font-display text-3xl tracking-tight">This board would not load.</p>
          <p className="max-w-sm text-sm text-ink-soft">{state.error}</p>
          <button
            type="button"
            onClick={() => void board.resync()}
            className="mt-2 rounded-[var(--radius)] bg-ink px-4 py-2 text-paper"
          >
            <span className="micro-label">Try again</span>
          </button>
        </div>
      ) : (
        <CanvasStage
          notes={notes}
          remoteDrags={board.remoteDrags}
          cursors={board.cursors}
          selfActorId={actor.id}
          editingByOthers={editingByOthers}
          onCreateAt={(x, y) => void createAt(x, y)}
          autoEditId={autoEditId}
          onMove={(note, x, y) => void board.moveNote(note, x, y)}
          onResize={(note, size) => void board.resizeNote(note, size)}
          onCommitText={commitText}
          onDelete={(note) => void board.deleteNote(note)}
          onReorder={(note, to) => void board.reorderNote(note, to)}
          onEditingChange={board.setEditing}
          sendDrag={board.sendDrag}
          sendPointer={board.sendPointer}
          onCameraReady={onCameraReady}
        />
      )}

      {/* ---- chrome, floating over the canvas ---- */}

      <header className="pointer-events-none absolute inset-x-0 top-0 z-30 flex items-start justify-between gap-4 p-4">
        <div className="pointer-events-auto flex items-center gap-3 rounded-[var(--radius-lg)] border border-hair bg-paper-up/92 py-2 pr-4 pl-3 shadow-[var(--shadow-nav)] backdrop-blur-[2px]">
          <Link
            href="/"
            aria-label="All boards"
            title="All boards"
            className="flex h-5 w-5 items-center justify-center text-ink-faint transition-colors hover:text-ink"
          >
            {/* Drawn rather than set: the mono face has no arrow, and a
                hyphen standing in for one looks like a mistake. */}
            <svg width="13" height="10" viewBox="0 0 13 10" aria-hidden="true">
              <path
                d="M5 1L1 5l4 4M1 5h11"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.4"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </Link>
          <h1 className="max-w-[38vw] truncate font-display text-lg leading-none font-medium tracking-tight">
            {state.board?.title ?? " "}
          </h1>
          {loading ? <Spinner size="sm" /> : null}
        </div>

        <div className="pointer-events-auto rounded-[var(--radius-lg)] border border-hair bg-paper-up/92 px-3 py-2 shadow-[var(--shadow-nav)] backdrop-blur-[2px]">
          <PresenceRail
            actor={actor}
            presence={board.presence}
            connection={board.connection}
            onRename={onRename}
          />
        </div>
      </header>

      <div className="pointer-events-none absolute inset-x-0 bottom-0 z-30 flex justify-center p-4">
        <CanvasToolbar
          zoom={zoom}
          tone={tone}
          onToneChange={setTone}
          onCreate={createAtCenter}
          onZoomIn={() => cameraRef.current?.zoomBy(1.25)}
          onZoomOut={() => cameraRef.current?.zoomBy(0.8)}
          onFit={() => cameraRef.current?.fit()}
        />
      </div>

      <Notices
        error={state.error}
        onDismissError={board.dismissError}
        notice={board.resyncNotice}
        onDismissNotice={board.dismissResyncNotice}
      />

      {/* An empty board should say so, and say what to do about it. */}
      {!loading && !failed && notes.length === 0 ? (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center">
          <div className="max-w-sm px-6 text-center">
            <p className="font-display text-4xl leading-[1.05] tracking-tight">
              Nothing here yet.
            </p>
            <p className="mt-3 text-[15px] leading-relaxed text-ink-soft">
              Double-click anywhere to leave a note. Drag it wherever it belongs — everyone
              else watches it move.
            </p>
          </div>
        </div>
      ) : null}
    </main>
  );
}

function Notices({
  error,
  onDismissError,
  notice,
  onDismissNotice,
}: {
  error: string | null;
  onDismissError: () => void;
  notice: string | null;
  onDismissNotice: () => void;
}) {
  if (error === null && notice === null) return null;

  return (
    <div
      className="pointer-events-none absolute inset-x-0 top-20 z-40 flex flex-col items-center gap-2 px-4"
      role="status"
      aria-live="polite"
    >
      {error !== null ? (
        <Bar tone="danger" onDismiss={onDismissError}>
          {error}
        </Bar>
      ) : null}
      {notice !== null ? (
        <Bar tone="accent" onDismiss={onDismissNotice}>
          {notice}
        </Bar>
      ) : null}
    </div>
  );
}

function Bar({
  tone,
  children,
  onDismiss,
}: {
  tone: "danger" | "accent";
  children: React.ReactNode;
  onDismiss: () => void;
}) {
  return (
    <div
      className={cn(
        "pointer-events-auto flex items-center gap-3 rounded-[var(--radius)] border px-3 py-2",
        "text-[13px] shadow-[var(--shadow-card)]",
        tone === "danger"
          ? "border-danger/30 bg-danger-soft text-danger"
          : "border-accent-deep bg-accent text-ink",
      )}
    >
      <span className="min-w-0">{children}</span>
      <button
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss"
        className="shrink-0 text-base leading-none opacity-60 hover:opacity-100"
      >
        &times;
      </button>
    </div>
  );
}
