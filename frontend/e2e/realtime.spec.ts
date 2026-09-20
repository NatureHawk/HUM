/**
 * The two-browser canvas test.
 *
 * Two genuinely separate browser contexts, each with its own identity, its own
 * camera and its own WebSocket, on the same board. Every assertion is about one
 * browser seeing something the *other* browser did, with no reload anywhere.
 *
 * Nothing is mocked: this runs against the real Next app, the real FastAPI
 * server, real Postgres and real Redis.
 *
 * Positions are asserted in WORLD coordinates — read off the note element's
 * inline `left`/`top`, which are world units — rather than screen pixels. That
 * is deliberate: the two browsers have different cameras, so screen positions
 * legitimately differ while the world position must not.
 */

import { expect, test, type BrowserContext, type Page } from "@playwright/test";

const API = process.env.E2E_API_BASE_URL ?? "http://localhost:8001/api";
// Same origin as API, without the `/api` suffix - the one other place the
// origin is needed (inside `page.evaluate`, which runs in the browser and
// cannot close over Node-side config).
const API_ORIGIN = API.replace(/\/api$/, "");

/**
 * Open a board as a fresh guest, then rename that guest deterministically.
 *
 * Identity is now server-issued: each BrowserContext gets its own signed
 * `sb_session` cookie from `POST /api/session` (the app calls this on load),
 * with an auto-generated name like "Amber Lynx". To keep assertions on
 * "Alice"/"Bob" meaningful, rename the guest right after load.
 *
 * Renaming is done by calling `PATCH /api/session` from inside the page (so
 * the browser's own cookie jar carries it) rather than by driving the
 * PresenceRail's rename input through click/type/Enter. Both work, but the
 * API call is deterministic - no race with React re-rendering the input
 * before it receives focus - so it was chosen over the UI route.
 */
async function openAs(
  context: BrowserContext,
  name: string,
  boardId: string,
): Promise<Page> {
  const page = await context.newPage();
  await page.goto(`/boards/${boardId}`);
  // "live" only appears once the socket is open and the sync frame applied,
  // which also means the initial POST /api/session handshake has completed.
  await expect(page.getByText(/live ·/)).toBeVisible();

  await page.evaluate(
    async ([origin, newName]) => {
      await fetch(`${origin}/api/session`, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: newName }),
      });
    },
    [API_ORIGIN, name] as [string, string],
  );

  // Reload so the app's session hook re-establishes with the renamed actor
  // and the socket reconnects under the new name - then confirm both the
  // connection and the rename actually landed before handing the page back.
  await page.reload();
  await expect(page.getByText(/live ·/)).toBeVisible();
  await expect(page.getByRole("button", { name: new RegExp(name) })).toBeVisible();

  return page;
}

const note = (page: Page, text: string) =>
  page.locator('[data-testid="note"]').filter({ hasText: text }).first();

const noteById = (page: Page, id: string) => page.locator(`[data-note-id="${id}"]`);

/** World position, straight off the element the canvas positions in world units. */
async function worldPosition(page: Page, id: string): Promise<{ x: number; y: number }> {
  return noteById(page, id).evaluate((el) => ({
    x: Number.parseFloat((el as HTMLElement).style.left),
    y: Number.parseFloat((el as HTMLElement).style.top),
  }));
}

/** Create a note by double-clicking the canvas at a screen point. */
async function createNoteAt(page: Page, sx: number, sy: number, text: string) {
  const canvas = page.getByTestId("canvas");
  await canvas.dblclick({ position: { x: sx, y: sy } });
  const editor = page.getByLabel("Note text");
  await expect(editor).toBeVisible();
  await editor.fill(text);
  await editor.press("Enter");
  await expect(note(page, text)).toBeVisible();
}

