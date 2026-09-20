/**
 * Milestone 5's two-browser test: colour, pen and stacking.
 *
 * The same discipline as realtime.spec.ts — two real browser contexts, two
 * identities, two cameras, one board, nothing mocked. Every assertion is about
 * one browser seeing what the *other* browser did, with no reload anywhere.
 *
 * Stroke geometry is asserted in WORLD coordinates (read off the `points` the
 * API stores and the `d` attribute the canvas renders) rather than screen
 * pixels, for the reason the note tests do the same: the two browsers have
 * different cameras, so their screen positions legitimately differ while the
 * world geometry must not.
 */

import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { API, createNoteAt, note, noteById, openAs } from "./helpers";

const strokeById = (page: Page, id: string) => page.locator(`[data-stroke-id="${id}"]`);
const strokes = (page: Page) => page.locator('[data-testid="stroke"]');

/** The durable z of a note, as this browser currently has it. */
const zOfNote = (page: Page, id: string) =>
  noteById(page, id).evaluate((el) => Number((el as HTMLElement).style.zIndex));

interface SnapshotNote {
  id: string;
  title: string;
  tone: string;
  x: number;
  y: number;
  width: number;
  height: number;
  z_index: number;
}
interface SnapshotStroke {
  id: string;
  points: [number, number][];
  z_index: number;
}
interface Snapshot {
  revision: number;
  notes: SnapshotNote[];
  strokes: SnapshotStroke[];
}

async function snapshot(request: APIRequestContext, boardId: string): Promise<Snapshot> {
  return (await request.get(`${API}/boards/${boardId}`)).json() as Promise<Snapshot>;
}

/** Fail loudly rather than dereferencing undefined three lines later. */
function byTitle(notes: SnapshotNote[], title: string): SnapshotNote {
  const found = notes.find((n) => n.title === title);
  if (!found) throw new Error(`no note titled "${title}" in the snapshot`);
  return found;
}

/** Draw with the pen across the canvas, with enough steps to stream frames. */
async function drawStroke(page: Page, from: { x: number; y: number }, to: { x: number; y: number }) {
  await page.mouse.move(from.x, from.y);
  await page.mouse.down();
  for (let step = 1; step <= 10; step += 1) {
    await page.mouse.move(
      from.x + ((to.x - from.x) * step) / 10,
      from.y + ((to.y - from.y) * step) / 10,
    );
    await page.waitForTimeout(20);
  }
  await page.mouse.up();
}

