// The Trash: hovering a row shows how to restore it, a click picks rows
// (Ctrl adds one, Shift takes a range), and holding the mouse button for a
// second restores the row, or the whole selection it belongs to. The Trash
// icon carries a count, so a full Trash is visible from anywhere (#749).
//
// The server's trash and restore endpoints are stubbed and recorded. The real
// ones would leave the shared fixture jobs trashed on the server, and the next
// spec's startup sync would move them out of its library.

import { test, expect } from "@playwright/test";

import {
  JOB_ID,
  SIBLING_JOB_ID,
  TRACK_TITLE,
  fixtureTrack,
  readCatalogState,
  seedCatalogState,
  stubUpdateCheck,
} from "./helpers.mjs";

const THIRD_ID = "e2e0feedface";
const TRASHED = [JOB_ID, SIBLING_JOB_ID, THIRD_ID];

async function seedFullTrash(page) {
  await seedCatalogState(page, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: [], color: null },
      { id: "trash", name: "Trash", items: [...TRASHED], color: null },
    ],
    tracks: {
      [JOB_ID]: fixtureTrack(JOB_ID, TRACK_TITLE),
      [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, "Second Trashed"),
      [THIRD_ID]: { ...fixtureTrack(THIRD_ID, "Third Trashed"), sourceUrl: "local:third.wav" },
    },
  });
  await stubUpdateCheck(page);
  const calls = [];
  await page.route(/\/api\/jobs\/[^/]+\/(trash|restore)$/, (route) => {
    const [, id, action] = route.request().url().match(/\/api\/jobs\/([^/]+)\/(trash|restore)$/);
    calls.push({ id, action });
    return route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
  });
  return calls;
}

async function openTrash(page) {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(".rail-trash").click();
  await expect(page.locator("#catalogPanel")).toHaveClass(/trash-view/);
  for (const id of TRASHED) await expect(trashRow(page, id)).toBeVisible();
}

const trashRow = (page, id) => page.locator(`#catalogList .cat-item.in-trash[data-id="${id}"]`);
const restoreCalls = (calls) => calls.filter((c) => c.action === "restore").map((c) => c.id).sort();

/** Press and hold on a row for `ms`, then let go. */
async function holdOn(page, id, ms) {
  const box = await trashRow(page, id).boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.waitForTimeout(ms);
  await page.mouse.up();
}

