"use client";

/**
 * Every pen mark on the board — committed strokes, this client's own
 * in-progress one, and everybody else's in-progress ones — drawn as small
 * SVGs, one pair of (positioned wrapper, svg) per STROKE.
 *
 * A stroke's `points` are already WORLD coordinates (see lib/types.ts), and
 * each wrapper is positioned in that same space inside CanvasStage's
 * world-transform div, exactly like NoteObject's outer div, so pan and zoom
 * move the whole layer rather than any individual point.
 *
 * ONE ELEMENT PER STROKE, NOT PER POINT — a stroke's points still collapse
 * into a single <path>. That is the constraint this design satisfies; it is
 * not "one <svg> for the whole board", which turned out to be unworkable for
 * the reason below.
 *
 * ---- Why per-stroke wrappers, not one shared <svg> ----
 *
 * An earlier version of this file put every stroke in ONE unpositioned <svg>
 * and gave each <path> its own `z-index`, on the theory that an unpositioned
 * SVG root does not wall off its children from the surrounding stacking
 * context. That theory does not hold: CSS z-index only has an effect on
 * elements that are POSITIONED (or flex/grid items), and an SVG <path> is
 * never positioned — there is no `position` property that applies to it. In
 * every engine, `z-index` on a plain <path> is simply inert, SVG children
 * paint in document order among themselves, and the whole <svg> subtree
 * paints as one unit at wherever the <svg> itself sits in the parent
 * stacking context. Verified in Chromium against the running dev stack: a
 * stroke with `z_index: 2` drawn over a note with `z_index: 1` still lost to
 * it at `document.elementFromPoint`, because the entire strokes layer was
 * one un-positioned unit sitting below every explicitly z-indexed note.
 *
 * The fix is to make each stroke its own real participant in the world
 * layer's stacking context, the same way each NoteObject is: a `position:
 * absolute` wrapper `<div>` carrying `zIndex: stroke.z_index`, sized to that
 * one stroke's own point bounding box (padded by its stroke width), with a
 * small <svg> inside it drawing that one path in the wrapper's local
 * coordinates. Positioned divs genuinely interleave by z-index; an SVG
 * subtree does not.
 *
 * Sizing the wrapper to the stroke's bounding box rather than leaving it 1x1
 * with `overflow: visible` also makes hit-testing unambiguous: the clickable
 * <path> sits inside a box that actually covers it, instead of depending on
 * pointer events reaching content painted outside an SVG's own box, which is
 * not something to build a selection feature on without checking it first.
 *
 * Selection is a click on the STROKE itself, not the wrapper: each wrapper
 * and its svg carry `pointer-events: none`, and only the <path> gets it back
 * — and only in SELECT mode, since in PEN mode a pointer-down over an old
 * stroke must start drawing a new one, not pick the old one up.
 */

import type { RemoteStroke } from "@/lib/board/useBoard";
import type { Stroke, StrokePoint } from "@/lib/types";

/** Mid-gesture strokes float above the committed stack while they're live —
    the same visual lift NoteObject gives a held note (`zIndex: 10_000`),
    for the same reason: no flicker while the artist is still drawing. */
const EPHEMERAL_Z = 10_000;

export interface StrokeLayerProps {
  /** Committed strokes. Paint order does not matter here — z-index does. */
  strokes: Stroke[];
  remoteStrokes: Record<string, RemoteStroke>;
  /** This client's own in-progress stroke, rendered before it ever commits. */
  localStroke: { points: StrokePoint[]; color: string; width: number } | null;
  selectedStrokeId: string | null;
  tool: "select" | "pen";
  zoom: number;
  onStrokePointerDown: (event: React.PointerEvent, stroke: Stroke) => void;
}

interface Box {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** The stroke's own point bounding box, padded enough that its rendered
    line — which extends strokeWidth/2 past the centreline at every point —
    is never clipped by the wrapper it lives in. */
function boundingBox(points: StrokePoint[], strokeWidth: number): Box {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const [x, y] of points) {
    if (x < minX) minX = x;
    if (x > maxX) maxX = x;
    if (y < minY) minY = y;
    if (y > maxY) maxY = y;
  }
  const pad = strokeWidth / 2 + 4;
  return {
    left: minX - pad,
    top: minY - pad,
    width: Math.max(1, maxX - minX + pad * 2),
    height: Math.max(1, maxY - minY + pad * 2),
  };
}

