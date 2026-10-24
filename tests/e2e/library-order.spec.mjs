// The Library lists songs oldest first, by when they were added, like the
// Queue (#772). It listed them newest first, so three songs added one after
// the other read 1, 2, 3 in the Queue and 3, 2, 1 in the Library.
//
// The order comes from when each song was added, not from where it sits in
// the stored folder, so an existing library that was stored newest first
// shows oldest first too. The phone reads the same list from /api/jobs and
// orders it the same way.

import { test, expect } from "@playwright/test";
import { fixtureTrack, seedCatalogState, stubExportEndpoints, stubUpdateCheck } from "./helpers.mjs";

const A = "0d0d0d0d0d01";
const B = "0d0d0d0d0d02";
const C = "0d0d0d0d0d03";

/** A track with its own source, so the startup dedup-by-source (#542) leaves it alone. */
const track = (id, title, createdAt) => ({
  ...fixtureTrack(id, title),
  sourceUrl: `local:${title}`,
  createdAt,
});

const shownOrder = (page) =>
  page.locator("#catalogList .cat-item").evaluateAll((els) => els.map((e) => e.dataset.id));

test("the Library shows the first song added first, whatever order it was stored in", async ({ page }) => {
  await seedCatalogState(page, {
    folders: [
      // Stored newest first, which is how every library before #772 was kept.
      { id: "f-unsorted", name: "Unsorted", items: [C, B, A], color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks: {
      [A]: track(A, "Song 1", 1000),
      [B]: track(B, "Song 2", 2000),
      [C]: track(C, "Song 3", 3000),
    },
  });
  await stubExportEndpoints(page);
  await stubUpdateCheck(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(`.cat-item[data-id="${A}"]`).waitFor({ timeout: 20000 });

  const order = (await shownOrder(page)).filter((id) => [A, B, C].includes(id));
  expect(order).toEqual([A, B, C]);
  // The two fixture jobs the startup sync adopts were added after all three,
  // so they come last rather than above the songs already there.
  const all = await shownOrder(page);
  expect(all.slice(0, 3)).toEqual([A, B, C]);
  expect(all.length).toBeGreaterThan(3);
});

test("songs added at the same moment keep one fixed order", async ({ page }) => {
  const X = "0d0d0d0d0d0a";
  const Y = "0d0d0d0d0d0b";
  await seedCatalogState(page, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: [Y, X], color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks: { [X]: track(X, "Same time X", 1000), [Y]: track(Y, "Same time Y", 1000) },
  });
  await stubUpdateCheck(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(`.cat-item[data-id="${X}"]`).waitFor({ timeout: 20000 });

  const order = (await shownOrder(page)).filter((id) => id === X || id === Y);
  expect(order).toEqual([X, Y]);
});

test("the phone's Library shows the first song added first", async ({ page }) => {
  const job = (job_id, title, created_at) => ({
    job_id,
    title,
    status: "done",
    stems: ["vocals", "drums", "bass", "other"],
    selected_stems: ["vocals", "drums", "bass", "other"],
    source_url: `local:${title}`,
    created_at,
    favorite: false,
  });
  await page.route(/\/api\/jobs$/, (route) => {
    if (route.request().method() !== "GET") return route.continue();
    // Deliberately in neither order: the page decides.
    return route.fulfill({ json: [job(B, "Song 2", 2000), job(C, "Song 3", 3000), job(A, "Song 1", 1000)] });
  });
  await page.goto("/?ui=mobile", { waitUntil: "domcontentloaded" });
  await page.locator('[data-action="tab"][data-tab="library"]').first().click();
  await page.locator(`.track[data-id="${A}"]`).first().waitFor({ timeout: 20000 });

  const order = await page.locator(".track").evaluateAll((els) => els.map((e) => e.dataset.id));
  expect(order).toEqual([A, B, C]);
});
