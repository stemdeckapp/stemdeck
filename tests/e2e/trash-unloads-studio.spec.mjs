// Trashing the song that is open empties the studio (#1072).
//
// Deleting a song used to forget it in the library list only. The mixer, the
// audio engine and the now-playing card kept showing it, and the Extract stems
// button stayed aimed at a song that was gone.
//
// The server's trash endpoint is stubbed and recorded: the real one would leave
// the shared fixture job trashed on the server, and the next spec's startup
// sync would move it out of its library.

import { test, expect } from "@playwright/test";

import {
  JOB_ID,
  SIBLING_JOB_ID,
  SIBLING_TITLE,
  TRACK_TITLE,
  fixtureTrack,
  openStudio,
  seedCatalogState,
  stubExportEndpoints,
  stubFavorites,
  stubUpdateCheck,
} from "./helpers.mjs";

// A link the composer accepts as importable (isReimportableSource). Nothing
// fetches it: the trash is stubbed and Extract stems is never pressed.
const LINK = "https://www.youtube.com/watch?v=e2eLink0001";

/** Answer the trash endpoint locally, as every case here does. */
async function stubTrash(page) {
  await page.route(/\/api\/jobs\/[^/]+\/trash$/, (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "{}" }),
  );
}

/** The fixture library, with the fixture track sourced from `sourceUrl`. */
async function seedWithSource(page, sourceUrl) {
  await seedCatalogState(page, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: [JOB_ID, SIBLING_JOB_ID], color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks: {
      [JOB_ID]: { ...fixtureTrack(JOB_ID, TRACK_TITLE), sourceUrl },
      [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, SIBLING_TITLE),
    },
  });
  await stubFavorites(page);
  await stubExportEndpoints(page);
  await stubUpdateCheck(page);
}

/**
 * The fixture job's own read, which opening it makes. Its source_url is
 * swapped when `sourceUrl` is given, since the server's answer is what the
 * open song is built from. `holdNext()` keeps the next read unanswered until
 * the function it returns is called, and resolves `asked` once that read
 * has reached the route.
 */
async function routeFixtureJob(page, { sourceUrl = null } = {}) {
  let held = null;
  await page.route(new RegExp(`/api/jobs/${JOB_ID}$`), async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    const gate = held;
    held = null;
    if (gate) {
      gate.markAsked();
      await gate.released;
    }
    const res = await route.fetch();
    const state = await res.json();
    if (sourceUrl) state.source_url = sourceUrl;
    await route.fulfill({ response: res, json: state });
  });
  return {
    holdNext() {
      let release;
      let markAsked;
      const released = new Promise((r) => { release = r; });
      const asked = new Promise((r) => { markAsked = r; });
      held = { released, markAsked };
      return { release, asked };
    },
  };
}

test("trashing the open song empties the studio", async ({ page }) => {
  const trashed = [];
  await page.route(/\/api\/jobs\/[^/]+\/trash$/, (route) => {
    trashed.push(route.request().url().match(/\/api\/jobs\/([^/]+)\/trash$/)[1]);
    return route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
  });
  await openStudio(page);
  await expect(page.locator(".app")).not.toHaveClass(/no-track/);

  // The row's actions only take the pointer on hover (#724).
  const row = page.locator(`.cat-item[data-id="${JOB_ID}"]`).first();
  await row.hover();
  await row.locator(".cat-del").click();

  await expect(page.locator(".app")).toHaveClass(/no-track/);
  await expect(page.locator("#t-time")).toHaveText(/00:00\s*\/\s*00:00/);
  await expect(page.locator(".cat-item.active")).toHaveCount(0);
  await expect(page.locator("#submit")).toHaveAttribute("data-resplit-job", "");
  expect(trashed).toContain(JOB_ID);
});

test("trashing a song that is not open leaves the open one playing", async ({ page }) => {
  await page.route(/\/api\/jobs\/[^/]+\/trash$/, (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "{}" }),
  );
  await openStudio(page);

  const other = page.locator(`.cat-item[data-id="${SIBLING_JOB_ID}"]`).first();
  await other.hover();
  await other.locator(".cat-del").click();

  await expect(page.locator(`.cat-item[data-id="${JOB_ID}"]`).first()).toHaveClass(/active/);
  await expect(page.locator(".app")).not.toHaveClass(/no-track/);
});

