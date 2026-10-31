// Takes that used to be lost to an engine that was replaced or never ran.
//
// A song with no peaks.json (older or hand-made jobs) started the streaming
// engine and swapped to the full-decode engine a moment later, when the
// peaks came back empty. A take started in that window recorded into the
// engine being destroyed: its context closed, Stop waited forever on
// "Saving", or the take was deleted as "too short" however long it was. The
// server now builds missing peaks (tests/test_stems_api.py); here the peaks
// answer is a held-back 404, the case where that build failed, so the window
// is wide and the test does not depend on how fast the fixture loads.
//
// The other tests are the safety net under any engine replacement: a take
// whose engine goes away mid-take keeps what it captured, and a take that
// never read the song's clock keeps its audio at an estimated place. Neither
// is deleted silently, and both say what happened.
import { test, expect } from "@playwright/test";
import { openStudio, JOB_ID } from "./helpers.mjs";
import { recBtn, removeTracks, jobState, watchTakes, waitForEngine } from "./recording-helpers.mjs";

test.use({
  launchOptions: {
    args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--mute-audio"],
  },
  permissions: ["microphone"],
});

test.afterEach(async ({ request }) => removeTracks(request));

/** Answer /peaks with 404 after `delayMs`, as a job without peaks.json did. */
async function peaksMissing(page, delayMs) {
  await page.route(`**/api/jobs/${JOB_ID}/peaks`, async (route) => {
    await new Promise((r) => setTimeout(r, delayMs));
    await route.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"peaks not found"}' });
  });
}

/** One recording track, so Record arms it with one press. */
async function oneTrack(request) {
  const created = await request.post(`/api/jobs/${JOB_ID}/tracks`, { data: {} });
  expect(created.status()).toBe(201);
}

/** Count the engines the page brings up, to show no swap happened. */
async function countEngines(page) {
  await page.addInitScript(() => {
    window.__engines = 0;
    let current = null;
    const tick = async () => {
      try {
        const st = await import("/js/state.js");
        if (st.audioEngine && st.audioEngine !== current) {
          current = st.audioEngine;
          window.__engines += 1;
        }
      } catch { /* the page is not there yet */ }
      setTimeout(tick, 20);
    };
    window.addEventListener("DOMContentLoaded", () => { tick(); });
  });
}

function secondsSent(log) {
  const sc = log.finishes.at(-1).body;
  return sc.timeline.end_song_sec - sc.timeline.first_sample_song_sec;
}

/** Make the current engine's clock never come up, whatever the reason. */
async function clockNeverReady(page) {
  await page.evaluate(async () => {
    const st = await import("/js/state.js");
    await st.audioEngine.ready;
    st.audioEngine.isClockReady = () => false;
  });
}

test("a take started right after opening a song without peaks is kept and placed", async ({ page, request }) => {
  await oneTrack(request);
  const log = watchTakes(page);
  await countEngines(page);
  await peaksMissing(page, 1500);
  await openStudio(page, { tauri: true });

  // At once, while the peaks answer is still out: the moment the old swap
  // destroyed the engine. Record waits for the engine the song settles on.
  await recBtn(page).click();
  await expect(recBtn(page)).toHaveClass(/recording/, { timeout: 10000 });
  await page.waitForTimeout(4000);
  // One engine for the whole take: none was swapped in under it.
  expect(await page.evaluate(() => window.__engines)).toBe(1);
  await recBtn(page).click();
  await expect(recBtn(page)).not.toHaveClass(/recording/, { timeout: 20000 });

  await expect.poll(() => log.finishStatus.at(-1), { timeout: 20000 }).toBe(200);
  expect(log.deletes).toEqual([]);
  expect(secondsSent(log)).toBeGreaterThan(3);
  // Placed by the song's own clock: armed before the song, so frame 0 is
  // just before song time 0, not an estimate.
  const sc = log.finishes.at(-1).body;
  expect(sc.timeline.first_sample_song_sec).toBeLessThanOrEqual(0);
  expect(sc.timeline.first_sample_song_sec).toBeGreaterThan(-1);
  await expect(page.locator("#recReview")).toBeVisible();
  await expect(page.locator("#recStatus")).toHaveText("");
  expect((await jobState(page)).takes.length).toBe(1);
});

test("a take whose engine is replaced mid-take keeps what it recorded", async ({ page, request }) => {
  await oneTrack(request);
  const log = watchTakes(page);
  await openStudio(page, { tauri: true });
  await waitForEngine(page);
  await recBtn(page).click();
  await expect(recBtn(page)).toHaveClass(/recording/, { timeout: 10000 });
  await page.waitForFunction(async () => (await import("/js/state.js")).audioEngine?.isClockReady?.(), null, { timeout: 10000 });
  await page.waitForTimeout(2500);

  // Reload the song's lanes under the take, as any change to its takes does.
  await page.evaluate((jobId) => {
    document.dispatchEvent(new CustomEvent("stemdeck:takes-changed", { detail: { jobId } }));
  }, JOB_ID);

  // It stops by itself and saves: no hang on "Saving", nothing deleted.
  await expect(recBtn(page)).not.toHaveClass(/recording|saving/, { timeout: 15000 });
  await expect.poll(() => log.finishStatus.at(-1), { timeout: 20000 }).toBe(200);
  expect(log.deletes).toEqual([]);
  expect(secondsSent(log)).toBeGreaterThan(2);
  await expect(page.locator("#recStatus")).toHaveText(/song reloaded/i);
  await expect(page.locator("#recReview")).toBeVisible();
  // What plays now is not being recorded, so it does not play on.
  await waitForEngine(page);
  expect(await page.evaluate(async () => (await import("/js/state.js")).audioEngine.isPlaying())).toBe(false);
});

test("a take that never reads the song's clock keeps its audio and says so", async ({ page, request }) => {
  await oneTrack(request);
  const log = watchTakes(page);
  await openStudio(page, { tauri: true });
  await waitForEngine(page);
  await clockNeverReady(page);
  await page.evaluate(async () => (await import("/js/state.js")).audioEngine.setTime(1));
  await recBtn(page).click();
  await expect(recBtn(page)).toHaveClass(/recording/, { timeout: 10000 });
  await page.waitForTimeout(2500);
  await recBtn(page).click();
  await expect(recBtn(page)).not.toHaveClass(/recording/, { timeout: 20000 });

  await expect.poll(() => log.finishStatus.at(-1), { timeout: 20000 }).toBe(200);
  expect(log.deletes).toEqual([]);
  expect(secondsSent(log)).toBeGreaterThan(2);
  // Where the playhead stood (1 s), less the round trip.
  const sc = log.finishes.at(-1).body;
  expect(sc.timeline.first_sample_song_sec).toBeGreaterThan(0.8);
  expect(sc.timeline.first_sample_song_sec).toBeLessThanOrEqual(1);
  await expect(page.locator("#recStatus")).toHaveText(/could not be lined up/i);
  await expect(page.locator("#recReview")).toBeVisible();
});
