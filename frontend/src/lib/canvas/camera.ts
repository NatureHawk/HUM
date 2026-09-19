/**
 * World coordinates, screen coordinates, and the camera between them.
 *
 * Almost every bug in a canvas comes from confusing the two spaces, so they
 * are kept apart here and nowhere else does the arithmetic.
 *
 * WORLD is the board's own coordinate system. A note at (1200, 640) is at that
 * point forever, for everybody, at every zoom level. This is what Postgres
 * stores and what events carry.
 *
 * SCREEN is pixels in this viewport right now. It depends entirely on where
 * this particular person happens to be looking.
 *
 *     screen = (world - camera) * zoom
 *     world  = screen / zoom + camera
 *
 * The camera is deliberately LOCAL. Alice reading the top-left of a board and
 * Bob zoomed into the bottom-right are both correct, and neither should move
 * the other's view. Persisting it would be the same category error as writing
 * presence to Postgres: it is viewport state, not board state.
 */

import type { Note } from "@/lib/types";

export interface Camera {
  /** World coordinate currently at the viewport's top-left corner. */
  x: number;
  y: number;
  zoom: number;
}

export const MIN_ZOOM = 0.2;
export const MAX_ZOOM = 2.5;

export const INITIAL_CAMERA: Camera = { x: 0, y: 0, zoom: 1 };

export interface Size {
  width: number;
  height: number;
}

export interface Point {
  x: number;
  y: number;
}

export function clampZoom(zoom: number): number {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom));
}

export function worldToScreen(world: Point, camera: Camera): Point {
  return {
    x: (world.x - camera.x) * camera.zoom,
    y: (world.y - camera.y) * camera.zoom,
  };
}

export function screenToWorld(screen: Point, camera: Camera): Point {
  return {
    x: screen.x / camera.zoom + camera.x,
    y: screen.y / camera.zoom + camera.y,
  };
}

/**
 * Zoom about a fixed screen point — the cursor, usually.
 *
 * Falls straight out of the two formulas above: note which world point is
 * under the pointer, change the zoom, then move the camera so that same world
 * point is under the pointer again. Without this the canvas appears to slide
 * away from you as you zoom, which is the single thing that makes a canvas
 * feel wrong.
 */
export function zoomAround(camera: Camera, screenPoint: Point, nextZoom: number): Camera {
  const zoom = clampZoom(nextZoom);
  const anchor = screenToWorld(screenPoint, camera);
  return {
    zoom,
    x: anchor.x - screenPoint.x / zoom,
    y: anchor.y - screenPoint.y / zoom,
  };
}

/** Pan by a screen-space delta — a drag of N pixels moves N pixels at any zoom. */
export function panByScreen(camera: Camera, dx: number, dy: number): Camera {
  return { ...camera, x: camera.x - dx / camera.zoom, y: camera.y - dy / camera.zoom };
}

/** The world rectangle currently visible. Used for placing new notes in view. */
export function visibleWorldRect(
  camera: Camera,
  viewport: Size,
): { x: number; y: number; width: number; height: number } {
  return {
    x: camera.x,
    y: camera.y,
    width: viewport.width / camera.zoom,
    height: viewport.height / camera.zoom,
  };
}

export function viewportCenterWorld(camera: Camera, viewport: Size): Point {
  return screenToWorld({ x: viewport.width / 2, y: viewport.height / 2 }, camera);
}

/**
 * A camera that frames every note, with breathing room.
 *
 * Returns the current camera unchanged for an empty board: there is nothing to
 * fit, and snapping to an arbitrary origin would feel like a glitch.
 */
export function fitToNotes(
  notes: Note[],
  viewport: Size,
  current: Camera,
  padding = 120,
): Camera {
  if (notes.length === 0 || viewport.width === 0 || viewport.height === 0) return current;

  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;

  for (const note of notes) {
    minX = Math.min(minX, note.x);
    minY = Math.min(minY, note.y);
    maxX = Math.max(maxX, note.x + note.width);
    maxY = Math.max(maxY, note.y + note.height);
  }

  const contentWidth = maxX - minX + padding * 2;
  const contentHeight = maxY - minY + padding * 2;
  const zoom = clampZoom(
    Math.min(viewport.width / contentWidth, viewport.height / contentHeight),
  );

  // Centre the content rather than aligning it to a corner.
  return {
    zoom,
    x: minX - padding - (viewport.width / zoom - contentWidth) / 2,
    y: minY - padding - (viewport.height / zoom - contentHeight) / 2,
  };
}

/** Paint order. z first, then id, so every client stacks an overlap identically. */
export function byStackingOrder(a: Note, b: Note): number {
  if (a.z_index !== b.z_index) return a.z_index - b.z_index;
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
}
