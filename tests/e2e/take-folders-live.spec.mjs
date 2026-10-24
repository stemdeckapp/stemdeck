// The live waveform (capture/liveWave.js) inside a take folder
// (capture/takeLanes.js): recording on a track that already has takes.
//
// While it records, the live bars draw over the track's own lane, anchored to
// its .take-live-slot, with the folder open or closed, and the previous take's
// bars are hidden there. After Stop the new take is the newest take lane and
// the one that plays, and the live bars hand over to the server's with no
// jump: bar for bar the same heights, measured at the moment of the switch in
// the folder's layout (which gains a row, so every lane changes height).
import { test, expect } from "@playwright/test";
import { openStudio, JOB_ID } from "./helpers.mjs";
import { recBtn, removeTracks, jobState, waitForEngine, watchTakes } from "./recording-helpers.mjs";

test.use({
  launchOptions: {
    args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--mute-audio"],
  },
  permissions: ["microphone"],
});

test.afterEach(async ({ request }) => removeTracks(request));

const RATE = 48000;

function tone(seconds, amp) {
  const n = Math.round(RATE * seconds);
  const buf = Buffer.alloc(n * 4);
  for (let i = 0; i < n; i++) buf.writeFloatLE(amp * Math.sin((2 * Math.PI * 220 * i) / RATE), i * 4);
  return buf;
}

/** A track with two finished takes made through the takes API. */
async function trackWithTwoTakes(request) {
  const r = await request.post(`/api/jobs/${JOB_ID}/tracks`, { data: {} });
  expect(r.status()).toBe(201);
  const trackId = (await r.json()).id;
  const takes = [];
  for (const amp of [0.3, 0.6]) {
    const c = await request.post(`/api/jobs/${JOB_ID}/takes`, { data: { track_id: trackId } });
    const takeId = (await c.json()).take_id;
    await request.put(`/api/jobs/${JOB_ID}/takes/${takeId}/parts/audio?seq=0`, {
      data: tone(3, amp),
      headers: { "content-type": "application/octet-stream" },
    });
    const fin = await request.post(`/api/jobs/${JOB_ID}/takes/${takeId}/finish`, {
      data: { v: 1, timeline: { first_sample_song_sec: 0.5, tempo: 1.0 }, capture: { rate: RATE, channels: 1 } },
    });
    expect(fin.status(), await fin.text()).toBe(200);
    takes.push(takeId);
  }
  return { trackId, takes };
}

/** Every visible mixer row and waveform lane, top to bottom. */
async function stack(page) {
  return page.evaluate(() => {
    const visible = (sel) => [...document.querySelectorAll(sel)]
      .filter((e) => !e.classList.contains("hidden") && e.getBoundingClientRect().height > 0)
      .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
    const id = (e) => e.dataset.takeId || e.dataset.stem;
    const top = (e) => Math.round(e.getBoundingClientRect().top);
    const mix = visible(".mixer-column .lane-header");
    const wave = visible(".stem-waveform-layer > .stem-waveform-row");
    return { mix: mix.map((e) => [id(e), top(e)]), wave: wave.map((e) => [id(e), top(e)]) };
  });
}

function expectAligned(g) {
  expect(g.wave.map((r) => r[0])).toEqual(g.mix.map((r) => r[0]));
  for (let i = 0; i < g.mix.length; i++) {
    expect(Math.abs(g.mix[i][1] - g.wave[i][1]), `row ${i} (${g.mix[i][0]})`).toBeLessThanOrEqual(1);
  }
}

/** The live lane against the track's main lane, while recording. */
async function liveOverMain(page, trackId) {
  return page.evaluate((id) => {
    const lane = document.querySelector(`.live-wave-lane[data-track-id="${id}"]`);
    const row = document.querySelector(`.stem-waveform-row[data-stem="${id}"]`);
    const slot = row?.querySelector(":scope > .take-live-slot");
    const canvas = lane?.querySelector("canvas");
    let pixels = 0;
    if (canvas?.width) {
      const { data } = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height);
      for (let i = 3; i < data.length; i += 4) if (data[i] > 0) pixels++;
    }
    const lr = lane?.getBoundingClientRect();
    const sr = slot?.getBoundingClientRect();
    const svg = row?.querySelector(".stem-waveform-svg");
    return {
      lane: !!lane,
      slot: !!slot,
      pixels,
      dTop: lr && sr ? Math.abs(lr.top - sr.top) : NaN,
      dH: lr && sr ? Math.abs(lr.height - sr.height) : NaN,
      oldBarsHidden: !svg || getComputedStyle(svg).visibility === "hidden",
    };
  }, trackId);
}

async function untilSongTime(page, sec) {
  await page.evaluate(async () => { window.__e2eState = await import("/js/state.js"); });
  await page.waitForFunction((t) => window.__e2eState.audioEngine?.isPlaying()
    && window.__e2eState.audioEngine.getCurrentTime() >= t, sec, { timeout: 15000 });
}

/** Note what the live lane was, and the server's bars, when it went. */
async function watchHandoff(page) {
  await page.evaluate(() => {
    window.__live = { removedWithBars: null, handoff: null, server: null };
    document.addEventListener("stemdeck:live-wave-handoff", (e) => { window.__live.handoff = e.detail; });
    new MutationObserver((muts) => {
      for (const m of muts) {
        for (const n of m.removedNodes) {
          if (!n.classList?.contains("live-wave-lane")) continue;
          const row = document.querySelector(`.stem-waveform-row[data-stem="${n.dataset.trackId}"]`);
          const rects = row ? [...row.querySelectorAll(":scope > .stem-waveform-svg rect")] : [];
          window.__live.removedWithBars = rects.length;
          window.__live.server = {
            bars: rects.length,
            heights: rects.map((r) => (parseFloat(r.getAttribute("height")) / 48) * row.clientHeight),
          };
        }
      }
    }).observe(document.querySelector(".waves-column"), { childList: true });
  });
}

