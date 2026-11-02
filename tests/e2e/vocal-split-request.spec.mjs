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
  fixtureTrack, seedCatalogState, seedLibrary, readCatalogState, stubUpdateCheck, stubVocalSplitHealth, openStudio,
} from "./helpers.mjs";

// A test below plays the song through a split.
test.use({ launchOptions: { args: ["--mute-audio", "--autoplay-policy=no-user-gesture-required"] } });

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

/** The open song's lanes, by stem name. */
const lanes = (page) =>
  page.evaluate(async () => Object.keys((await import("/js/state.js")).trackIndex).filter((n) => !/^t[kr]_/.test(n)));

/** The open song's split as the Stems control reads it. */
const splitShown = (page) =>
  page.evaluate(async () => {
    const s = (await import("/js/modeStems.js")).layoutState();
    return { jobId: s.jobId, state: s.splitState, reason: s.splitReason };
  });

const studioReady = (page) =>
  page.waitForFunction(
    () => !/\/\s*00:00\s*$/.test(document.querySelector("#t-time")?.textContent || "00:00 / 00:00"),
    null,
    { timeout: 20000 },
  );

/** Whether the transport plays, where, and whether its engine is the one
 * kept as window.__before. */
const transportNow = (page) =>
  page.evaluate(async () => {
    const st = await import("/js/state.js");
    const tr = (await import("/js/transport.js")).transport();
    return {
      playing: Boolean(tr?.isPlaying?.()),
      time: tr?.getCurrentTime?.() ?? 0,
      sameEngine: st.audioEngine === window.__before,
      beforePlaying: Boolean(window.__before?.isPlaying?.()),
    };
  });

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

