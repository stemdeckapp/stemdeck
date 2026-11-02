// Lead + Backing chosen at import is kept on the job (#848). It used to live
// only in the page that submitted the import, so a reload, a restart or a
// second window during the import dropped it: the song finished with one
// Vocals stem and nothing said why.
//
// The fixture has no model to split with, so the split itself is stubbed at
// POST /api/jobs/{id}/vocal-split, and the job's state is the real one with
// only the split fields changed. The server side (the request stored on the
// job, through a restart) is tests/test_jobs_api.py and
// tests/test_registry_persistence.py.
import { test, expect } from "@playwright/test";
import {
  JOB_ID, SIBLING_JOB_ID, SIBLING_TITLE, TRACK_TITLE,
  fixtureTrack, seedCatalogState, seedLibrary, readCatalogState, stubUpdateCheck, stubVocalSplitHealth,
} from "./helpers.mjs";

const SPLIT_URL = `**/api/jobs/${JOB_ID}/vocal-split`;
const NEW_JOB_ID = "e2e0a5c0848a";

const tr = (page, key, vars) =>
  page.evaluate(async ([k, v]) => (await import("/js/i18n.js")).t(k, v), [key, vars]);

/** The fixture job as the server states it, with `patch(state)` laid over. */
async function serveJob(page, patch) {
  let current = patch;
  await page.route(`**/api/jobs/${JOB_ID}`, async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: { ...body, ...current(body) } });
  });
  return { set: (next) => { current = next; } };
}

/** The pair a finished split adds, played from the fixture's vocals file. */
function withPair(body) {
  const vocals = body.stems.find((s) => s.name === "vocals");
  return {
    vocal_split: "done",
    vocal_split_wanted: true,
    stems: [...body.stems, { ...vocals, name: "lead_vocals" }, { ...vocals, name: "backing_vocals" }],
  };
}

/** A page that opens while the fixture's import still reads "processing" in
 * the library: what a reload, a restart or a second window finds. */
async function openWithImportInProgress(page) {
  await seedCatalogState(page, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: [JOB_ID, SIBLING_JOB_ID], color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks: {
      [JOB_ID]: { ...fixtureTrack(JOB_ID, TRACK_TITLE), status: "processing" },
      [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, SIBLING_TITLE),
    },
  });
  await stubUpdateCheck(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
}

const savedStems = async (page) =>
  ((await readCatalogState(page))?.tracks?.[JOB_ID]?.audioStems || []).map((s) => s.name);

