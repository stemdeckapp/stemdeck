// Removing a song from the Queue stops its extraction and keeps the song (#772).
//
// The remove button on a Queue row used to cancel the import, which deleted
// the song outright: it left the Library as well, and it never reached the
// Trash. It now only stops the extraction. The song stays in the Library with
// Extract, the way a song restored from the Trash shows, and Extract starts it
// again.
//
// The queue and this one job are answered here: the e2e backend never runs a
// separation, so a job that stays queued cannot be produced for real. The
// answers follow the server: a cancel ends "cancelled", a stop "stopped"
// (tests/test_queue_remove.py covers the server half).

import { test, expect } from "@playwright/test";
import {
  JOB_ID,
  SIBLING_JOB_ID,
  fixtureTrack,
  readCatalogState,
  seedCatalogState,
  stubExportEndpoints,
  stubUpdateCheck,
} from "./helpers.mjs";

const Q = "0e0e0e0e0e01";
const TITLE = "Waiting Song";

async function setUp(page) {
  await seedCatalogState(page, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: [Q, JOB_ID, SIBLING_JOB_ID], color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks: {
      [Q]: { ...fixtureTrack(Q, TITLE), status: "queued", sourceUrl: "local:waiting-song", createdAt: 1000 },
      [JOB_ID]: fixtureTrack(JOB_ID, "E2E Fixture Track"),
      [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, "E2E Fixture Track (again)"),
    },
  });
  await stubExportEndpoints(page);
  await stubUpdateCheck(page);

  const server = { status: "queued", calls: [] };
  const state = () => ({
    job_id: Q,
    title: TITLE,
    status: server.status,
    stage: server.status,
    progress: 0,
    source_url: "local:waiting-song",
    selected_stems: ["vocals", "drums", "bass", "other"],
    stems: [],
    created_at: 1000,
    trashed_at: null,
  });
  const queueBody = () => ({
    running: null,
    queued: server.status === "queued" ? [{ job_id: Q, title: TITLE, status: "queued", progress: 0 }] : [],
    paused: false,
    max_pending_uploads: 5,
    max_pending_urls: 50,
    capacity_left_uploads: 5,
    capacity_left_urls: 50,
  });
  await page.route("**/api/queue", (route) => route.fulfill({ json: queueBody() }));
  await page.route("**/api/queue/events", (route) => route.abort());
  await page.route(`**/api/jobs/${Q}`, (route) => route.fulfill({ json: state() }));
  for (const action of ["cancel", "stop", "trash", "extract"]) {
    await page.route(`**/api/jobs/${Q}/${action}`, (route) => {
      server.calls.push(action);
      if (action === "cancel") server.status = "cancelled";
      if (action === "stop") server.status = "stopped";
      if (action === "extract") server.status = "queued";
      return route.fulfill({ json: state() });
    });
  }
  // The startup sync reads the whole registry; this job is in it.
  await page.route(/\/api\/jobs\?trashed=include$/, async (route) => {
    const res = await route.fetch();
    const jobs = await res.json();
    return route.fulfill({ json: [...jobs, state()] });
  });
  return server;
}

const libraryRow = (page) => page.locator(`#catalogList .cat-item[data-id="${Q}"]:not(.queue-row)`);

test("removing a song from the Queue keeps it in the Library with Extract, not in the Trash", async ({ page }) => {
  const server = await setUp(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await page.locator(".rail-queue").click();
  const remove = page.locator(`.queue-row[data-id="${Q}"] .queue-cancel`);
  await expect(remove).toBeVisible();
  await remove.click();

  await expect(page.locator(`.queue-row[data-id="${Q}"]`)).toHaveCount(0);

  await page.locator(".rail-library").click();
  const row = libraryRow(page);
  await expect(row, "the song left the Library").toBeVisible();
  await expect(row.locator(".cat-extract")).toHaveText("Extract");
  const stored = await readCatalogState(page);
  expect(stored.tracks[Q]?.status).toBe("stopped");
  expect(stored.folders.find((f) => f.id === "trash").items).not.toContain(Q);
  // Never a cancel, which deletes the song's files on the server.
  expect(server.calls).toEqual(["stop"]);

  // Extract starts it again, as for a song restored from the Trash.
  await row.hover();
  await row.locator(".cat-extract").click();
  await expect.poll(() => server.calls).toEqual(["stop", "extract"]);
  await expect(page.locator(".rail-queue")).toBeVisible();
  await expect(row.locator(".cat-extract")).toHaveCount(0);
});

test("the Queue's remove button says it stops the extraction and keeps the song", async ({ page }) => {
  await setUp(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(".rail-queue").click();
  const remove = page.locator(`.queue-row[data-id="${Q}"] .queue-cancel`);
  await expect(remove).toHaveAttribute("title", "Stop extracting. The song stays in your library");
  await expect(remove).toHaveAttribute("aria-label", `Stop extracting ${TITLE}`);
});
