"use client";

/**
 * A sticky note on the canvas.
 *
 * Follows HUM's object anatomy: an OUTER element that owns placement and
 * nothing else, and an INNER sheet that owns its resting rotation and hover
 * lean. Keeping those on separate elements is what lets a note be dragged and
 * tilted at once without the two transforms fighting.
 *
 * Everything here is in WORLD units. The note has no idea a camera exists —
 * the stage applies one transform to the whole layer, so panning and zooming
 * re-render nothing.
 */

import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/cn";
import type { Note } from "@/lib/types";

export interface NoteObjectProps {
  note: Note;
  selected: boolean;
  /** True while THIS client is carrying it. */
  dragging: boolean;
  /** Name of someone else carrying it right now, from an ephemeral drag frame. */
  carriedBy: string | null;
  /** Name of someone else with its editor open, from presence. */
  editedBy: string | null;
  /** True for a note whose creation has not been acknowledged yet. */
  optimistic: boolean;
  /** Open this note's editor immediately - it was just created. */
  autoEdit: boolean;
  zoom: number;
  onPointerDown: (event: React.PointerEvent, note: Note) => void;
  onResizePointerDown: (event: React.PointerEvent, note: Note) => void;
  onCommitText: (note: Note, updates: { title: string; body: string }, baseVersion: number) => void;
  onEditingChange: (noteId: string | null) => void;
}

const TONE_CLASS: Record<Note["tone"], string> = {
  // The one object that is not white paper: accent-filled, ink on top.
  accent: "bg-accent border-[rgba(21,21,15,0.22)] text-ink",
  paper: "bg-paper-deep border-hair text-ink",
  ink: "bg-ink border-[rgba(21,21,15,0.5)] text-paper",
};

const LABEL_CLASS: Record<Note["tone"], string> = {
  accent: "text-[rgba(21,21,15,0.62)]",
  paper: "text-ink-faint",
  ink: "text-[rgba(251,250,246,0.6)]",
};