/** Points translated into the wrapper's own local coordinates (its top-left
    corner is the path data's origin), rendered as one polyline command. */
function pathData(points: StrokePoint[], origin: { left: number; top: number }): string {
  if (points.length === 0) return "";
  const [[x0, y0], ...rest] = points;
  let d = `M${x0 - origin.left} ${y0 - origin.top}`;
  for (const [x, y] of rest) d += ` L${x - origin.left} ${y - origin.top}`;
  return d;
}

function StrokeMark({
  box,
  d,
  color,
  width,
  zIndex,
  haloD,
  haloWidth,
  interactive,
  dataStrokeId,
  onPointerDown,
}: {
  box: Box;
  d: string;
  color: string;
  width: number;
  zIndex: number;
  /** Present only for the selected committed stroke. */
  haloD?: string;
  haloWidth?: number;
  interactive: boolean;
  dataStrokeId?: string;
  onPointerDown?: (event: React.PointerEvent) => void;
}) {
  return (
    <div
      className="absolute top-0 left-0"
      style={{
        transform: `translate(${box.left}px, ${box.top}px)`,
        width: box.width,
        height: box.height,
        zIndex,
        pointerEvents: "none",
      }}
    >
      <svg width={box.width} height={box.height} style={{ overflow: "visible" }} aria-hidden="true">
        {haloD !== undefined ? (
          // Painted first so it sits just under the coloured path above it —
          // same element, same box, no separate z tier needed for that.
          <path
            d={haloD}
            fill="none"
            stroke="var(--color-ink)"
            strokeOpacity={0.2}
            strokeWidth={haloWidth}
            strokeLinecap="round"
            strokeLinejoin="round"
            style={{ pointerEvents: "none" }}
          />
        ) : null}
        <path
          data-stroke-id={dataStrokeId}
          data-testid={dataStrokeId !== undefined ? "stroke" : undefined}
          d={d}
          fill="none"
          stroke={color}
          strokeWidth={width}
          strokeLinecap="round"
          strokeLinejoin="round"
          style={{
            // "stroke" rather than "auto": hit-test the drawn line, not the
            // path's box, so clicking the gap inside a loop doesn't select it.
            pointerEvents: interactive ? "stroke" : "none",
            cursor: interactive ? "pointer" : undefined,
          }}
          onPointerDown={onPointerDown}
        />
      </svg>
    </div>
  );
}

export function StrokeLayer({
  strokes,
  remoteStrokes,
  localStroke,
  selectedStrokeId,
  tool,
  zoom,
  onStrokePointerDown,
}: StrokeLayerProps) {
  return (
    <>
      {strokes.map((stroke) => {
        const selected = stroke.id === selectedStrokeId;
        // Counter-scaled like the note's own selection ring, so the extra
        // halo width reads as a constant few screen pixels rather than
        // growing with the stroke at high zoom.
        const haloWidth = selected ? stroke.width + Math.max(4, 8 / zoom) : 0;
        const box = boundingBox(stroke.points, selected ? haloWidth : stroke.width);
        return (
          <StrokeMark
            key={stroke.id}
            box={box}
            d={pathData(stroke.points, box)}
            color={stroke.color}
            width={stroke.width}
            zIndex={stroke.z_index}
            haloD={selected ? pathData(stroke.points, box) : undefined}
            haloWidth={selected ? haloWidth : undefined}
            interactive={tool === "select"}
            dataStrokeId={stroke.id}
            onPointerDown={(event) => onStrokePointerDown(event, stroke)}
          />
        );
      })}

      {/* In-progress strokes are never interactive — a stroke isn't a real,
          selectable object until it has committed and gotten a server id. */}
      {localStroke && localStroke.points.length > 1
        ? (() => {
            const box = boundingBox(localStroke.points, localStroke.width);
            return (
              <StrokeMark
                box={box}
                d={pathData(localStroke.points, box)}
                color={localStroke.color}
                width={localStroke.width}
                zIndex={EPHEMERAL_Z}
                interactive={false}
              />
            );
          })()
        : null}
      {Object.values(remoteStrokes).map((remote) => {
        const box = boundingBox(remote.points, remote.width);
        return (
          <StrokeMark
            key={remote.strokeId}
            box={box}
            d={pathData(remote.points, box)}
            color={remote.color}
            width={remote.width}
            zIndex={EPHEMERAL_Z}
            interactive={false}
          />
        );
      })}
    </>
  );
}
