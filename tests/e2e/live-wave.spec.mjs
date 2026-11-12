// The live waveform of a take while it records (static/js/capture/liveWave.js).
//
// Chromium's fake capture device plays a beep, so the lane must show bars
// that grow while recording, ending where the playhead is (less the batch and
// round-trip lag), at every zoom; and must be replaced by the server's bars
// after Stop without a gap, and be gone on discard or a failed take.
//
// The fixture song is 6 seconds, which bounds how long a take can run here.
import { test, expect } from "@playwright/test";
import { openStudio } from "./helpers.mjs";
import {
  recBtn, trackRows, removeTracks, addTrack, waitForEngine, pressRecord, watchTakes,
} from "./recording-helpers.mjs";

test.use({
  launchOptions: {
    args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--mute-audio"],
  },
  permissions: ["microphone"],
});

test.afterEach(async ({ request }) => removeTracks(request));

// How far the live edge may trail the playhead: a batch (85 ms), the round
// trip estimate, a frame, and the scheduling of a headless run.
const MAX_LAG_SEC = 0.45;

function watchConsole(page) {
  const errors = [];
  // Network refusals the recording flow expects (a part the server already
  // has answers 409) are logged by the browser as resource errors; they are
  // not script errors.
  page.on("console", (m) => { if (m.type() === "error" && !/^Failed to load resource/.test(m.text())) errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push(String(e)));
  return errors;
}

/** What the live lane of `trackId` shows right now, measured from its pixels. */
async function sample(page, trackId) {
  return page.evaluate(async (id) => {
    const st = await import("/js/state.js");
    const lane = document.querySelector(`.live-wave-lane[data-track-id="${id}"]`);
    const canvas = lane?.querySelector("canvas");
    if (!canvas || !canvas.width) {
      const lw = await import("/js/capture/liveWave.js");
      // Why there is nothing to measure, for the failure message.
      return {
        missing: JSON.stringify({
          stats: lw.getLiveWaveStats(),
          duration: st.totalDuration,
          now: st.audioEngine?.getCurrentTime(),
          lanes: document.querySelectorAll(".live-wave-lane").length,
        }),
      };
    }
    const g = canvas.getContext("2d");
    const { data, width, height } = g.getImageData(0, 0, canvas.width, canvas.height);
    let pixels = 0;
    let right = -1;
    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        if (data[(y * width + x) * 4 + 3] > 0) {
          pixels++;
          if (x > right) right = x;
        }
      }
    }
    const cssW = lane.clientWidth;
    const scale = width / cssW;
    const row = document.querySelector(`.stem-waveform-row[data-stem="${id}"]`);
    const lr = lane.getBoundingClientRect();
    const rr = row?.getBoundingClientRect();
    const marker = document.querySelector(".playhead-marker");
    const markerPct = parseFloat(marker?.style.left || "NaN");
    const now = st.audioEngine.getCurrentTime();
    return {
      pixels,
      rightCss: right < 0 ? -1 : (right + 1) / scale,
      cssW,
      duration: st.totalDuration,
      now,
      playheadX: (now / st.totalDuration) * cssW,
      markerX: (markerPct / 100) * cssW,
      laneTop: lr.top,
      laneH: lr.height,
      rowTop: rr ? rr.top : NaN,
      rowH: rr ? row.clientHeight * (rr.height / row.offsetHeight) : NaN,
      liveStats: (await import("/js/capture/liveWave.js")).getLiveWaveStats(),
    };
  }, trackId);
}