/** Drag a note by a screen delta, with enough intermediate moves to stream. */
async function dragNote(page: Page, id: string, dx: number, dy: number) {
  const box = await noteById(page, id).boundingBox();
  if (!box) throw new Error("note has no box");
  const startX = box.x + box.width / 2;
  const startY = box.y + box.height / 2;

  await page.mouse.move(startX, startY);
  await page.mouse.down();
  for (let step = 1; step <= 8; step += 1) {
    await page.mouse.move(startX + (dx * step) / 8, startY + (dy * step) / 8);
    await page.waitForTimeout(16);
  }
  await page.mouse.up();
}

test.describe("two browsers on one canvas", () => {
  let boardId: string;

  test.beforeEach(async ({ request }) => {
    const response = await request.post(`${API}/boards`, {
      data: { title: `E2E ${Date.now()}` },
    });
    expect(response.ok()).toBeTruthy();
    boardId = (await response.json()).id;
  });

  test.afterEach(async ({ request }) => {
    await request.delete(`${API}/boards/${boardId}`);
  });

  test("notes appear, move, change and vanish across both browsers", async ({
    browser,
    request,
  }) => {
    const contextA = await browser.newContext();
    const contextB = await browser.newContext();
    const A = await openAs(contextA, "Alice", boardId);
    const B = await openAs(contextB, "Bob", boardId);

    await expect(A.getByText(/live · 2 here/)).toBeVisible();

    // --- A creates -> B sees --------------------------------------------
    await createNoteAt(A, 400, 300, "Written by Alice");
    await expect(note(B, "Written by Alice")).toBeVisible();

    const snapshot = await (await request.get(`${API}/boards/${boardId}`)).json();
    const id: string = snapshot.notes[0].id;

    // --- A drags -> B sees it at the new WORLD position -------------------
    const before = await worldPosition(B, id);
    await dragNote(A, id, 260, 160);

    await expect
      .poll(async () => (await worldPosition(B, id)).x, { timeout: 15_000 })
      .toBeGreaterThan(before.x + 100);
    const after = await worldPosition(B, id);
    expect(after.y).toBeGreaterThan(before.y + 50);

    // Both browsers agree with the database.
    const persisted = (await (await request.get(`${API}/boards/${boardId}`)).json())
      .notes[0];
    expect(Math.round(persisted.x)).toBe(Math.round(after.x));
    expect(Math.round((await worldPosition(A, id)).x)).toBe(Math.round(after.x));

    // --- B edits the text -> A sees it -----------------------------------
    await noteById(B, id).dblclick();
    const editor = B.getByLabel("Note text");
    await editor.fill("Edited by Bob");
    await editor.press("Enter");
    await expect(note(A, "Edited by Bob")).toBeVisible();

    // The edit must not have moved the note back.
    expect(Math.round((await worldPosition(A, id)).x)).toBe(Math.round(after.x));

    // --- A resizes -> B sees the new size ---------------------------------
    await noteById(A, id).click();
    const sizeBefore = await noteById(B, id).evaluate(
      (el) => (el as HTMLElement).style.width,
    );
    const box = await noteById(A, id).boundingBox();
    if (!box) throw new Error("no box");
    await A.mouse.move(box.x + box.width, box.y + box.height);
    await A.mouse.down();
    await A.mouse.move(box.x + box.width + 120, box.y + box.height + 90, { steps: 6 });
    await A.mouse.up();

    await expect
      .poll(
        async () =>
          noteById(B, id).evaluate((el) => (el as HTMLElement).style.width),
        { timeout: 15_000 },
      )
      .not.toBe(sizeBefore);

    // --- B deletes -> A sees it go ---------------------------------------
    await noteById(B, id).click();
    await B.keyboard.press("Delete");
    await expect(noteById(A, id)).toHaveCount(0);

    await contextA.close();
    await contextB.close();
  });

  test("a drag streams to the other browser without writing its position", async ({
    browser,
    request,
  }) => {
    // The ephemeral tier: while a note is held, its position travels over the
    // socket so collaborators can watch, and the position itself is not
    // written until the pointer is released.
    //
    // Milestone 5 made grabbing raise the note to the front, which IS a real
    // mutation — but only when the note is not already on top, which the
    // server decides (see docs/canvas-polish.md §4). This board holds exactly
    // one note, so it is already topmost and the grab writes nothing at all.
    // The counting below is therefore the original one, and the property it
    // protects is unchanged: no write per streamed frame, one on release.
    const contextA = await browser.newContext();
    const contextB = await browser.newContext();
    const A = await openAs(contextA, "Alice", boardId);
    const B = await openAs(contextB, "Bob", boardId);

    await createNoteAt(A, 350, 260, "Carried");
    await expect(note(B, "Carried")).toBeVisible();

    const snapshot = await (await request.get(`${API}/boards/${boardId}`)).json();
    const id: string = snapshot.notes[0].id;
    const revisionBefore: number = snapshot.revision;
    const startWorld = await worldPosition(B, id);

    // Hold the note and move it, WITHOUT releasing.
    const box = await noteById(A, id).boundingBox();
    if (!box) throw new Error("no box");
    await A.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await A.mouse.down();
    for (let step = 1; step <= 10; step += 1) {
      await A.mouse.move(box.x + box.width / 2 + step * 24, box.y + box.height / 2);
      await A.waitForTimeout(24);
    }

    // B is already showing it somewhere else, from drag frames alone.
    await expect
      .poll(async () => (await worldPosition(B, id)).x, { timeout: 10_000 })
      .toBeGreaterThan(startWorld.x + 80);

    // Ten streamed frames, and the grab of an already-topmost note: nothing
    // written at all. The position the database holds is still where the note
    // was before anybody picked it up.
    const midFlight = await (await request.get(`${API}/boards/${boardId}`)).json();
    expect(midFlight.revision).toBe(revisionBefore);
    expect(Math.round(midFlight.notes[0].x)).toBe(Math.round(startWorld.x));

    // Release: exactly one write, and the revision moves once.
    await A.mouse.up();
    await expect
      .poll(
        async () => (await (await request.get(`${API}/boards/${boardId}`)).json()).revision,
        { timeout: 10_000 },
      )
      .toBe(revisionBefore + 1);
    const settled = await (await request.get(`${API}/boards/${boardId}`)).json();
    expect(Math.round(settled.notes[0].x)).toBeGreaterThan(Math.round(startWorld.x) + 80);

    await contextA.close();
    await contextB.close();
  });

  test("dragging a note does not invalidate the other person's open editor", async ({
    browser,
    request,
  }) => {
    // The field-aware concurrency rule, end to end: `version` guards content,
    // and position is not content.
    const contextA = await browser.newContext();
    const contextB = await browser.newContext();
    const A = await openAs(contextA, "Alice", boardId);
    const B = await openAs(contextB, "Bob", boardId);

    await createNoteAt(A, 380, 280, "Shared");
    await expect(note(B, "Shared")).toBeVisible();
    const id: string = (await (await request.get(`${API}/boards/${boardId}`)).json())
      .notes[0].id;

    // Bob opens the editor first — capturing version 1.
    await noteById(B, id).dblclick();
    await expect(B.getByLabel("Note text")).toBeVisible();

    // Alice drags it out from under him.
    await dragNote(A, id, 200, 120);
    await expect
      .poll(async () => (await (await request.get(`${API}/boards/${boardId}`)).json()).revision)
      .toBeGreaterThan(1);

    // Bob's save must still succeed: the move touched no field he edited.
    await B.getByLabel("Note text").fill("Bob finished typing");
    await B.getByLabel("Note text").press("Enter");

    await expect(note(A, "Bob finished typing")).toBeVisible();
    await expect(B.getByText(/someone else edited this note first/i)).toHaveCount(0);

    await contextA.close();
    await contextB.close();
  });

  test("simultaneous text edits still resolve deterministically", async ({
    browser,
    request,
  }) => {
    const contextA = await browser.newContext();
    const contextB = await browser.newContext();
    const A = await openAs(contextA, "Alice", boardId);
    const B = await openAs(contextB, "Bob", boardId);

    await createNoteAt(A, 360, 260, "Contested");
    await expect(note(B, "Contested")).toBeVisible();
    const id: string = (await (await request.get(`${API}/boards/${boardId}`)).json())
      .notes[0].id;

    // Both open the editor at the same version.
    await noteById(A, id).dblclick();
    await noteById(B, id).dblclick();
    await A.getByLabel("Note text").fill("Alice wins");
    await B.getByLabel("Note text").fill("Bob loses");

    await A.getByLabel("Note text").press("Enter");
    await expect(note(A, "Alice wins")).toBeVisible();

    // Bob's save is based on a stale version: rejected, and he is told.
    await B.getByLabel("Note text").press("Enter");
    await expect(B.getByText(/someone else edited this note first/i)).toBeVisible();

    // Then rebased onto the server's copy, so both agree without a reload.
    await expect(note(B, "Alice wins")).toBeVisible();

    await contextA.close();
    await contextB.close();
  });

  test("z-order is board state, so both browsers stack an overlap the same way", async ({
    browser,
    request,
  }) => {
    const contextA = await browser.newContext();
    const contextB = await browser.newContext();
    const A = await openAs(contextA, "Alice", boardId);
    const B = await openAs(contextB, "Bob", boardId);

    await createNoteAt(A, 300, 240, "Under");
    await createNoteAt(A, 380, 300, "Over");
    await expect(note(B, "Over")).toBeVisible();

    const notes = (await (await request.get(`${API}/boards/${boardId}`)).json()).notes;
    const under = notes.find((n: { title: string }) => n.title === "Under");
    const over = notes.find((n: { title: string }) => n.title === "Over");
    expect(over.z_index).toBeGreaterThan(under.z_index);

    const zOf = (page: Page, id: string) =>
      noteById(page, id).evaluate((el) => Number((el as HTMLElement).style.zIndex));

    // Raise the lower one in A; B must agree.
    await noteById(A, under.id).click();
    await A.keyboard.press("]");

    await expect
      .poll(async () => await zOf(B, under.id), { timeout: 15_000 })
      .toBeGreaterThan(await zOf(B, over.id));
    expect(await zOf(A, under.id)).toBe(await zOf(B, under.id));

    await contextA.close();
    await contextB.close();
  });

  test("the camera is personal: zooming one browser does not move the other", async ({
    browser,
    request,
  }) => {
    // Notes are shared; cameras are not. Persisting the camera would make two
    // people fight over where everyone is looking.
    const contextA = await browser.newContext();
    const contextB = await browser.newContext();
    const A = await openAs(contextA, "Alice", boardId);
    const B = await openAs(contextB, "Bob", boardId);

    await createNoteAt(A, 400, 300, "Anchor");
    await expect(note(B, "Anchor")).toBeVisible();
    const id: string = (await (await request.get(`${API}/boards/${boardId}`)).json())
      .notes[0].id;

    const zoomLabel = (page: Page) =>
      page.locator("text=/^\\d+%$/").first().innerText();

    const bScreenBefore = await noteById(B, id).boundingBox();
    const bWorldBefore = await worldPosition(B, id);
    const aZoomBefore = await zoomLabel(A);
    const bZoomBefore = await zoomLabel(B);

    await A.getByRole("button", { name: "Zoom in" }).click();
    await A.getByRole("button", { name: "Zoom in" }).click();

    // A's zoom really moved...
    await expect.poll(async () => Number.parseInt(await zoomLabel(A), 10)).toBeGreaterThan(
      Number.parseInt(aZoomBefore, 10),
    );

    // ...and B's zoom, screen position and world position are all untouched.
    expect(await zoomLabel(B)).toBe(bZoomBefore);
    const bScreenAfter = await noteById(B, id).boundingBox();
    expect(Math.round(bScreenAfter!.x)).toBe(Math.round(bScreenBefore!.x));
    expect(await worldPosition(B, id)).toEqual(bWorldBefore);

    await contextA.close();
    await contextB.close();
  });

  test("a client that misses revisions resyncs from canonical state", async ({
    browser,
    request,
  }) => {
    const contextA = await browser.newContext();
    const A = await openAs(contextA, "Alice", boardId);

    await createNoteAt(A, 300, 250, "Before the blackout");
    await expect(note(A, "Before the blackout")).toBeVisible();

    await A.context().setOffline(true);

    // The `request` fixture has its own cookie jar, separate from either
    // browser context, and note creation now requires a session (identity
    // rides the signed cookie, not an `actor` field in the body). Establish
    // one guest session for it before posting on its behalf.
    await request.post(`${API}/session`);

    // Three notes created behind its back: a multi-step gap, so this is real
    // recovery rather than an off-by-one a single event could mask.
    for (const title of ["Missed one", "Missed two", "Missed three"]) {
      const created = await request.post(`${API}/boards/${boardId}/notes`, {
        data: {
          x: 900,
          y: 700,
          title,
          op_id: crypto.randomUUID(),
        },
      });
      expect(created.ok()).toBeTruthy();
    }

    await A.context().setOffline(false);

    await expect(A.getByText(/live ·/)).toBeVisible({ timeout: 30_000 });
    for (const title of ["Missed one", "Missed two", "Missed three"]) {
      await expect(note(A, title)).toBeVisible({ timeout: 30_000 });
    }
    await expect(note(A, "Before the blackout")).toBeVisible();

    await contextA.close();
  });

  test("presence and cursors are real and disappear with the connection", async ({
    browser,
  }) => {
    const contextA = await browser.newContext();
    const A = await openAs(contextA, "Alice", boardId);
    await expect(A.getByText(/live · just you/)).toBeVisible();

    const contextB = await browser.newContext();
    const B = await openAs(contextB, "Bob", boardId);
    await expect(A.getByText(/live · 2 here/)).toBeVisible();
    await expect(
      A.getByRole("list", { name: "Collaborators on this board" }).getByTitle("Bob"),
    ).toBeVisible();

    // Bob moves his pointer across the canvas; Alice sees a labelled cursor.
    const canvas = B.getByTestId("canvas");
    const box = await canvas.boundingBox();
    if (!box) throw new Error("no canvas box");
    for (let step = 1; step <= 8; step += 1) {
      await B.mouse.move(box.x + 120 + step * 30, box.y + 160 + step * 12);
      await B.waitForTimeout(40);
    }
    await expect(A.getByText("Bob", { exact: true }).last()).toBeVisible({
      timeout: 15_000,
    });

    await B.close();
    await contextB.close();
    await expect(A.getByText(/live · just you/)).toBeVisible();

    await contextA.close();
  });

  test("a note's body is editable and syncs to the other browser", async ({ browser }) => {
    const contextA = await browser.newContext();
    const contextB = await browser.newContext();
    const A = await openAs(contextA, "Alice", boardId);
    const B = await openAs(contextB, "Bob", boardId);

    await createNoteAt(A, 340, 260, "Has detail");
    await expect(note(B, "Has detail")).toBeVisible();

    // Open the editor and fill the body field - a second field alongside the
    // title, saved together.
    await note(A, "Has detail").dblclick();
    const detail = A.getByLabel("Note detail");
    await expect(detail).toBeVisible();
    await detail.fill("The body text that should sync");

    // Save via the title field's Enter, exactly as the title-only tests do.
    await A.getByLabel("Note text").press("Enter");

    // B sees the body land on the note, with no reload.
    await expect(note(B, "The body text that should sync")).toBeVisible();

    await contextA.close();
    await contextB.close();
  });
});