test.describe("palette, pen and stacking across two browsers", () => {
  let boardId: string;

  test.beforeEach(async ({ request }) => {
    const response = await request.post(`${API}/boards`, {
      data: { title: `E2E polish ${Date.now()}` },
    });
    expect(response.ok()).toBeTruthy();
    boardId = (await response.json()).id;
  });

  test.afterEach(async ({ request }) => {
    await request.delete(`${API}/boards/${boardId}`);
  });

  test("A changes a note's colour and B sees it, as a tone-only update", async ({
    browser,
    request,
  }) => {
    const contextA = await browser.newContext();
    const contextB = await browser.newContext();
    const A = await openAs(contextA, "Alice", boardId);
    const B = await openAs(contextB, "Bob", boardId);

    await createNoteAt(A, 400, 300, "Recoloured");
    await expect(note(B, "Recoloured")).toBeVisible();

    const before = (await snapshot(request, boardId)).notes[0];
    const id: string = before.id;

    // Select it, then pick a different preset from the note's own control.
    await noteById(A, id).click();
    await A.getByRole("button", { name: /colour: peach/i }).click();

    // B sees the new tone with no reload...
    await expect
      .poll(async () => (await snapshot(request, boardId)).notes[0].tone, { timeout: 15_000 })
      .toBe("peach");
    await expect
      .poll(async () =>
        noteById(B, id).evaluate((el) =>
          getComputedStyle(el.querySelector("div") ?? el).backgroundColor,
        ),
      )
      .not.toBe("rgba(0, 0, 0, 0)");

    // ...and nothing but the tone moved: a tone change is a note.updated
    // carrying one field, not a whole-note replacement.
    const after = (await snapshot(request, boardId)).notes[0];
    expect(after.tone).toBe("peach");
    expect(after.title).toBe(before.title);
    expect([after.x, after.y]).toEqual([before.x, before.y]);
    expect([after.width, after.height]).toEqual([before.width, before.height]);

    await contextA.close();
    await contextB.close();
  });

  test("A draws and B sees the stroke, committed as exactly one durable write", async ({
    browser,
    request,
  }) => {
    const contextA = await browser.newContext();
    const contextB = await browser.newContext();
    const A = await openAs(contextA, "Alice", boardId);
    const B = await openAs(contextB, "Bob", boardId);

    const revisionBefore: number = (await snapshot(request, boardId)).revision;

    await A.getByRole("button", { name: "Pen tool" }).click();
    const canvas = A.getByTestId("canvas");
    const box = await canvas.boundingBox();
    if (!box) throw new Error("no canvas box");
    await drawStroke(
      A,
      { x: box.x + 250, y: box.y + 250 },
      { x: box.x + 500, y: box.y + 380 },
    );

    // B sees it, having never reloaded.
    await expect(strokes(B)).toHaveCount(1, { timeout: 15_000 });

    // One completed stroke is ONE row and ONE revision - not one per point.
    const after = await snapshot(request, boardId);
    expect(after.strokes).toHaveLength(1);
    expect(after.revision).toBe(revisionBefore + 1);

    const stored = after.strokes[0];
    expect(stored.points.length).toBeGreaterThan(2);
    // World coordinates, not screen: B's camera differs from A's, and the
    // stored geometry belongs to neither of them.
    await expect(strokeById(B, stored.id)).toHaveCount(1);

    // Zooming B must not move the stroke in world space.
    const dBefore = await strokeById(B, stored.id).getAttribute("d");
    await B.getByRole("button", { name: "Zoom in" }).click();
    await B.getByRole("button", { name: "Zoom in" }).click();
    expect(await strokeById(B, stored.id).getAttribute("d")).toBe(dBefore);

    await contextA.close();
    await contextB.close();
  });

  test("the last object grabbed is the topmost, and both browsers agree", async ({
    browser,
    request,
  }) => {
    // The headline behaviour: grabbing raises. Not an explicit "bring to
    // front" command - the act of picking the thing up.
    const contextA = await browser.newContext();
    const contextB = await browser.newContext();
    const A = await openAs(contextA, "Alice", boardId);
    const B = await openAs(contextB, "Bob", boardId);

    await createNoteAt(A, 320, 260, "Note one");
    await createNoteAt(A, 400, 320, "Note two");
    await expect(note(B, "Note two")).toBeVisible();

    const notes = (await snapshot(request, boardId)).notes;
    const one = byTitle(notes, "Note one");
    const two = byTitle(notes, "Note two");

    // Freshly created, note two is on top.
    expect(two.z_index).toBeGreaterThan(one.z_index);

    // A grabs note one: it comes to the front, in BOTH browsers.
    await noteById(A, one.id).click();
    await expect
      .poll(async () => await zOfNote(B, one.id), { timeout: 15_000 })
      .toBeGreaterThan(await zOfNote(B, two.id));

    // A grabs note two: now that one is in front.
    await noteById(A, two.id).click();
    await expect
      .poll(async () => await zOfNote(B, two.id), { timeout: 15_000 })
      .toBeGreaterThan(await zOfNote(B, one.id));

    // Bob grabs note one — and both browsers converge on his order, because
    // the stack is board state resolved by the server, not render order.
    await noteById(B, one.id).click();
    await expect
      .poll(async () => await zOfNote(A, one.id), { timeout: 15_000 })
      .toBeGreaterThan(await zOfNote(A, two.id));

    expect(await zOfNote(A, one.id)).toBe(await zOfNote(B, one.id));
    expect(await zOfNote(A, two.id)).toBe(await zOfNote(B, two.id));

    await contextA.close();
    await contextB.close();
  });

  test("clicking an overlapping pile selects the visible top note", async ({
    browser,
    request,
  }) => {
    // Placed through the API at deliberately overlapping world coordinates,
    // rather than by double-clicking the canvas twice: the second
    // double-click would land ON the first note and open its editor instead
    // of making a second note, which is correct behaviour and useless here.
    await request.post(`${API}/session`);
    for (const [title, x, y] of [
      ["Underneath", 400, 300],
      ["On top", 460, 340],
    ] as const) {
      const created = await request.post(`${API}/boards/${boardId}/notes`, {
        data: { title, x, y, op_id: crypto.randomUUID() },
      });
      expect(created.ok()).toBeTruthy();
    }

    const contextA = await browser.newContext();
    const A = await openAs(contextA, "Alice", boardId);
    await expect(note(A, "On top")).toBeVisible();

    const notes = (await snapshot(request, boardId)).notes;
    const under = byTitle(notes, "Underneath");
    const over = byTitle(notes, "On top");
    expect(over.z_index).toBeGreaterThan(under.z_index);

    // Click where the two genuinely overlap. The browser's own hit-testing
    // has to hand that click to the note in front, not the one behind it.
    const overBox = await noteById(A, over.id).boundingBox();
    const underBox = await noteById(A, under.id).boundingBox();
    if (!overBox || !underBox) throw new Error("no box");
    const x = overBox.x + 6;
    const y = overBox.y + 6;
    expect(x).toBeLessThan(underBox.x + underBox.width);
    expect(y).toBeLessThan(underBox.y + underBox.height);
    await A.mouse.click(x, y);

    // The front note took the click, so it is the one that is selected - and
    // being already topmost, grabbing it changed nothing at all.
    await expect(A.getByRole("button", { name: /colour: peach/i })).toBeVisible();
    await expect
      .poll(async () => (await snapshot(request, boardId)).notes.at(-1)?.title)
      .toBe("On top");

    // The note behind, once grabbed, really does come forward.
    await A.mouse.click(underBox.x + 6, underBox.y + underBox.height - 6);
    await expect
      .poll(async () => (await snapshot(request, boardId)).notes.at(-1)?.title, {
        timeout: 15_000,
      })
      .toBe("Underneath");

    await contextA.close();
  });

  test("a stroke can be selected and deleted, and B sees it go", async ({
    browser,
    request,
  }) => {
    const contextA = await browser.newContext();
    const contextB = await browser.newContext();
    const A = await openAs(contextA, "Alice", boardId);
    const B = await openAs(contextB, "Bob", boardId);

    await A.getByRole("button", { name: "Pen tool" }).click();
    const box = await A.getByTestId("canvas").boundingBox();
    if (!box) throw new Error("no canvas box");
    await drawStroke(
      A,
      { x: box.x + 200, y: box.y + 200 },
      { x: box.x + 460, y: box.y + 200 },
    );

    await expect(strokes(B)).toHaveCount(1, { timeout: 15_000 });
    const stored = (await snapshot(request, boardId)).strokes[0];

    // Back to select, click the stroke itself, delete it.
    await A.getByRole("button", { name: "Select tool" }).click();
    await strokeById(A, stored.id).click({ force: true });
    await A.keyboard.press("Delete");

    await expect(strokes(B)).toHaveCount(0, { timeout: 15_000 });
    expect((await snapshot(request, boardId)).strokes).toHaveLength(0);

    await contextA.close();
    await contextB.close();
  });

  test("notes and strokes interleave on one z sequence, not as two flat layers", async ({
    browser,
    request,
  }) => {
    // Notes and strokes share ONE server-side stacking cursor, so "drawn
    // after" and "grabbed last" have to mean the same thing across both object
    // types. This is asserted through the browser's own hit-testing rather
    // than by reading z-index off the DOM, because what matters is which
    // object a person actually touches where the two overlap — and an
    // implementation can carry the right z attribute while painting in the
    // wrong order.
    const contextA = await browser.newContext();
    const A = await openAs(contextA, "Alice", boardId);

    await createNoteAt(A, 400, 300, "Beneath");
    await A.getByRole("button", { name: "Pen tool" }).click();
    const box = await A.getByTestId("canvas").boundingBox();
    if (!box) throw new Error("no canvas box");
    // Straight through the note.
    await drawStroke(
      A,
      { x: box.x + 300, y: box.y + 300 },
      { x: box.x + 520, y: box.y + 300 },
    );
    await expect(strokes(A)).toHaveCount(1, { timeout: 15_000 });

    const state = await snapshot(request, boardId);
    const drawn = state.strokes[0];
    const beneath = byTitle(state.notes, "Beneath");
    expect(drawn.z_index).toBeGreaterThan(beneath.z_index);

    // Back to SELECT before probing. Hit-testing is the honest way to ask
    // "what is on top here", but it only answers while strokes accept pointer
    // events at all — in PEN mode they deliberately do not, because drawing
    // across an old stroke must start a new one rather than select it.
    await A.getByRole("button", { name: "Select tool" }).click();

    /** What the browser hits where the stroke crosses the note. */
    const topmostWhereTheyOverlap = () =>
      A.evaluate(() => {
        const path = document.querySelector('[data-testid="stroke"]');
        const noteEl = document.querySelector('[data-testid="note"]');
        if (!path || !noteEl) return "missing";
        const pb = path.getBoundingClientRect();
        const nb = noteEl.getBoundingClientRect();
        const x = Math.max(pb.left, nb.left) + 6;
        const y = pb.top + pb.height / 2;
        if (x < nb.left || x > nb.right || y < nb.top || y > nb.bottom) return "no-overlap";
        const hit = document.elementFromPoint(x, y);
        return hit?.closest("[data-testid]")?.getAttribute("data-testid") ?? "neither";
      });

    // Drawn last, so the stroke is what you touch there.
    await expect.poll(topmostWhereTheyOverlap, { timeout: 15_000 }).toBe("stroke");

    // Now grab the note: it must come above the stroke, because grabbing
    // raises through the same one cursor the stroke was created from.
    await noteById(A, beneath.id).click({ position: { x: 20, y: 20 } });
    await expect
      .poll(async () => {
        const after = await snapshot(request, boardId);
        return byTitle(after.notes, "Beneath").z_index > after.strokes[0].z_index;
      })
      .toBe(true);
    await expect.poll(topmostWhereTheyOverlap, { timeout: 15_000 }).toBe("note");

    await contextA.close();
  });
});