/** The live edge sits on the playhead, within the lag and one bar slot. */
function expectAligned(s, label) {
  const slot = s.cssW / Math.max(40, Math.round(s.cssW / 5));
  const pxPerSec = s.cssW / s.duration;
  const gap = s.playheadX - s.rightCss;
  expect(gap, `${label}: live edge ${s.rightCss.toFixed(1)} px vs playhead ${s.playheadX.toFixed(1)} px`)
    .toBeGreaterThanOrEqual(-slot - 1);
  expect(gap, `${label}: live edge trails the playhead by too much`)
    .toBeLessThanOrEqual(MAX_LAG_SEC * pxPerSec + slot + 1);
  // The ruler's playhead marker says the same as the engine.
  expect(Math.abs(s.markerX - s.playheadX), `${label}: marker vs engine`).toBeLessThanOrEqual(pxPerSec * 0.1 + 1);
  // Over the track's own lane.
  expect(Math.abs(s.laneTop - s.rowTop), `${label}: lane top`).toBeLessThanOrEqual(1);
  expect(Math.abs(s.laneH - s.rowH), `${label}: lane height`).toBeLessThanOrEqual(1);
}

/** Add a track and wait until the reload it causes has fully settled. */
async function addSettledTrack(page) {
  const id = await addTrack(page);
  await waitForEngine(page);
  await expect(page.locator(`.stem-waveform-row[data-stem="${id}"]`)).toBeAttached({ timeout: 20000 });
  await expect(page.locator("#waveLoadingOverlay")).toBeHidden({ timeout: 20000 });
  return id;
}

// waitForFunction does not await an async predicate (a Promise is truthy),
// so the state module is put on window once and read synchronously.
async function exposeState(page) {
  await page.evaluate(async () => { window.__e2eState = await import("/js/state.js"); });
}

async function untilPlaying(page) {
  await exposeState(page);
  await page.waitForFunction(() => window.__e2eState.audioEngine?.isPlaying(), null, { timeout: 10000 });
}

async function untilSongTime(page, sec) {
  await page.waitForFunction((t) => window.__e2eState.audioEngine?.getCurrentTime() >= t, sec, { timeout: 10000 });
}

