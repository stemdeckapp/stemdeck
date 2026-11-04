// Dragging a song onto a folder while the library list rebuilds (#979).
//
// render() empties the list and builds it again. A background import that
// finishes, or any other state change, rebuilds it. When that lands while a
// song is held over a folder, the row being dragged and the folder under the
// cursor are thrown away, and the browser delivers the drop to neither: the song
// stays where it was. Measured on the desktop shell (WebView2, through its
// DevTools port), 40 of 40 drops were lost when the rebuild came after the last
// dragover and before the release, and none of 40 when the pointer moved again
// after the rebuild. dragend fired every time.
//
// A folder dragged by its grip is the same list and the same rebuild, so it is
// held to the same rule.
//
// The queue is stubbed (see stubImportQueue): the e2e backend never separates.
// The finished job is the seeded fixture, so its stems are real files.
import { test, expect } from "@playwright/test";

import {
  JOB_ID,
  SIBLING_JOB_ID,
  SIBLING_TITLE,
  fixtureTrack,
  readCatalogState,
  seedCatalogState,
  stubExportEndpoints,
  stubImportQueue,
  stubUpdateCheck,
} from "./helpers.mjs";

test.use({ viewport: { width: 1600, height: 900 } });

// seed.py's MP3 song (stem-storage.spec.mjs).
const DRAGGED = "e2e0f00dface";
const MINE = '.folder[data-id="f-mine"] > .folder-head';
const row = (page, id) => page.locator(`.cat-item[data-id="${id}"]`).first();

// The MP3 song is the one dragged: its source is its own, so the import, which
// shares the sibling's source, folds into the sibling (#542) and never into it.
async function open(page) {
  await seedCatalogState(page, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: [DRAGGED, SIBLING_JOB_ID], color: null },
      { id: "f-mine", name: "Mine", items: [], color: null },
      { id: "f-other", name: "Other", items: [], color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks: {
      [DRAGGED]: { ...fixtureTrack(DRAGGED, "E2E MP3 Track"), sourceUrl: "local:e2e-mp3-fixture" },
      [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, SIBLING_TITLE),
    },
  });
  await page.route("**/api/jobs", (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ job_id: JOB_ID }) });
  });
  const queue = await stubImportQueue(page, JOB_ID, "Importing Song");
  await stubExportEndpoints(page);
  await stubUpdateCheck(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await row(page, SIBLING_JOB_ID).waitFor({ timeout: 20000 });
  await page.locator("#url").fill("https://www.youtube.com/watch?v=dQw4w9WgXcQ");
  await page.locator("#submit").click();
  await queue.run();
  await expect(row(page, JOB_ID)).toBeVisible();
  return queue;
}

const center = (box) => ({ x: box.x + box.width / 2, y: box.y + box.height / 2 });

/** Press on the song and carry it over the folder, without releasing. */
async function holdOverFolder(page) {
  const from = center(await row(page, DRAGGED).boundingBox());
  const to = center(await page.locator(MINE).boundingBox());
  await page.mouse.move(from.x, from.y);
  await page.mouse.down();
  await page.mouse.move(from.x + 8, from.y + 4, { steps: 3 });
  await page.mouse.move(to.x, to.y, { steps: 8 });
}

/** The import has finished and its track is in the store: completeSettledJob ran. */
async function importLanded(page) {
  await expect
    .poll(async () => (await readCatalogState(page))?.tracks?.[JOB_ID]?.status, { timeout: 15000 })
    .toBe("done");
}

test.describe("a song dragged onto a folder while the library rebuilds", () => {
  test("lands in the folder when an import finishes before the release", async ({ page }) => {
    const queue = await open(page);

    await holdOverFolder(page);
    await queue.settle();
    await importLanded(page);
    await page.mouse.up();

    await expect(page.locator(`.folder[data-id="f-mine"] .cat-item[data-id="${DRAGGED}"]`)).toHaveCount(1);
    await expect(page.locator(`.folder[data-id="f-unsorted"] .cat-item[data-id="${DRAGGED}"]`)).toHaveCount(0);
    const state = await readCatalogState(page);
    expect(state.folders.find((f) => f.id === "f-mine").items).toEqual([DRAGGED]);
    // The drag ended cleanly: nothing is left marked as dragged or as a target.
    await expect(page.locator(".cat-item.dragging")).toHaveCount(0);
    await expect(page.locator(".folder.drop-target")).toHaveCount(0);
  });

  test("shows the finished import once the drag ends", async ({ page }) => {
    const queue = await open(page);

    await holdOverFolder(page);
    await queue.settle();
    await importLanded(page);
    // Held back while the song is held: the import's row still reads as processing.
    await expect(row(page, JOB_ID).locator(".cat-status")).toHaveClass(/processing/);
    await page.mouse.up();

    // And run once the drag ends.
    await expect(row(page, JOB_ID).locator(".cat-status")).not.toHaveClass(/processing/);
    await expect(page.locator(".folder.drop-target")).toHaveCount(0);
  });

  test("a folder dragged by its grip still nests when an import finishes before the release", async ({ page }) => {
    const queue = await open(page);

    // Other's grip onto the middle of Mine's row nests Other inside Mine. The
    // finished import moves the sidebar up a few pixels, so the pointer starts
    // above the middle and stays in the nesting zone after the shift.
    await page.locator('.folder[data-id="f-other"] > .folder-head').hover();
    const gripEl = page.locator('.folder[data-id="f-other"] > .folder-head .f-grip');
    await expect(gripEl).toBeVisible();
    const grip = center(await gripEl.boundingBox());
    const mine = await page.locator(MINE).boundingBox();
    const to = { x: mine.x + mine.width / 2, y: mine.y + mine.height * 0.4 };
    await page.mouse.move(grip.x, grip.y);
    await page.mouse.down();
    await page.mouse.move(grip.x + 4, grip.y - 4, { steps: 3 });
    await page.mouse.move(to.x, to.y, { steps: 8 });
    await queue.settle();
    await importLanded(page);
    await page.mouse.up();

    await expect
      .poll(async () => (await readCatalogState(page)).folders.find((f) => f.id === "f-other")?.parentId ?? null)
      .toBe("f-mine");
    await expect(page.locator(".folder.folder-dragging")).toHaveCount(0);
  });

  test("a rebuild with no drag held still runs at once", async ({ page }) => {
    const queue = await open(page);
    await expect(row(page, JOB_ID).locator(".cat-status")).toHaveClass(/processing/);

    await queue.settle();
    await importLanded(page);

    await expect(row(page, JOB_ID).locator(".cat-status")).not.toHaveClass(/processing/);
  });
});