export function NoteObject({
  note,
  selected,
  dragging,
  carriedBy,
  editedBy,
  optimistic,
  autoEdit,
  zoom,
  onPointerDown,
  onResizePointerDown,
  onCommitText,
  onEditingChange,
}: NoteObjectProps) {
  const [editing, setEditing] = useState(false);
  const [draftTitle, setDraftTitle] = useState(note.title);
  const [draftBody, setDraftBody] = useState(note.body);
  const [syncedTitle, setSyncedTitle] = useState(note.title);
  const [syncedBody, setSyncedBody] = useState(note.body);
  const [baseVersion, setBaseVersion] = useState(note.version);
  const [autoEditConsumed, setAutoEditConsumed] = useState(false);
  const titleRef = useRef<HTMLTextAreaElement>(null);
  // Scopes the "did focus leave the editor" check on blur to just these two
  // fields, so tabbing between title and body doesn't look like leaving.
  const editorRef = useRef<HTMLDivElement>(null);

  // Follow remote text changes while the editor is CLOSED. While it is open the
  // drafts are deliberately left alone: destroying someone's half-typed
  // sentence to show them a collaborator's is worse than letting the conflict
  // surface on save, where the version check will catch it.
  if (!editing && (note.title !== syncedTitle || note.body !== syncedBody)) {
    setSyncedTitle(note.title);
    setSyncedBody(note.body);
    setDraftTitle(note.title);
    setDraftBody(note.body);
  }

  // Creation is one gesture: place the note and start typing.
  //
  // Adjusted during render rather than in an effect, so the note's first paint
  // already has its editor open - an effect would show it closed for a frame.
  // The local flag consumes it exactly once per mounted note.
  if (autoEdit && !autoEditConsumed) {
    setAutoEditConsumed(true);
    setBaseVersion(note.version);
    setDraftTitle(note.title);
    setDraftBody(note.body);
    setEditing(true);
  }

  useEffect(() => {
    if (!editing) return;
    const node = titleRef.current;
    node?.focus();
    node?.setSelectionRange(node.value.length, node.value.length);
  }, [editing]);

  // Tell collaborators this note is open, and clear it on the way out —
  // including on unmount, so closing a tab mid-edit leaves no stale hint.
  useEffect(() => {
    if (!editing) return;
    onEditingChange(note.id);
    return () => onEditingChange(null);
  }, [editing, note.id, onEditingChange]);

  function beginEditing() {
    // Capture the version the edit is BASED ON. See useBoard.updateNote.
    setBaseVersion(note.version);
    setDraftTitle(note.title);
    setDraftBody(note.body);
    setEditing(true);
  }

  function cancel() {
    setDraftTitle(note.title);
    setDraftBody(note.body);
    setEditing(false);
  }

  function commit() {
    setEditing(false);
    const nextTitle = draftTitle.trim();
    const nextBody = draftBody.trim();
    if (nextTitle !== note.title || nextBody !== note.body) {
      onCommitText(note, { title: nextTitle, body: nextBody }, baseVersion);
    }
  }

  /** Save only when focus leaves BOTH fields, not when it moves between them. */
  function handleFieldBlur(event: React.FocusEvent) {
    const next = event.relatedTarget as Node | null;
    if (next && editorRef.current?.contains(next)) return;
    commit();
  }

  const heldByAnyone = dragging || carriedBy !== null;

  return (
    <div
      data-note-id={note.id}
      data-testid="note"
      className="absolute"
      style={{
        left: note.x,
        top: note.y,
        width: note.width,
        height: note.height,
        // Held notes float above the stack while the gesture lasts, then drop
        // back to their real z. Purely visual; the durable order is z_index.
        zIndex: heldByAnyone ? 10_000 : note.z_index,
      }}
      onPointerDown={(event) => {
        if (editing) return;
        onPointerDown(event, note);
      }}
      onDoubleClick={(event) => {
        event.stopPropagation();
        beginEditing();
      }}
    >
      <div
        className={cn(
          "relative flex h-full w-full flex-col border p-3",
          "rounded-[var(--radius)] shadow-[var(--shadow-card)]",
          "transition-[box-shadow,border-color,opacity] duration-[var(--duration-base)]",
          "ease-[var(--ease-soft)]",
          TONE_CLASS[note.tone],
          !editing && "cursor-grab",
          heldByAnyone && "shadow-[var(--shadow-held)]",
          dragging && "cursor-grabbing",
          optimistic && "opacity-60",
        )}
        style={{
          // The sheet's own resting tilt, plus a touch more while carried —
          // the thing that makes it read as a physical object being lifted.
          transform: `rotate(${note.rotation + (heldByAnyone ? 1.1 : 0)}deg)`,
          transformOrigin: "50% 50%",
        }}
      >
        {/* Selection ring lives outside the sheet's rotation so it never
            appears crooked, and is counter-scaled so it stays a hairline at
            every zoom. */}
        {selected ? (
          <span
            aria-hidden="true"
            className="pointer-events-none absolute -inset-[3px] rounded-[var(--radius-lg)] border-ink"
            style={{ borderWidth: Math.max(1, 1.5 / zoom) }}
          />
        ) : null}

        {/* The pin. Sits proud of the sheet's top edge, and rotates with it,
            so the note reads as something pushed onto a board rather than a
            rectangle drawn on one. */}
        <span
          aria-hidden="true"
          className={cn(
            "absolute -top-[5px] right-[13px] h-[10px] w-[10px] rounded-full",
            "shadow-[0_2px_4px_rgba(21,21,15,0.3)]",
            note.tone === "ink" ? "bg-paper-up" : "bg-ink",
          )}
        />

        <header className="mb-2 flex flex-none items-center justify-between gap-2">
          <span className={cn("micro-label truncate", LABEL_CLASS[note.tone])}>
            {carriedBy ?? editedBy ?? note.author_name ?? "unsigned"}
          </span>
          {/* The grab affordance: three dots that spread when the sheet is
              touched. The reason anyone believes it moves before trying. */}
          <span className="group/handle flex flex-none gap-[2px]" aria-hidden="true">
            {[0, 1, 2].map((i) => (
              <i
                key={i}
                className={cn(
                  "h-[3px] w-[3px] rounded-full transition-transform",
                  "duration-[var(--duration-slow)] ease-[var(--ease-rise)]",
                  note.tone === "ink" ? "bg-[rgba(251,250,246,0.5)]" : "bg-[rgba(21,21,15,0.45)]",
                  heldByAnyone && i === 0 && "-translate-x-[2px]",
                  heldByAnyone && i === 2 && "translate-x-[2px]",
                )}
              />
            ))}
          </span>
        </header>

        {editing ? (
          <div ref={editorRef} className="flex min-h-0 flex-1 flex-col gap-1.5">
            <textarea
              ref={titleRef}
              value={draftTitle}
              rows={2}
              maxLength={500}
              aria-label="Note text"
              onChange={(event) => setDraftTitle(event.target.value)}
              onBlur={handleFieldBlur}
              onPointerDown={(event) => event.stopPropagation()}
              onKeyDown={(event) => {
                event.stopPropagation();
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  commit();
                }
                if (event.key === "Escape") cancel();
              }}
              className={cn(
                "shrink-0 resize-none border-0 bg-transparent p-0",
                "text-[0.94rem] leading-[1.28] tracking-[-0.012em] outline-none",
                "placeholder:opacity-45",
              )}
              placeholder="Type a thought…"
            />
            <textarea
              value={draftBody}
              maxLength={4000}
              aria-label="Note detail"
              onChange={(event) => setDraftBody(event.target.value)}
              onBlur={handleFieldBlur}
              onPointerDown={(event) => event.stopPropagation()}
              onKeyDown={(event) => {
                // Enter here inserts a newline (the textarea's default) rather
                // than saving — only the title field commits on Enter.
                event.stopPropagation();
                if (event.key === "Escape") cancel();
              }}
              className={cn(
                "min-h-0 flex-1 resize-none border-0 bg-transparent p-0",
                "text-[0.8rem] leading-[1.35] tracking-[-0.008em] outline-none opacity-80",
                "placeholder:opacity-45",
              )}
              placeholder="Add detail…"
            />
          </div>
        ) : (
          <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-hidden">
            <p
              className={cn(
                "shrink-0 overflow-hidden text-[0.94rem] leading-[1.28]",
                "tracking-[-0.012em] whitespace-pre-wrap",
                note.title.length === 0 && "opacity-45",
              )}
              style={{ textWrap: "pretty" }}
            >
              {note.title.length > 0 ? note.title : "Double-click to write"}
            </p>
            {note.body.length > 0 ? (
              <p
                className={cn(
                  "min-h-0 flex-1 overflow-hidden text-[0.8rem] leading-[1.35]",
                  "tracking-[-0.008em] whitespace-pre-wrap opacity-70",
                )}
                style={{ textWrap: "pretty" }}
              >
                {note.body}
              </p>
            ) : null}
          </div>
        )}

        {/* Resize handle, bottom-right. Counter-scaled so it stays a
            consistent physical size however far you are zoomed out. */}
        {selected && !editing ? (
          <span
            role="presentation"
            onPointerDown={(event) => {
              event.stopPropagation();
              onResizePointerDown(event, note);
            }}
            className="absolute -right-1 -bottom-1 cursor-nwse-resize rounded-[1px] border border-ink bg-paper-up"
            style={{
              width: Math.max(8, 10 / zoom),
              height: Math.max(8, 10 / zoom),
            }}
          />
        ) : null}
      </div>
    </div>
  );
}