test("the take's waveform grows in its lane while recording, on the playhead, then hands over to the server's", async ({ page }, testInfo) => {
  test.setTimeout(90_000);
  await page.setViewportSize({ width: 1600, height: 1000 });
  const errors = watchConsole(page);
  const log = watchTakes(page);
  await openStudio(page);
  const trackId = await addSettledTrack(page);

  // Record whether the server's bars were already drawn at the moment the
  // live lane went (no gap), and what the live bars were then (no jump).
  await page.evaluate(() => {
    window.__live = { removedWithBars: null, handoff: null };
    document.addEventListener("stemdeck:live-wave-handoff", (e) => { window.__live.handoff = e.detail; });
    new MutationObserver((muts) => {
      for (const m of muts) {
        for (const n of m.removedNodes) {
          if (n.classList?.contains("live-wave-lane")) {
            const id = n.dataset.trackId;
            const row = document.querySelector(`.stem-waveform-row[data-stem="${id}"]`);
            const rects = row ? [...row.querySelectorAll("rect")] : [];
            window.__live.removedWithBars = rects.length;
            // The server's bars as drawn at that moment, in CSS px.
            window.__live.server = {
              bars: rects.length,
              heights: rects.map((r) => (parseFloat(r.getAttribute("height")) / 48) * row.clientHeight),
            };
          }
        }
      }
    }).observe(document.querySelector(".waves-column"), { childList: true });
  });

  await pressRecord(page);
  await untilPlaying(page);
  const shots = process.env.LIVEWAVE_SHOT_DIR || testInfo.outputDir;

  await untilSongTime(page, 1.4);
  const a = await sample(page, trackId);
  await page.screenshot({ path: `${shots}/live-wave-1.png` });
  await untilSongTime(page, 2.6);
  const b = await sample(page, trackId);
  await page.screenshot({ path: `${shots}/live-wave-2.png` });

  expect(a.missing, "a live canvas exists while recording").toBeUndefined();
  expect(a.pixels).toBeGreaterThan(0);
  expect(b.pixels, "the waveform grows").toBeGreaterThan(a.pixels);
  expect(b.rightCss, "the right edge moves on").toBeGreaterThan(a.rightCss);
  expectAligned(a, "zoom 1, first sample");
  expectAligned(b, "zoom 1, second sample");

  // Zoom in while recording: the bars are redrawn at the new width, still on
  // the playhead.
  await page.evaluate(async () => (await import("/js/transport.js")).setWaveZoomLevel(3));
  await untilSongTime(page, 3.6);
  const c = await sample(page, trackId);
  await page.screenshot({ path: `${shots}/live-wave-zoom3.png` });
  expect(c.cssW).toBeGreaterThan(b.cssW * 2);
  expectAligned(c, "zoom 3");

  // Cost: a frame redraws one or two bars.
  const cost = c.liveStats;
  const mean = cost.drawMs / Math.max(1, cost.frames);
  testInfo.annotations.push({ type: "live-wave cost", description: `frames ${cost.frames}, mean ${mean.toFixed(3)} ms, max ${cost.maxDrawMs.toFixed(3)} ms, full redraws ${cost.fullRedraws}, batches ${cost.batches}, batch mean ${(cost.batchMs / Math.max(1, cost.batches)).toFixed(3)} ms, batch max ${cost.maxBatchMs.toFixed(3)} ms` });
  console.log(`live-wave cost: ${testInfo.annotations.at(-1).description}`);
  expect(cost.frames).toBeGreaterThan(10);
  expect(mean, "mean draw per frame").toBeLessThan(2);
  expect(cost.maxDrawMs, "worst draw per frame").toBeLessThan(8);

  // Stop: the live lane stays until the reload has drawn the server's bars.
  await recBtn(page).click();
  await expect(recBtn(page)).not.toHaveClass(/recording/);
  await expect.poll(() => log.finishStatus.at(-1), { timeout: 20000 }).toBe(200);
  await expect(page.locator("#recKeep")).toBeVisible({ timeout: 20000 });
  await expect(page.locator(".live-wave-lane")).toHaveCount(0, { timeout: 20000 });
  const live = await page.evaluate(() => window.__live);
  expect(live.removedWithBars, "the server's bars were drawn when the live lane went").toBeGreaterThan(0);

  // No jump: bar for bar, the live heights match the server's.
  expect(live.handoff).not.toBeNull();
  // Compared with the server's bars as they were at that moment: the reload
  // redraws them again later at its own zoom.
  const { server } = live;
  expect(server.bars).toBe(live.handoff.bars);
  const heights = Object.values(live.handoff.heights);
  // The first and last bar of the take are partly filled on one side and
  // partly padded on the other, so compare the bars inside it.
  const drawn = heights.map((v, i) => (v >= 0 ? i : -1)).filter((i) => i >= 0).slice(1, -1);
  const meanDiff = (shift) => {
    let sum = 0;
    let n = 0;
    for (const i of drawn) {
      const j = i + shift;
      if (j < 0 || j >= server.heights.length) continue;
      sum += Math.abs(heights[i] - server.heights[j]);
      n++;
    }
    return sum / Math.max(1, n);
  };
  const close = drawn.filter((i) => Math.abs(heights[i] - server.heights[i]) <= 2).length;
  console.log(`live-wave handoff: ${drawn.length} bars, ${close} within 2 px, mean diff ${meanDiff(0).toFixed(2)} px (shift -1: ${meanDiff(-1).toFixed(2)}, +1: ${meanDiff(1).toFixed(2)}) on a ${live.handoff.height} px lane`);
  expect(drawn.length).toBeGreaterThan(20);
  // In time: no other bar offset matches better than none.
  expect(meanDiff(0)).toBeLessThan(meanDiff(-1));
  expect(meanDiff(0)).toBeLessThan(meanDiff(1));
  // In height: the live points are cut on the server's own grid, so the
  // bars match; a beep edge may still land a sample apart after the take is
  // resampled to the stems' rate.
  expect(close / drawn.length, "share of bars within 2 px").toBeGreaterThan(0.95);
  expect(meanDiff(0), "mean live vs server bar height, px").toBeLessThan(0.5);

  // Keep: the server's waveform stays, no live lane comes back.
  await page.locator("#recKeep").click();
  await expect(page.locator(".live-wave-lane")).toHaveCount(0);
  await expect(page.locator(`.stem-waveform-row[data-stem="${trackId}"] rect`).first()).toBeAttached();
  expect(errors, errors.join("\n")).toEqual([]);
});

