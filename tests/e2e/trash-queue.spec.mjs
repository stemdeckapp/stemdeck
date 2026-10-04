// Trashing an import that is still in the queue (#748), and what the Trash
// says about files on disk (#749).
//
// The queue is stubbed rather than driven: the e2e backend never runs a real
// separation, so a job that stays "running" long enough to click on cannot be
// produced against it. The server half (trash cancels the job) is covered by
// tests/test_jobs_trash.py; this covers what the page does around it.

import { test, expect } from "@playwright/test";

import { fixtureTrack, seedCatalogState, stubUpdateCheck } from "./helpers.mjs";

const RUNNING_ID = "e2e0feedf00d";
const RUNNING_TITLE = "Still Importing";

function queueFrame(running) {
  return {
    running: running
      ? { job_id: RUNNING_ID, status: "analyzing", stage: "Analyzing audio...", progress: 0, title: RUNNING_TITLE }
      : null,
    queued: [],
    paused: false,
    max_pending_uploads: 5,
    max_pending_urls: 50,
    capacity_left_uploads: 5,
    capacity_left_urls: 50,
  };
}

/**
 * A library holding one import that the queue reports as running, until the
 * page trashes it. Counts the queue reads, so a test can tell a refresh that
 * followed the trash from one that was already going to happen.
 */
async function seedRunningImport(page) {
  await seedCatalogState(page, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: [RUNNING_ID], color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    // Its own source, or the startup sync's dedup-by-source (#542) folds it
    // into the seeded fixture job and the row under test is gone.
    tracks: {
      [RUNNING_ID]: {
        ...fixtureTrack(RUNNING_ID, RUNNING_TITLE),
        status: "analyzing",
        sourceUrl: "local:still-importing",
      },
    },
  });
  await stubUpdateCheck(page);

  const seen = { trashed: false, queueReadsAfterTrash: 0 };
  await page.route(`**/api/jobs/${RUNNING_ID}/trash`, (route) => {
    seen.trashed = true;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ job_id: RUNNING_ID, trashed_at: 1 }),
    });
  });
  await page.route("**/api/queue", (route) => {
    if (seen.trashed) seen.queueReadsAfterTrash += 1;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(queueFrame(!seen.trashed)),
    });
  });
  // The stream is held off so the only queue reads are the ones the page asks
  // for, which is what the refresh test counts.
  await page.route("**/api/queue/events", (route) => route.abort());
  return seen;
}

test("a queue row offers the same trash can as the library rows", async ({ page }) => {
  await seedRunningImport(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await page.locator(".rail-queue").click();
  const cancel = page.locator(`.queue-row[data-id="${RUNNING_ID}"] .queue-cancel`);
  await expect(cancel).toBeVisible();
  // The library's trash can, not the old X (#748).
  await expect(cancel.locator('polyline[points="3 6 5 6 21 6"]')).toHaveCount(1);
  await expect(cancel.locator('path[d="M18 6 6 18 M6 6l12 12"]')).toHaveCount(0);
});

test("trashing an import that is still running refreshes the queue at once", async ({ page }) => {
  const seen = await seedRunningImport(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  // The first read has to have landed, or the page does not yet know the
  // track is in the queue and has no reason to refresh it.
  await page.locator(".rail-queue").click();
  await expect(page.locator(`.queue-row[data-id="${RUNNING_ID}"]`)).toBeVisible();
  await page.locator(".rail-library").click();

  // The row's actions only take the pointer on hover (#724).
  const row = page.locator(`.cat-item[data-id="${RUNNING_ID}"]`);
  await row.hover();
  await row.locator(".cat-del").click();

  await expect.poll(() => seen.queueReadsAfterTrash).toBeGreaterThan(0);
  // The stream is aborted, so only the refresh the trash asked for can have
  // told the page the queue is empty, which hides the queue button.
  await expect(page.locator(".rail-queue")).toHaveClass(/(^| )hidden( |$)/);
});
