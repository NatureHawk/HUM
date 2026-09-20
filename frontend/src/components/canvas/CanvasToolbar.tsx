"use client";

/**
 * A small floating control cluster, deliberately quiet.
 *
 * Only actions that are genuinely hard to discover otherwise: making a note,
 * changing zoom, framing the board. Everything else on this canvas is done by
 * touching the thing itself.
 */

import { cn } from "@/lib/cn";
import { TONES, type NoteTone } from "@/lib/types";

const TONE_SWATCH: Record<NoteTone, string> = {
  accent: "bg-accent",
  paper: "bg-paper-deep",
  peach: "bg-note-peach",
  sky: "bg-note-sky",
};

export type CanvasTool = "select" | "pen";

export function CanvasToolbar({
  zoom,
  tone,
  onToneChange,
  tool,
  onToolChange,
  onCreate,
  onZoomIn,
  onZoomOut,
  onFit,
}: {
  zoom: number;
  tone: NoteTone;
  onToneChange: (tone: NoteTone) => void;
  tool: CanvasTool;
  onToolChange: (tool: CanvasTool) => void;
  onCreate: () => void;
  onZoomIn: () => void;
  onZoomOut: () => void;
  onFit: () => void;
}) {
  return (
    <div
      className={cn(
        "pointer-events-auto flex items-center gap-1 rounded-[var(--radius-lg)] border border-hair",
        "bg-paper-up/92 p-1 shadow-[var(--shadow-nav)] backdrop-blur-[2px]",
      )}
    >
      <div className="flex items-center gap-1" role="group" aria-label="Tool">
        <ModeButton active={tool === "select"} label="Select tool" onClick={() => onToolChange("select")}>
          <span className="micro-label">Select</span>
        </ModeButton>
        <ModeButton active={tool === "pen"} label="Pen tool" onClick={() => onToolChange("pen")}>
          {/* Drawn rather than set: the mono face has no pen glyph. */}
          <svg width="13" height="13" viewBox="0 0 13 13" aria-hidden="true">
            <path
              d="M9.3 1.2l2.5 2.5-7.3 7.3-3.2 1 1-3.2z"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.3"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </ModeButton>
      </div>

      <span className="mx-1 h-5 w-px bg-hair" aria-hidden="true" />

      <button
        type="button"
        onClick={onCreate}
        className={cn(
          "flex items-center gap-2 rounded-[var(--radius)] bg-ink px-3 py-2 text-paper",
          "transition-[transform,background-color] duration-[var(--duration-fast)]",
          "ease-[var(--ease-soft)] hover:bg-ink-2 active:scale-[0.98]",
        )}
        title="Add a note (N) — or double-click anywhere on the canvas"
      >
        <span className="text-[15px] leading-none">+</span>
        <span className="micro-label">Note</span>
      </button>

      <div className="mx-1 flex items-center gap-1" role="group" aria-label="Note colour">
        {TONES.map((value) => (
          <button
            key={value}
            type="button"
            onClick={() => onToneChange(value)}
            aria-label={`${value} notes`}
            aria-pressed={tone === value}
            title={`New notes: ${value}`}
            className={cn(
              "h-5 w-5 rounded-[var(--radius-xs)] border transition-transform",
              "duration-[var(--duration-fast)] ease-[var(--ease-rise)]",
              TONE_SWATCH[value],
              tone === value
                ? "scale-110 border-ink"
                : "border-hair hover:scale-105 hover:border-hair-strong",
            )}
          />
        ))}
      </div>

      <span className="mx-1 h-5 w-px bg-hair" aria-hidden="true" />

      <ToolbarButton label="Zoom out" onClick={onZoomOut}>
        &minus;
      </ToolbarButton>
      <span
        className="micro-label w-11 text-center text-ink-faint tabular-nums"
        aria-live="polite"
      >
        {Math.round(zoom * 100)}%
      </span>
      <ToolbarButton label="Zoom in" onClick={onZoomIn}>
        +
      </ToolbarButton>
      <ToolbarButton label="Fit to notes" onClick={onFit}>
        <span className="micro-label">Fit</span>
      </ToolbarButton>
    </div>
  );
}

/** SELECT / PEN — a toggle pair, not a click-once action, so the active one
    gets the same ink-filled treatment as the permanent "+ Note" button rather
    than the quieter ToolbarButton hover state below. */
function ModeButton({
  active,
  label,
  onClick,
  children,
}: {
  active: boolean;
  label: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        "flex h-8 min-w-8 items-center justify-center gap-1.5 rounded-[var(--radius)] px-2.5",
        "transition-[background-color,color] duration-[var(--duration-fast)] ease-[var(--ease-soft)]",
        active ? "bg-ink text-paper" : "text-ink-soft hover:bg-paper-deep hover:text-ink",
      )}
    >
      {children}
    </button>
  );
}

function ToolbarButton({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      onClick={onClick}
      className={cn(
        "flex h-8 min-w-8 items-center justify-center rounded-[var(--radius)] px-2",
        "text-[14px] leading-none text-ink-soft transition-colors",
        "duration-[var(--duration-fast)] ease-[var(--ease-soft)]",
        "hover:bg-paper-deep hover:text-ink",
      )}
    >
      {children}
    </button>
  );
}
