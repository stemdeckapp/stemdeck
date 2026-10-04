// An import never takes the studio while it runs (#747). With nothing open it
// used to put the song's name and "Extracting" on the Now Playing card; now the
// card stays empty, the progress lives on the library row and the queue, and the
// finished track opens by itself if the studio is still empty.
//
// The queue is stubbed (see stubImportQueue): the e2e backend never separates.
// The finished job is the seeded fixture, so its stems are real files.
import { test, expect } from "@playwright/test";

import {
  JOB_ID,
  SIBLING_JOB_ID,
  TRACK_TITLE,
  SIBLING_TITLE,
  fixtureTrack,
  seedCatalogState,
  stubExportEndpoints,
  stubImportQueue,
  stubUpdateCheck,
} from "./helpers.mjs";

test.use({ viewport: { width: 1600, height: 900 } });

// Only the sibling is in the store, so the import's own row is new and nothing
// is dedup-folded into it (#542).
async function seedSibling(page) {
  await seedCatalogState(page, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: [SIBLING_JOB_ID], color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks: { [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, SIBLING_TITLE) },
  });
}

async function startImport(page) {
  await page.route("**/api/jobs", (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ job_id: JOB_ID }) });
  });
  const queue = await stubImportQueue(page, JOB_ID, "Importing Song");
  await stubExportEndpoints(page);
  await stubUpdateCheck(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(".cat-item").first().waitFor({ timeout: 20000 });
  await page.locator("#url").fill("https://www.youtube.com/watch?v=dQw4w9WgXcQ");
  await page.locator("#submit").click();
  await queue.run();
  return queue;
}

test.describe("an import with the studio empty", () => {
  test("keeps the Now Playing card empty while it runs, then opens the track", async ({ page }) => {
    await seedSibling(page);
    const queue = await startImport(page);

    await expect(page.locator(".app")).toHaveClass(/no-track/);
    await expect(page.locator("#title")).toHaveText("Ready to import a track");
    const card = page.locator("#nowPlayingPanel");
    await expect(card).not.toContainText("Importing Song");
    await expect(card).not.toContainText(/extracting/i);
    // The progress is on the library row instead.
    await expect(page.locator(`.cat-item[data-id="${JOB_ID}"]`).first()).toBeVisible();

    await queue.settle();
    await expect(page.locator(".app")).not.toHaveClass(/no-track/, { timeout: 15000 });
    await expect(page.locator("#title")).toHaveText(TRACK_TITLE, { timeout: 15000 });
  });

  test("does not open the finished track over one the user opened meanwhile", async ({ page }) => {
    await seedSibling(page);
    const queue = await startImport(page);

    await page.locator(`.cat-item[data-id="${SIBLING_JOB_ID}"]`).first().click();
    await expect(page.locator("#title")).toHaveText(SIBLING_TITLE, { timeout: 15000 });

    await queue.settle();
    await page.waitForTimeout(1500);
    await expect(page.locator("#title")).toHaveText(SIBLING_TITLE);
  });

  test("does not take the studio from a Sync again import holding it", async ({ page }) => {
    await seedSibling(page);
    const queue = await startImport(page);

    // "Sync again" tears the player down and claims the studio by id, with no
    // engine loaded. Set that claim the way importFromUrl does, through the
    // same module instance the app uses.
    await page.evaluate(async () => {
      const state = await import("/js/state.js");
      state.setForegroundJobId("aaaaaaaaaaaa");
    });

    await queue.settle();
    await page.waitForTimeout(1500);
    await expect(page.locator(".app")).toHaveClass(/no-track/);
    await expect(page.locator("#title")).not.toHaveText(TRACK_TITLE);
  });
});