test("deleting the folder that holds the open song empties the studio", async ({ page }) => {
  // deleteFolder moves the folder's songs to the Trash itself, without
  // moveTrackToTrash, so it has to unload the open one on its own. Unsorted
  // cannot be deleted, so the song sits in a folder of the person's own.
  await stubTrash(page);
  await seedCatalogState(page, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: [SIBLING_JOB_ID], color: null },
      { id: "f-e2e-setlist", name: "Set list", items: [JOB_ID], color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks: {
      [JOB_ID]: fixtureTrack(JOB_ID, TRACK_TITLE),
      [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, SIBLING_TITLE),
    },
  });
  await stubFavorites(page);
  await stubExportEndpoints(page);
  await stubUpdateCheck(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
  await page.waitForFunction(
    () => !/\/\s*00:00\s*$/.test(document.querySelector("#t-time")?.textContent || "00:00 / 00:00"),
    null,
    { timeout: 20000 },
  );
  await expect(page.locator(".app")).not.toHaveClass(/no-track/);

  // The folder's delete button only shows on hover; it asks for no confirmation.
  const head = page.locator('#catalogList .folder[data-id="f-e2e-setlist"] > .folder-head');
  await head.hover();
  await head.locator(".f-del").click();

  await expect(page.locator('#catalogList .folder[data-id="f-e2e-setlist"]')).toHaveCount(0);
  await expect(page.locator(".app")).toHaveClass(/no-track/);
  await expect(page.locator(".cat-item.active")).toHaveCount(0);
  await expect(page.locator("#submit")).toHaveAttribute("data-resplit-job", "");
});

test("trashing an open link-sourced song leaves Extract stems nothing to import", async ({ page }) => {
  await stubTrash(page);
  await seedWithSource(page, LINK);
  await routeFixtureJob(page, { sourceUrl: LINK });
  await page.goto("/", { waitUntil: "domcontentloaded" });

  const row = page.locator(`.cat-item[data-id="${JOB_ID}"]`).first();
  await row.click();
  await page.waitForFunction(
    () => !/\/\s*00:00\s*$/.test(document.querySelector("#t-time")?.textContent || "00:00 / 00:00"),
    null,
    { timeout: 20000 },
  );
  // Opening it put its link in the box and lifted `required`: the state that
  // made Extract stems import the trashed song again.
  const url = page.locator("#url");
  await expect(url).toHaveValue(LINK);
  await expect(url).not.toHaveAttribute("required");

  await row.hover();
  await row.locator(".cat-del").click();

  await expect(page.locator(".app")).toHaveClass(/no-track/);
  await expect(url).toHaveValue("");
  await expect(url).toHaveAttribute("required", "");
  await expect(page.locator("#submit")).toHaveAttribute("data-resplit-job", "");
});

test("a song trashed while it is still loading does not open", async ({ page }) => {
  await stubTrash(page);
  await seedWithSource(page, fixtureTrack(JOB_ID, TRACK_TITLE).sourceUrl);
  const job = await routeFixtureJob(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  const row = page.locator(`.cat-item[data-id="${JOB_ID}"]`).first();
  await expect(row).toBeVisible();
  await expect(page.locator(".app")).toHaveClass(/no-track/);

  // The row's own loader (openTrackInStudio is what a click on it calls), run
  // from here so the test can wait for that load to end however it ends.
  // Its read of the job is held, so the trash lands while the load is out.
  const { release, asked } = job.holdNext();
  await page.evaluate(async (id) => {
    const catalog = await import("/js/catalog.js");
    window.__e2eLoad = catalog.openTrackInStudio(id);
  }, JOB_ID);
  await asked;

  await row.hover();
  await row.locator(".cat-del").click();
  release();
  await page.evaluate(() => window.__e2eLoad);

  // Before #1072's follow-up the load went on: the song opened from the Trash.
  await expect(page.locator(".app")).toHaveClass(/no-track/);
  await expect(page.locator(".cat-item.active")).toHaveCount(0);
  expect(await page.evaluate(async () => (await import("/js/catalog.js")).getCurrentTrackInfo())).toBeNull();
});
