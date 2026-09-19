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
  ink: "bg-ink",
};

export function CanvasToolbar({
  zoom,
  tone,
  onToneChange,
  onCreate,
  onZoomIn,
  onZoomOut,
  onFit,
}: {
  zoom: number;
  tone: NoteTone;
  onToneChange: (tone: NoteTone) => void;
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