function expectNoJump(live) {
  expect(live.removedWithBars, "the server's bars were drawn when the live lane went").toBeGreaterThan(0);
  expect(live.handoff).not.toBeNull();
  const { server } = live;
  expect(server.bars).toBe(live.handoff.bars);
  const heights = Object.values(live.handoff.heights);
  const drawn = heights.map((v, i) => (v >= 0 ? i : -1)).filter((i) => i >= 0).slice(1, -1);
  let sum = 0;
  let close = 0;
  for (const i of drawn) {
    const d = Math.abs(heights[i] - server.heights[i]);
    sum += d;
    if (d <= 2) close++;
  }
  const mean = sum / Math.max(1, drawn.length);
  console.log(`folder handoff: ${drawn.length} bars, ${close} within 2 px, mean diff ${mean.toFixed(2)} px on a ${live.handoff.height} px lane`);
  expect(drawn.length).toBeGreaterThan(20);
  expect(close / drawn.length, "share of bars within 2 px").toBeGreaterThan(0.95);
  expect(mean, "mean live vs server bar height, px").toBeLessThan(0.5);
}

for (const folder of ["open", "closed"]) {
  test(`recording on a track with two takes, folder ${folder}: live bars on the main lane, then the newest take lane, no jump`, async ({ page, request }, testInfo) => {
    test.setTimeout(90_000);
    const log = watchTakes(page);
    const { trackId, takes } = await trackWithTwoTakes(request);
    if (folder === "closed") {
      await page.addInitScript((id) => {
        localStorage.setItem("stemdeck.takeFolders", JSON.stringify({ [id]: false }));
      }, trackId);
    }
    await page.setViewportSize({ width: 1600, height: 1000 });
    await openStudio(page);
    await waitForEngine(page);
    await expect(page.locator(`.stem-waveform-row[data-stem="${trackId}"] .stem-waveform-svg`)).toBeAttached({ timeout: 15000 });
    await expect(page.locator("#waveLoadingOverlay")).toBeHidden({ timeout: 20000 });
    const toggle = page.locator(`.take-folder-toggle[data-track="${trackId}"]`);
    const takeRows = page.locator(`.mixer-column .take-folder-row[data-folder-of="${trackId}"]`);
    await expect(toggle).toHaveAttribute("aria-expanded", folder === "open" ? "true" : "false");
    await expect(takeRows).toHaveCount(folder === "open" ? 2 : 0);
    await watchHandoff(page);

    // Record: the only track is armed by Record.
    await recBtn(page).click();
    await expect(recBtn(page)).toHaveClass(/recording/, { timeout: 10000 });
    await untilSongTime(page, 1.6);
    const during = await liveOverMain(page, trackId);
    if (folder === "open") {
      await page.screenshot({ path: `${process.env.SHOT_DIR || testInfo.outputDir}/recording-folder-open-2-takes.png` });
    }
    expect(during.slot, "the main lane has its slot").toBe(true);
    expect(during.lane, "a live lane while recording").toBe(true);
    expect(during.pixels, "live bars drawn").toBeGreaterThan(0);
    expect(during.dTop, "live lane top on the main lane").toBeLessThanOrEqual(1);
    expect(during.dH, "live lane height is the main lane's").toBeLessThanOrEqual(1);
    expect(during.oldBarsHidden, "the previous take's bars are hidden on the main lane").toBe(true);
    // The take lanes keep their own bars and stay aligned while recording.
    expectAligned(await stack(page));
    if (folder === "open") {
      await expect(page.locator(`.take-folder-lane[data-folder-of="${trackId}"] .stem-waveform-svg`)).toHaveCount(2);
    }

    await untilSongTime(page, 2.8);
    await recBtn(page).click();
    await expect(recBtn(page)).not.toHaveClass(/recording/);
    await expect.poll(() => log.finishStatus.at(-1), { timeout: 20000 }).toBe(200);
    await expect(page.locator("#recKeep")).toBeVisible({ timeout: 20000 });
    await expect(page.locator(".live-wave-lane")).toHaveCount(0, { timeout: 20000 });
    expectNoJump(await page.evaluate(() => window.__live));

    // The new take plays, and is the newest in the folder.
    const job = await jobState(page);
    const recorded = job.tracks[0].active_take_id;
    expect(takes).not.toContain(recorded);
    await waitForEngine(page);
    await expect(toggle.locator(".take-folder-count")).toHaveText("3", { timeout: 15000 });
    if (folder === "open") {
      await expect(takeRows).toHaveCount(3);
      const ids = await takeRows.evaluateAll((els) => els.map((e) => e.dataset.takeId));
      expect(ids).toEqual([recorded, takes[1], takes[0]]);
      await expect(takeRows.first()).toHaveClass(/playing/);
      await expect(page.locator(`.take-folder-lane[data-take-id="${recorded}"]`)).toHaveClass(/playing/);
    } else {
      await expect(takeRows).toHaveCount(0);
      await expect(toggle).toHaveAttribute("aria-expanded", "false");
    }
    await expect(page.locator(`.stem-waveform-row[data-stem="${trackId}"]`)).not.toHaveClass(/take-recording/);
    await expect.poll(async () => (await stack(page)).wave.length, { timeout: 10000 })
      .toBe(folder === "open" ? 8 : 5);
    expectAligned(await stack(page));
    await page.locator("#recKeep").click();
    await expect(page.locator(".live-wave-lane")).toHaveCount(0);
  });
}