test.describe("Lead + Backing chosen at import", () => {
  test.beforeEach(async ({ page }) => {
    await stubVocalSplitHealth(page, { available: true, reason: null });
  });

  for (const kind of ["link", "upload"]) {
    test(`goes to the server with the ${kind}`, async ({ page }) => {
      const sent = [];
      await page.route(/\/api\/jobs$/, (route) => {
        if (route.request().method() !== "POST") return route.fallback();
        sent.push(route.request().postData() || "");
        return route.fulfill({ json: { job_id: NEW_JOB_ID } });
      });
      await seedLibrary(page);
      await stubUpdateCheck(page);
      await page.goto("/", { waitUntil: "domcontentloaded" });

      const split = page.locator('#vocalModeToggle .vocal-mode-btn[data-mode="split"]');
      await split.click();
      await expect(split).toHaveAttribute("aria-pressed", "true");
      if (kind === "link") {
        await page.locator("#url").fill("https://youtu.be/dQw4w9WgXcQ");
      } else {
        await page.locator("#fileInput").setInputFiles({
          name: "song.wav", mimeType: "audio/wav", buffer: Buffer.from("RIFF0000WAVE"),
        });
      }
      await page.locator("#submit").click();

      await expect.poll(() => sent.length).toBe(1);
      if (kind === "link") expect(JSON.parse(sent[0]).vocal_split).toBe(true);
      else expect(sent[0]).toMatch(/name="vocal_split"\r\n\r\ntrue\r\n/);
    });
  }

  test("is split once a reloaded page finds the import done", async ({ page }) => {
    const job = await serveJob(page, () => ({ vocal_split: "none", vocal_split_wanted: true }));
    const posts = [];
    await page.route(SPLIT_URL, (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      posts.push(1);
      // The page reads the job again after a split: from now on it has the pair.
      job.set(withPair);
      return route.fulfill({ json: {} });
    });

    await openWithImportInProgress(page);

    await expect.poll(() => savedStems(page)).toEqual(expect.arrayContaining(["lead_vocals", "backing_vocals"]));
    // Settled: no second request follows.
    await page.waitForTimeout(1000);
    expect(posts).toHaveLength(1);
    expect((await readCatalogState(page)).tracks[JOB_ID].status).toBe("done");
  });

  test("both completion paths of one page share one split and its result", async ({ page }) => {
    // A "Sync again" restore is finished by its own stream and by the queue.
    const job = await serveJob(page, () => ({ vocal_split: "none", vocal_split_wanted: true }));
    const posts = [];
    let release;
    const held = new Promise((r) => { release = r; });
    await page.route(SPLIT_URL, async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      posts.push(1);
      await held;
      job.set(withPair);
      return route.fulfill({ json: {} });
    });
    await seedLibrary(page);
    await stubUpdateCheck(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });

    const both = page.evaluate(async (id) => {
      const job = await import("/js/job.js");
      const state = await (await fetch(`/api/jobs/${id}`)).json();
      const results = await Promise.all([job.runVocalSplitIfWanted(state), job.runVocalSplitIfWanted(state)]);
      return results.map((s) => s.stems.map((x) => x.name));
    }, JOB_ID);
    await expect.poll(() => posts.length).toBe(1);
    release();

    for (const stems of await both) expect(stems).toEqual(expect.arrayContaining(["lead_vocals", "backing_vocals"]));
    expect(posts).toHaveLength(1);
  });

  test("a split another page already started is waited for, not dropped", async ({ page }) => {
    // Running for the first two reads, then the other page's split ends.
    let reads = 0;
    await serveJob(page, (body) =>
      (reads++ < 2 ? { vocal_split: "running", vocal_split_wanted: true } : withPair(body)));
    const posts = [];
    await page.route(SPLIT_URL, (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      posts.push(1);
      return route.fulfill({ status: 409, json: { detail: "vocal split already running" } });
    });

    await openWithImportInProgress(page);

    await expect.poll(() => savedStems(page), { timeout: 15_000 })
      .toEqual(expect.arrayContaining(["lead_vocals", "backing_vocals"]));
    await page.waitForTimeout(1000);
    expect(posts).toHaveLength(1);
    expect(reads).toBeGreaterThan(2);
    await expect(page.locator("#error")).toBeHidden();
  });

  test("a split that failed while no page watched is said on the next load", async ({ page }) => {
    await serveJob(page, () => ({ vocal_split: "error", vocal_split_reason: "interrupted", vocal_split_wanted: true }));
    const posts = [];
    await page.route(SPLIT_URL, (route) => {
      posts.push(1);
      return route.fulfill({ status: 500, json: { detail: "vocal split failed", vocal_split_reason: "worker_failed" } });
    });

    await openWithImportInProgress(page);

    const box = page.locator("#error");
    await expect(box).toContainText(await tr(page, "split.failed", { title: TRACK_TITLE }));
    await expect(box).toContainText(await tr(page, "split.reason.interrupted"));
    await expect(box.locator(".split-retry-btn")).toBeVisible();
    // Said, not run again on its own: Try again is the person's choice.
    expect(posts).toEqual([]);
  });

  test("is not asked for when the import did not choose it", async ({ page }) => {
    await serveJob(page, () => ({ vocal_split: "none", vocal_split_wanted: false }));
    const posts = [];
    await page.route(SPLIT_URL, (route) => {
      posts.push(1);
      return route.fulfill({ status: 500, json: { detail: "vocal split failed", vocal_split_reason: "worker_failed" } });
    });

    await openWithImportInProgress(page);

    await expect.poll(async () => (await readCatalogState(page))?.tracks?.[JOB_ID]?.status).toBe("done");
    expect(posts).toEqual([]);
  });
});