test("discard leaves no live waveform and an empty lane", async ({ page }) => {
  test.setTimeout(60_000);
  const errors = watchConsole(page);
  const log = watchTakes(page);
  await openStudio(page);
  const trackId = await addSettledTrack(page);
  await pressRecord(page);
  await untilPlaying(page);
  await untilSongTime(page, 1.2);
  await expect(page.locator(`.live-wave-lane[data-track-id="${trackId}"]`)).toHaveCount(1);
  await recBtn(page).click();
  await expect.poll(() => log.finishStatus.at(-1), { timeout: 20000 }).toBe(200);
  await expect(page.locator("#recDiscard")).toBeVisible({ timeout: 20000 });
  await page.locator("#recDiscard").click();
  await expect.poll(() => log.deletes.length, { timeout: 10000 }).toBe(1);
  await waitForEngine(page);
  await expect(page.locator(".live-wave-lane")).toHaveCount(0);
  // The track has no take again: its lane draws nothing.
  await expect.poll(() => page.locator(`.stem-waveform-row[data-stem="${trackId}"] rect`).count(), { timeout: 10000 }).toBe(0);
  expect(errors, errors.join("\n")).toEqual([]);
});

test("a take that fails takes its live waveform with it at once", async ({ page }) => {
  test.setTimeout(60_000);
  await openStudio(page);
  const trackId = await addSettledTrack(page);
  // The server refuses to finish the take.
  await page.route(/\/takes\/t[0-9a-f]{8}\/finish$/, (route) => route.fulfill({ status: 500, body: "{}" }));
  await pressRecord(page);
  await untilPlaying(page);
  await untilSongTime(page, 1.2);
  await expect(page.locator(`.live-wave-lane[data-track-id="${trackId}"]`)).toHaveCount(1);
  await recBtn(page).click();
  await expect(page.locator(".live-wave-lane")).toHaveCount(0, { timeout: 20000 });
  await expect(page.locator("#recKeep")).toBeHidden();
});

test("two armed tracks each get their own live waveform, over their own lane", async ({ page }) => {
  test.setTimeout(60_000);
  const log = watchTakes(page);
  await openStudio(page);
  const a = await addSettledTrack(page);
  const b = await addSettledTrack(page);
  // Input 1 and Input 2 of the same stereo device.
  for (const [i, label] of [[0, "Input 1"], [1, "Input 2"]]) {
    const sel = trackRows(page).nth(i).locator(".track-input");
    await expect.poll(() => sel.locator("option").count(), { timeout: 10000 }).toBeGreaterThan(2);
    const value = await sel.locator("option", { hasText: new RegExp(`^Fake Audio Input 1 · ${label}$`) }).getAttribute("value");
    await sel.selectOption(value);
  }
  await trackRows(page).nth(0).locator(".track-arm").click();
  await trackRows(page).nth(1).locator(".track-arm").click();
  await recBtn(page).click();
  await expect(recBtn(page)).toHaveClass(/recording/);
  await untilPlaying(page);
  await untilSongTime(page, 1.5);
  const sa = await sample(page, a);
  const sb = await sample(page, b);
  for (const [s, label] of [[sa, "track 1"], [sb, "track 2"]]) {
    expect(s.missing, label).toBeUndefined();
    expect(s.pixels, label).toBeGreaterThan(0);
    expectAligned(s, label);
  }
  expect(sb.laneTop).toBeGreaterThan(sa.laneTop);
  await recBtn(page).click();
  await expect.poll(() => log.finishStatus.length, { timeout: 20000 }).toBe(2);
  await expect(page.locator(".live-wave-lane")).toHaveCount(0, { timeout: 20000 });
});