test.describe("the Trash icon", () => {
  test("shows how many songs are in the Trash", async ({ page }) => {
    await seedFullTrash(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    const badge = page.locator("#trashBadge");
    await expect(badge).toBeVisible();
    await expect(badge).toHaveText("3");
    await expect(page.locator(".rail-trash")).toHaveAttribute("aria-label", "Trash (3)");
  });

  test("shows no count when the Trash is empty", async ({ page }) => {
    await seedCatalogState(page, {
      folders: [
        { id: "f-unsorted", name: "Unsorted", items: [JOB_ID, SIBLING_JOB_ID], color: null },
        { id: "trash", name: "Trash", items: [], color: null },
      ],
      tracks: {
        [JOB_ID]: fixtureTrack(JOB_ID, TRACK_TITLE),
        [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, "Second"),
      },
    });
    await stubUpdateCheck(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await expect(page.locator(".cat-item").first()).toBeVisible();
    await expect(page.locator("#trashBadge")).toBeHidden();
    await expect(page.locator(".rail-trash")).toHaveAttribute("aria-label", "Trash");
  });
});

test.describe("a Trash row", () => {
  test("looks like a library row at rest, and shows the hint on hover", async ({ page }) => {
    await seedFullTrash(page);
    await openTrash(page);
    const row = trashRow(page, JOB_ID);
    const hint = row.locator(".trash-hint");
    const line = row.locator(".cat-sub:not(.trash-hint)");
    await page.mouse.move(0, 0);
    await expect(row.locator(".cat-meta")).toHaveCSS("filter", "none");
    await expect(line).toBeVisible();
    await expect(hint).toBeHidden();

    await row.hover();
    await expect(hint).toBeVisible();
    await expect(hint).toHaveText("Hold left click for 1 second to restore");
    await expect(line).toBeHidden();
  });

  test("a click selects it and does not open the song", async ({ page }) => {
    const calls = await seedFullTrash(page);
    await openTrash(page);
    await trashRow(page, JOB_ID).click();
    await expect(trashRow(page, JOB_ID)).toHaveClass(/selected/);
    await expect(trashRow(page, JOB_ID)).toHaveAttribute("aria-selected", "true");
    await expect(page.locator(".app")).toHaveClass(/no-track/);
    expect(restoreCalls(calls)).toEqual([]);
  });

  test("Ctrl+click picks several, and Ctrl+click again drops one", async ({ page }) => {
    await seedFullTrash(page);
    await openTrash(page);
    await trashRow(page, JOB_ID).click();
    await trashRow(page, THIRD_ID).click({ modifiers: ["Control"] });
    await expect(trashRow(page, JOB_ID)).toHaveClass(/selected/);
    await expect(trashRow(page, THIRD_ID)).toHaveClass(/selected/);
    await expect(trashRow(page, SIBLING_JOB_ID)).not.toHaveClass(/selected/);

    await trashRow(page, JOB_ID).click({ modifiers: ["Control"] });
    await expect(trashRow(page, JOB_ID)).not.toHaveClass(/selected/);
    await expect(trashRow(page, THIRD_ID)).toHaveClass(/selected/);
  });

  test("Shift+click picks the range from the last row picked", async ({ page }) => {
    await seedFullTrash(page);
    await openTrash(page);
    const order = await page.locator("#catalogList .cat-item.in-trash").evaluateAll((els) => els.map((e) => e.dataset.id));
    await trashRow(page, order[0]).click();
    await trashRow(page, order[2]).click({ modifiers: ["Shift"] });
    for (const id of order) await expect(trashRow(page, id)).toHaveClass(/selected/);

    // A plain click starts over with that row alone.
    await trashRow(page, order[1]).click();
    await expect(trashRow(page, order[1])).toHaveClass(/selected/);
    await expect(trashRow(page, order[0])).not.toHaveClass(/selected/);
    await expect(trashRow(page, order[2])).not.toHaveClass(/selected/);
  });

  test("let go before a second and nothing is restored", async ({ page }) => {
    const calls = await seedFullTrash(page);
    await openTrash(page);
    await holdOn(page, JOB_ID, 400);
    await page.waitForTimeout(900);
    await expect(trashRow(page, JOB_ID)).toBeVisible();
    expect(restoreCalls(calls)).toEqual([]);
  });

  test("held for a second, it leaves the Trash for the library", async ({ page }) => {
    const calls = await seedFullTrash(page);
    await openTrash(page);
    await holdOn(page, JOB_ID, 1300);
    await expect(trashRow(page, JOB_ID)).toHaveCount(0);
    await expect(trashRow(page, SIBLING_JOB_ID)).toBeVisible();
    await expect.poll(() => restoreCalls(calls)).toEqual([JOB_ID]);
    await expect(page.locator("#trashBadge")).toHaveText("2");

    const state = await readCatalogState(page);
    expect(state.folders.find((f) => f.id === "trash").items).not.toContain(JOB_ID);
    expect(state.folders.find((f) => f.id === "f-unsorted").items).toContain(JOB_ID);
  });

  test("held on a selected row, the whole selection is restored", async ({ page }) => {
    const calls = await seedFullTrash(page);
    await openTrash(page);
    await trashRow(page, JOB_ID).click();
    await trashRow(page, THIRD_ID).click({ modifiers: ["Control"] });
    await holdOn(page, THIRD_ID, 1300);
    await expect(trashRow(page, JOB_ID)).toHaveCount(0);
    await expect(trashRow(page, THIRD_ID)).toHaveCount(0);
    await expect(trashRow(page, SIBLING_JOB_ID)).toBeVisible();
    await expect.poll(() => restoreCalls(calls)).toEqual([JOB_ID, THIRD_ID].sort());
    await expect(page.locator("#trashBadge")).toHaveText("1");
  });

  test("held on a selected row, every selected row shows the fill, and letting go clears it", async ({ page }) => {
    await seedFullTrash(page);
    await openTrash(page);
    await trashRow(page, JOB_ID).click();
    await trashRow(page, THIRD_ID).click({ modifiers: ["Control"] });
    const box = await trashRow(page, THIRD_ID).boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await expect(trashRow(page, JOB_ID)).toHaveClass(/holding/);
    await expect(trashRow(page, THIRD_ID)).toHaveClass(/holding/);
    await expect(trashRow(page, SIBLING_JOB_ID)).not.toHaveClass(/holding/);
    await page.mouse.up();
    await expect(trashRow(page, JOB_ID)).not.toHaveClass(/holding/);
    await expect(trashRow(page, THIRD_ID)).not.toHaveClass(/holding/);
  });

  test("held on a row outside the selection, only that row shows the fill", async ({ page }) => {
    await seedFullTrash(page);
    await openTrash(page);
    await trashRow(page, JOB_ID).click();
    await trashRow(page, THIRD_ID).click({ modifiers: ["Control"] });
    const box = await trashRow(page, SIBLING_JOB_ID).boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await expect(trashRow(page, SIBLING_JOB_ID)).toHaveClass(/holding/);
    await expect(trashRow(page, JOB_ID)).not.toHaveClass(/holding/);
    await expect(trashRow(page, THIRD_ID)).not.toHaveClass(/holding/);
    await page.mouse.up();
  });

  test("held on a row outside the selection, only that row is restored", async ({ page }) => {
    const calls = await seedFullTrash(page);
    await openTrash(page);
    await trashRow(page, JOB_ID).click();
    await trashRow(page, THIRD_ID).click({ modifiers: ["Control"] });
    await holdOn(page, SIBLING_JOB_ID, 1300);
    await expect(trashRow(page, SIBLING_JOB_ID)).toHaveCount(0);
    await expect(trashRow(page, JOB_ID)).toBeVisible();
    await expect(trashRow(page, THIRD_ID)).toBeVisible();
    await expect.poll(() => restoreCalls(calls)).toEqual([SIBLING_JOB_ID]);
  });

  test("holding Enter on a focused row restores it too", async ({ page }) => {
    const calls = await seedFullTrash(page);
    await openTrash(page);
    await trashRow(page, JOB_ID).focus();
    await page.keyboard.down("Enter");
    await page.waitForTimeout(1300);
    await page.keyboard.up("Enter");
    await expect(trashRow(page, JOB_ID)).toHaveCount(0);
    await expect.poll(() => restoreCalls(calls)).toEqual([JOB_ID]);
  });
});
