// The Unsorted row in Stem Collections is a system folder: every new import
// lands in it, it cannot be deleted, and it has nothing to reorder. It used to
// carry a drag grip ("Drag to reorder") and its own New subfolder button, both
// revealed by a hover highlight, while the Stem Collections header right above
// already has New Folder (#695).
//
// A folder the user made keeps all of that: its grip still reorders and nests,
// and its New subfolder still works.
import { test, expect } from "@playwright/test";
import {
  JOB_ID,
  SIBLING_JOB_ID,
  TRACK_TITLE,
  SIBLING_TITLE,
  fixtureTrack,
  seedCatalogState,
  stubExportEndpoints,
  stubUpdateCheck,
} from "./helpers.mjs";

const UNSORTED = '.folder[data-id="f-unsorted"] > .folder-head';
const MINE = '.folder[data-id="f-mine"] > .folder-head';

async function open(page) {
  // Zero the motion tokens so a hover background is read after, not during,
  // its transition.
  await page.emulateMedia({ reducedMotion: "reduce" });
  await seedCatalogState(page, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: [JOB_ID], color: null },
      { id: "f-mine", name: "Mine", items: [SIBLING_JOB_ID], color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks: {
      [JOB_ID]: fixtureTrack(JOB_ID, TRACK_TITLE),
      [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, SIBLING_TITLE),
    },
  });
  await stubExportEndpoints(page);
  await stubUpdateCheck(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(UNSORTED).waitFor({ timeout: 20000 });
}

const background = (page, sel) =>
  page.locator(sel).evaluate((el) => getComputedStyle(el).backgroundColor);

test.describe("Unsorted folder row", () => {
  test("has no grip, no New subfolder and no delete", async ({ page }) => {
    await open(page);
    const head = page.locator(UNSORTED);
    await head.hover();
    await expect(head.locator(".f-grip")).toHaveCount(0);
    await expect(head.locator(".f-subfolder")).toHaveCount(0);
    await expect(head.locator(".f-del")).toHaveCount(0);
    // The one New Folder button is on the section header.
    await expect(page.locator("#newFolderBtn")).toHaveCount(1);
  });

  test("does not light up on hover", async ({ page }) => {
    await open(page);
    const before = await background(page, UNSORTED);
    await page.locator(UNSORTED).hover();
    expect(await background(page, UNSORTED)).toBe(before);
  });

  test("a user folder keeps its grip, New subfolder, delete and hover", async ({ page }) => {
    await open(page);
    const head = page.locator(MINE);
    const before = await background(page, MINE);
    await head.hover();
    await expect(head.locator(".f-grip")).toBeVisible();
    await expect(head.locator(".f-grip")).toHaveAttribute("draggable", "true");
    await expect(head.locator(".f-subfolder")).toHaveCount(1);
    await expect(head.locator(".f-del")).toHaveCount(1);
    expect(await background(page, MINE)).not.toBe(before);
  });
});
