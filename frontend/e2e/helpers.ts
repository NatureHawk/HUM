/**
 * Shared helpers for the two-browser tests.
 *
 * Extracted from realtime.spec.ts when a second spec needed the same
 * "open a board as a named guest" dance. That spec still has its own copies —
 * they were not moved, only shared — so nothing about the Milestone 3 test
 * changed when this file appeared.
 */

import { expect, type BrowserContext, type Page } from "@playwright/test";

export const API = process.env.E2E_API_BASE_URL ?? "http://localhost:8001/api";
export const API_ORIGIN = API.replace(/\/api$/, "");

/**
 * Open a board as a fresh guest, then rename that guest deterministically.
 *
 * Identity is server-issued: each BrowserContext gets its own signed
 * `sb_session` cookie. The rename goes through `PATCH /api/session` from inside
 * the page rather than through the rename input, because the API call has no
 * race with React re-rendering the input before it takes focus.
 */
export async function openAs(
  context: BrowserContext,
  name: string,
  boardId: string,
): Promise<Page> {
  const page = await context.newPage();
  await page.goto(`/boards/${boardId}`);
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

  await page.reload();
  await expect(page.getByText(/live ·/)).toBeVisible();
  await expect(page.getByRole("button", { name: new RegExp(name) })).toBeVisible();

  return page;
}

export const note = (page: Page, text: string) =>
  page.locator('[data-testid="note"]').filter({ hasText: text }).first();

export const noteById = (page: Page, id: string) => page.locator(`[data-note-id="${id}"]`);

/** World position, straight off the element the canvas positions in world units. */
export async function worldPosition(
  page: Page,
  id: string,
): Promise<{ x: number; y: number }> {
  return noteById(page, id).evaluate((el) => ({
    x: Number.parseFloat((el as HTMLElement).style.left),
    y: Number.parseFloat((el as HTMLElement).style.top),
  }));
}

/** Create a note by double-clicking the canvas at a screen point. */
export async function createNoteAt(page: Page, sx: number, sy: number, text: string) {
  const canvas = page.getByTestId("canvas");
  await canvas.dblclick({ position: { x: sx, y: sy } });
  const editor = page.getByLabel("Note text");
  await expect(editor).toBeVisible();
  await editor.fill(text);
  await editor.press("Enter");
  await expect(note(page, text)).toBeVisible();
}
