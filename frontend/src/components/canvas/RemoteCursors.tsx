"use client";

/**
 * Other people's cursors.
 *
 * Positioned in WORLD coordinates inside the transformed layer, so a cursor
 * points at the same *place on the board* for everyone regardless of where
 * each person's camera happens to be. Sending screen coordinates instead is
 * the classic version of this bug: the cursor lands somewhere different for
 * every viewer.
 *
 * The pointer glyph and label are counter-scaled by the zoom so they stay a
 * constant physical size — a cursor that grows as you zoom in stops reading as
 * a cursor.
 */

import type { RemoteCursor } from "@/lib/board/useBoard";
import { hueForActor } from "@/lib/session";

export function RemoteCursors({
  cursors,
  selfActorId,
  zoom,
}: {
  cursors: RemoteCursor[];
  selfActorId: string;
  zoom: number;
}) {
  const others = cursors.filter((cursor) => cursor.actor.id !== selfActorId);
  if (others.length === 0) return null;

  return (
    <>
      {others.map((cursor) => {
        const hue = hueForActor(cursor.actor.id);
        return (
          <div
            key={cursor.connectionId}
            aria-hidden="true"
            className="pointer-events-none absolute top-0 left-0 z-[20000]"
            style={{
              transform: `translate(${cursor.x}px, ${cursor.y}px) scale(${1 / zoom})`,
              transformOrigin: "0 0",
              // Short, linear: cursors arrive ~30/s, so the tween only has to
              // bridge one frame. Anything longer reads as lag.
              transition: "transform 90ms linear",
            }}
          >
            <svg width="16" height="20" viewBox="0 0 16 20" fill="none">
              <path
                d="M1 1l11.5 10.5-5.1.4-2.7 4.9L1 1z"
                fill={`hsl(${hue} 55% 42%)`}
                stroke="var(--color-paper-up)"
                strokeWidth="1.2"
                strokeLinejoin="round"
              />
            </svg>
            <span
              className="micro-label ml-3 inline-block rounded-[var(--radius-xs)] px-1.5 py-[3px] whitespace-nowrap"
              style={{
                backgroundColor: `hsl(${hue} 55% 42%)`,
                color: "var(--color-paper-up)",
              }}
            >
              {cursor.actor.name}
            </span>
          </div>
        );
      })}
    </>
  );
}