// A split that ends while its song is open. The song used to keep one Vocals
// lane and a "Splitting..." Stems button until it was opened again.
test.describe("the open song when its split ends", () => {
  test.beforeEach(async ({ page }) => {
    await stubVocalSplitHealth(page, { available: true, reason: null });
  });

  /** A split this page starts for the import and that runs until `release()`. */
  async function holdSplit(page) {
    const job = await serveJob(page, () => ({ vocal_split: "none", vocal_split_wanted: true }));
    const posts = [];
    let release;
    const held = new Promise((r) => { release = r; });
    await page.route(SPLIT_URL, async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      posts.push(1);
      job.set(() => ({ vocal_split: "running", vocal_split_wanted: true }));
      const answer = await held;
      job.set(answer.ok ? withPair : () => ({ vocal_split: "error", vocal_split_reason: answer.reason, vocal_split_wanted: true }));
      return answer.ok
        ? route.fulfill({ json: {} })
        : route.fulfill({ status: 500, json: { detail: "vocal split failed", vocal_split_reason: answer.reason } });
    });
    await openWithImportInProgress(page);
    await expect.poll(() => posts.length).toBe(1);
    // The import is done and its split runs: the song opens with one Vocals lane.
    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    await studioReady(page);
    expect(await lanes(page)).not.toContain("lead_vocals");
    expect(await splitShown(page)).toMatchObject({ jobId: JOB_ID, state: "running" });
    return { posts, release };
  }

  test("gets its Lead + Backing lanes", async ({ page }) => {
    const { posts, release } = await holdSplit(page);

    release({ ok: true });

    await expect.poll(() => lanes(page)).toEqual(expect.arrayContaining(["lead_vocals", "backing_vocals"]));
    expect(await splitShown(page)).toMatchObject({ jobId: JOB_ID, state: "done" });
    expect(posts).toHaveLength(1);
  });

  test("keeps playing from where it was", async ({ page }) => {
    const { release } = await holdSplit(page);
    await page.waitForFunction(async () => {
      const st = await import("/js/state.js");
      if (!st.audioEngine || (await st.audioEngine.ready) !== true) return false;
      window.__before = st.audioEngine;
      return true;
    }, null, { timeout: 20000 });
    await page.locator("#t-play").click();
    await expect.poll(async () => (await transportNow(page)).time).toBeGreaterThan(0.5);
    const before = await transportNow(page);

    release({ ok: true });

    await expect.poll(() => lanes(page)).toEqual(expect.arrayContaining(["lead_vocals", "backing_vocals"]));
    await expect.poll(async () => {
      const now = await transportNow(page);
      return !now.sameEngine && now.playing && now.time >= before.time;
    }).toBe(true);
    // One song plays: the engine from before the split is not still going.
    expect((await transportNow(page)).beforePlaying).toBe(false);
  });

  test("says why when the split fails", async ({ page }) => {
    const { release } = await holdSplit(page);

    release({ ok: false, reason: "out_of_memory" });

    await expect(page.locator("#error")).toContainText(await tr(page, "split.reason.out_of_memory"));
    await expect.poll(() => splitShown(page)).toEqual({ jobId: JOB_ID, state: "error", reason: "out_of_memory" });
    expect(await lanes(page)).toContain("vocals");
  });

  test("a split no page here started is followed too", async ({ page }) => {
    // A page reloaded during the split, or a second window: the song is done
    // in the library and its split runs elsewhere.
    const job = await serveJob(page, () => ({ vocal_split: "running", vocal_split_wanted: true }));
    const posts = [];
    await page.route(SPLIT_URL, (route) => {
      posts.push(1);
      return route.fulfill({ status: 409, json: { detail: "vocal split already running" } });
    });
    await openStudio(page);
    expect(await splitShown(page)).toMatchObject({ jobId: JOB_ID, state: "running" });

    job.set(withPair);

    await expect.poll(() => lanes(page), { timeout: 15_000 })
      .toEqual(expect.arrayContaining(["lead_vocals", "backing_vocals"]));
    expect(await splitShown(page)).toMatchObject({ jobId: JOB_ID, state: "done" });
    expect(posts).toEqual([]);
  });

  test("a split no page here started says why it failed", async ({ page }) => {
    const job = await serveJob(page, () => ({ vocal_split: "running", vocal_split_wanted: true }));
    await openStudio(page);
    expect(await splitShown(page)).toMatchObject({ jobId: JOB_ID, state: "running" });

    job.set(() => ({ vocal_split: "error", vocal_split_reason: "timeout", vocal_split_wanted: true }));

    await expect(page.locator("#error")).toContainText(await tr(page, "split.reason.timeout"), { timeout: 15_000 });
    await expect.poll(() => splitShown(page)).toEqual({ jobId: JOB_ID, state: "error", reason: "timeout" });
  });

  test("a split that started after this page's own one ended is waited for", async ({ page }) => {
    // This page's split failed earlier; another window runs it again, and the
    // song reloads meanwhile (a mode switch, say).
    const job = await serveJob(page, () => ({}));
    await page.route(SPLIT_URL, (route) =>
      route.fulfill({ status: 500, json: { detail: "vocal split failed", vocal_split_reason: "worker_failed" } }));
    await openStudio(page);
    await page.evaluate(async (id) => {
      const job = await import("/js/job.js");
      const state = await (await fetch(`/api/jobs/${id}`)).json();
      await job.runVocalSplitIfWanted({ ...state, vocal_split_wanted: true });
    }, JOB_ID);
    job.set(() => ({ vocal_split: "running" }));
    let loads = 0;
    page.on("request", (req) => { if (req.url().endsWith(`/api/jobs/${JOB_ID}/peaks`)) loads++; });

    await page.evaluate(async (id) => (await import("/js/reloadPlace.js")).reloadKeepingPlace(id), JOB_ID);
    await expect.poll(() => splitShown(page)).toMatchObject({ jobId: JOB_ID, state: "running" });
    // Still running: no reload follows the one asked for.
    await page.waitForTimeout(3000);
    expect(loads).toBe(1);

    job.set(withPair);
    await expect.poll(() => lanes(page), { timeout: 15_000 })
      .toEqual(expect.arrayContaining(["lead_vocals", "backing_vocals"]));
  });

  test("another song opened meanwhile is left alone", async ({ page }) => {
    const { release } = await holdSplit(page);
    await page.locator(`.cat-item[data-id="${SIBLING_JOB_ID}"]`).first().click();
    await expect.poll(() => page.evaluate(async () => (await import("/js/state.js")).currentJobId)).toBe(SIBLING_JOB_ID);
    await studioReady(page);
    await page.locator("#t-play").click();
    await expect.poll(async () => (await transportNow(page)).playing).toBe(true);
    const followed = page.evaluate(async (id) => {
      const st = await import("/js/state.js");
      const tr = (await import("/js/transport.js")).transport();
      const reload = await import("/js/reloadPlace.js");
      const paired = () => (JSON.parse(localStorage.getItem("stemdeck.folders"))?.tracks?.[id]?.audioStems || [])
        .some((s) => s.name === "backing_vocals");
      // Sampled on every frame until the split's song has its pair saved.
      let paused = false;
      let restoring = false;
      while (!paired()) {
        paused ||= !tr.isPlaying();
        restoring ||= reload.restorePending();
        await new Promise((r) => requestAnimationFrame(r));
      }
      for (let i = 0; i < 30; i++) {
        paused ||= !tr.isPlaying();
        restoring ||= reload.restorePending();
        await new Promise((r) => requestAnimationFrame(r));
      }
      return { paused, restoring, open: st.currentJobId };
    }, JOB_ID);

    release({ ok: true });

    // Not paused, not reloaded, still the song the user chose.
    expect(await followed).toEqual({ paused: false, restoring: false, open: SIBLING_JOB_ID });
  });
});

// Try again while a split of the same song already runs (a 409): it waits
// for that split and then shows its lanes (F52).
test("Try again on a split already running waits for it and shows the pair", async ({ page }) => {
  await stubVocalSplitHealth(page, { available: true, reason: null });
  const job = await serveJob(page, () => ({}));
  const posts = [];
  await page.route(SPLIT_URL, (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    posts.push(1);
    if (posts.length === 1) {
      return route.fulfill({ status: 500, json: { detail: "vocal split failed", vocal_split_reason: "worker_failed" } });
    }
    // Another window started it again meanwhile: running for two reads.
    let reads = 0;
    job.set((body) => (reads++ < 2 ? { vocal_split: "running" } : withPair(body)));
    return route.fulfill({ status: 409, json: { detail: "vocal split already running" } });
  });
  await openStudio(page);
  await page.evaluate(async (id) => {
    const job = await import("/js/job.js");
    const state = await (await fetch(`/api/jobs/${id}`)).json();
    await job.runVocalSplitIfWanted({ ...state, vocal_split_wanted: true });
  }, JOB_ID);
  await expect(page.locator("#error .split-retry-btn")).toBeVisible();

  await page.locator("#error .split-retry-btn").click();

  // The overlay stays while it waits, and is gone once the pair is loaded.
  await expect(page.locator("#waveLoadingOverlay")).toBeVisible();
  await expect.poll(() => lanes(page), { timeout: 15_000 })
    .toEqual(expect.arrayContaining(["lead_vocals", "backing_vocals"]));
  await expect(page.locator("#waveLoadingOverlay")).toBeHidden();
  expect(posts).toHaveLength(2);
});
