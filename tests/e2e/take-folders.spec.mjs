// Take folders (static/js/capture/takeLanes.js, takeFolderModel.js, and the
// take lanes player.js draws). A recording track is a folder of its takes: the
// main row plays one of them, and the arrow on the row opens one lane per take
// under the track, newest on top, each with its own mixer row and waveform.
//
// The takes are made through the real takes API (create, one audio part,
// finish), not by recording: three recordings would take most of the test's
// time and prove nothing more about the folder. One test does record, to show
// a fresh recording lands in the folder. Each take is a tone of a different
// level, so their waveforms differ and "the main row follows" can be seen in
// the drawing, not only in the request log.
import { test, expect } from "@playwright/test";
import { openStudio, JOB_ID } from "./helpers.mjs";
import { trackRows, removeTracks, jobState, waitForEngine, recordForAWhile, watchTakes } from "./recording-helpers.mjs";

test.use({
  launchOptions: {
    args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--mute-audio"],
  },
  permissions: ["microphone"],
});

test.afterEach(async ({ request }) => removeTracks(request));

const RATE = 48000;

/** A mono float32 tone, `seconds` long at amplitude `amp`, as the recorder sends it. */
function tone(seconds, amp) {
  const n = Math.round(RATE * seconds);
  const buf = Buffer.alloc(n * 4);
  for (let i = 0; i < n; i++) buf.writeFloatLE(amp * Math.sin((2 * Math.PI * 220 * i) / RATE), i * 4);
  return buf;
}

async function makeTrack(request) {
  const r = await request.post(`/api/jobs/${JOB_ID}/tracks`, { data: {} });
  expect(r.status()).toBe(201);
  return (await r.json()).id;
}

/** One finished take on `trackId`; it becomes the track's active take. */
async function makeTake(request, trackId, { amp = 0.5, seconds = 3 } = {}) {
  const r = await request.post(`/api/jobs/${JOB_ID}/takes`, { data: { track_id: trackId } });
  expect(r.status()).toBe(201);
  const takeId = (await r.json()).take_id;
  const part = await request.put(`/api/jobs/${JOB_ID}/takes/${takeId}/parts/audio?seq=0`, {
    data: tone(seconds, amp),
    headers: { "content-type": "application/octet-stream" },
  });
  expect(part.status()).toBe(200);
  const fin = await request.post(`/api/jobs/${JOB_ID}/takes/${takeId}/finish`, {
    data: {
      v: 1,
      timeline: { first_sample_song_sec: 0.5, tempo: 1.0 },
      latency: { round_trip_ms: 0, source: "estimated", user_nudge_ms: 0 },
      capture: { rate: RATE, channels: 1, device_label: "E2E tone" },
    },
  });
  expect(fin.status(), await fin.text()).toBe(200);
  return takeId;
}

/** A track with `levels.length` takes, oldest first. Returns ids. */
async function trackWithTakes(request, levels) {
  const trackId = await makeTrack(request);
  const takes = [];
  for (const amp of levels) takes.push(await makeTake(request, trackId, { amp }));
  return { trackId, takes };
}

const toggle = (page, trackId) => page.locator(`.take-folder-toggle[data-track="${trackId}"]`);
const takeRows = (page, trackId) => page.locator(`.mixer-column .take-folder-row[data-folder-of="${trackId}"]`);
const takeLanes = (page, trackId) => page.locator(`.stem-waveform-row.take-folder-lane[data-folder-of="${trackId}"]`);

/**
 * Every visible mixer row and waveform lane, top to bottom, with what each
 * one is (a stem, a track, or a take).
 */
async function stack(page) {
  return page.evaluate(() => {
    const visible = (sel) => [...document.querySelectorAll(sel)]
      .filter((e) => !e.classList.contains("hidden") && e.getBoundingClientRect().height > 0)
      .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
    const box = (e) => {
      const b = e.getBoundingClientRect();
      return { top: Math.round(b.top), h: Math.round(b.height) };
    };
    const id = (e) => e.dataset.takeId || e.dataset.stem;
    const mix = visible(".mixer-column .lane-header");
    const wave = visible(".stem-waveform-layer > .stem-waveform-row");
    return {
      mix: mix.map((e) => ({ id: id(e), ...box(e) })),
      wave: wave.map((e) => ({ id: id(e), ...box(e) })),
      count: Number(getComputedStyle(document.querySelector(".app")).getPropertyValue("--visible-track-count")),
    };
  });
}

function expectAligned(g) {
  expect(g.wave.map((r) => r.id)).toEqual(g.mix.map((r) => r.id));
  expect(g.count).toBe(g.mix.length);
  for (let i = 0; i < g.mix.length; i++) {
    expect(Math.abs(g.mix[i].top - g.wave[i].top), `row ${i} (${g.mix[i].id}) top`).toBeLessThanOrEqual(1);
  }
}

/** The bars of a lane's waveform, as drawn (the heights of its rects). */
async function bars(page, sel) {
  return page.locator(sel).first().evaluate((row) => [...row.querySelectorAll(".stem-waveform-svg rect")]
    .map((r) => Number(r.getAttribute("height"))));
}

/** Open the studio on the fixture job and wait for the lanes to be drawn. */
async function openWithLanes(page, trackId, opts) {
  await page.setViewportSize({ width: 1600, height: 1000 });
  await openStudio(page, opts);
  await waitForEngine(page);
  await expect(page.locator(`.stem-waveform-row[data-stem="${trackId}"]`)).toHaveCount(1, { timeout: 15000 });
}

test("one take: the folder starts closed, the arrow opens and closes it, aligned both ways", async ({ page, request }) => {
  const { trackId, takes } = await trackWithTakes(request, [0.5]);
  await openWithLanes(page, trackId);

  await expect(toggle(page, trackId)).toHaveAttribute("aria-expanded", "false");
  await expect(toggle(page, trackId)).toBeEnabled();
  await expect(takeRows(page, trackId)).toHaveCount(0);
  await expect(takeLanes(page, trackId)).toHaveCount(0);
  let g = await stack(page);
  expect(g.mix.map((r) => r.id)).toEqual(["vocals", "drums", "bass", "other", trackId]);
  expectAligned(g);

  await toggle(page, trackId).click();
  await expect(toggle(page, trackId)).toHaveAttribute("aria-expanded", "true");
  await expect(takeRows(page, trackId)).toHaveCount(1);
  await expect(takeLanes(page, trackId)).toHaveCount(1);
  await expect.poll(async () => (await stack(page)).wave.length).toBe(6);
  g = await stack(page);
  expect(g.mix.map((r) => r.id)).toEqual(["vocals", "drums", "bass", "other", trackId, takes[0]]);
  expectAligned(g);
  // Every row is one lane high, the take rows too.
  expect(new Set(g.mix.map((r) => r.h)).size).toBe(1);

  await toggle(page, trackId).click();
  await expect(takeRows(page, trackId)).toHaveCount(0);
  await expect(takeLanes(page, trackId)).toHaveCount(0);
  expectAligned(await stack(page));
});

test("three takes: open by default, newest on top, the playing one marked, every row on its lane", async ({ page, request }) => {
  const { trackId, takes } = await trackWithTakes(request, [0.2, 0.5, 0.9]);
  const [t1, t2, t3] = takes;
  await openWithLanes(page, trackId);

  await expect(toggle(page, trackId)).toHaveAttribute("aria-expanded", "true");
  await expect(toggle(page, trackId).locator(".take-folder-count")).toHaveText("3");
  await expect(takeRows(page, trackId)).toHaveCount(3);
  const rowIds = await takeRows(page, trackId).evaluateAll((els) => els.map((e) => e.dataset.takeId));
  expect(rowIds).toEqual([t3, t2, t1]);
  await expect(takeRows(page, trackId).locator(".take-folder-name")).toHaveText(["Take 3", "Take 2", "Take 1"]);
  // The newest is the take that plays: it was finished last.
  await expect(takeRows(page, trackId).nth(0)).toHaveClass(/playing/);
  await expect(takeRows(page, trackId).nth(0).locator(".take-play")).toHaveAttribute("aria-pressed", "true");
  await expect(takeRows(page, trackId).nth(1).locator(".take-play")).toHaveAttribute("aria-pressed", "false");

  await expect.poll(async () => (await stack(page)).wave.length, { timeout: 10000 }).toBe(8);
  const g = await stack(page);
  expect(g.mix.map((r) => r.id)).toEqual(["vocals", "drums", "bass", "other", trackId, t3, t2, t1]);
  expectAligned(g);
  // Each lane draws its own take: three levels, three different drawings.
  const drawn = await Promise.all([t3, t2, t1].map((id) => bars(page, `.take-folder-lane[data-take-id="${id}"]`)));
  expect(Math.max(...drawn[0])).toBeGreaterThan(Math.max(...drawn[1]));
  expect(Math.max(...drawn[1])).toBeGreaterThan(Math.max(...drawn[2]));
  await expect(page.locator(`.take-folder-lane[data-take-id="${t3}"]`)).toHaveClass(/playing/);

  // Aligned at other window heights too, including one where the stack overflows.
  for (const height of [720, 853, 1150]) {
    await page.setViewportSize({ width: 1600, height });
    await page.waitForTimeout(400);
    expectAligned(await stack(page));
  }
});

test("open or closed is remembered per track across a reload", async ({ page, request }) => {
  const a = await trackWithTakes(request, [0.3, 0.6]);
  const b = await trackWithTakes(request, [0.4, 0.8]);
  await openWithLanes(page, b.trackId);
  await expect(toggle(page, a.trackId)).toHaveAttribute("aria-expanded", "true");
  await expect(toggle(page, b.trackId)).toHaveAttribute("aria-expanded", "true");
  await toggle(page, a.trackId).click();
  await expect(takeRows(page, a.trackId)).toHaveCount(0);
  expectAligned(await stack(page));

  await page.reload();
  await openWithLanes(page, b.trackId);
  await expect(toggle(page, a.trackId)).toHaveAttribute("aria-expanded", "false");
  await expect(toggle(page, b.trackId)).toHaveAttribute("aria-expanded", "true");
  await expect(takeRows(page, b.trackId)).toHaveCount(2);
  await expect.poll(async () => (await stack(page)).wave.length, { timeout: 10000 }).toBe(8);
  const g = await stack(page);
  expect(g.mix.map((r) => r.id)).toEqual(["vocals", "drums", "bass", "other", a.trackId, b.trackId, ...[...b.takes].reverse()]);
  expectAligned(g);
});

test("a folder still opens and closes when storage is blocked", async ({ page, request }) => {
  await page.addInitScript(() => {
    const blocked = (k) => String(k).startsWith("stemdeck.takeFolders");
    const get = Storage.prototype.getItem;
    const set = Storage.prototype.setItem;
    Storage.prototype.getItem = function (k) { if (blocked(k)) throw new Error("storage blocked"); return get.call(this, k); };
    Storage.prototype.setItem = function (k, v) { if (blocked(k)) throw new Error("storage blocked"); return set.call(this, k, v); };
  });
  const { trackId } = await trackWithTakes(request, [0.3, 0.6]);
  await openWithLanes(page, trackId);
  await expect(takeRows(page, trackId)).toHaveCount(2);
  await toggle(page, trackId).click();
  await expect(takeRows(page, trackId)).toHaveCount(0);
  expectAligned(await stack(page));
  await toggle(page, trackId).click();
  await expect(takeRows(page, trackId)).toHaveCount(2);
});

test("clicking a take lane makes it the take that plays, and the main row follows", async ({ page, request }) => {
  const log = watchTakes(page);
  const { trackId, takes } = await trackWithTakes(request, [0.2, 0.5, 0.9]);
  const [t1, t2] = takes;
  await openWithLanes(page, trackId, { tauri: true });
  const main = `.stem-waveform-row[data-stem="${trackId}"]`;
  const lane1 = `.take-folder-lane[data-take-id="${t1}"]`;
  await expect.poll(() => bars(page, lane1), { timeout: 10000 }).not.toEqual([]);
  const before = await bars(page, main);

  // A click on the oldest take's waveform.
  const reloaded = page.waitForRequest((r) => r.url().includes(`/takes/${t1}/audio.wav`), { timeout: 20000 });
  await page.locator(lane1).click();
  await expect.poll(() => log.trackPatches.length).toBe(1);
  expect(log.trackPatches[0]).toEqual({ active_take_id: t1 });
  await reloaded;
  await waitForEngine(page);
  await expect(page.locator(`.take-folder-row[data-take-id="${t1}"]`)).toHaveClass(/playing/, { timeout: 15000 });
  await expect(page.locator(lane1)).toHaveClass(/playing/);
  // The track's own lane now draws the take that plays, bar for bar.
  await expect.poll(async () => JSON.stringify(await bars(page, main)), { timeout: 10000 })
    .toBe(JSON.stringify(await bars(page, lane1)));
  expect(await bars(page, main)).not.toEqual(before);
  // The folder stayed open through the reload, still newest on top.
  await expect(takeRows(page, trackId)).toHaveCount(3);
  expectAligned(await stack(page));

  // The play control on a row does the same.
  await page.locator(`.take-folder-row[data-take-id="${t2}"] .take-play`).click();
  await expect.poll(async () => (await jobState(page)).tracks[0].active_take_id, { timeout: 10000 }).toBe(t2);
  await expect(page.locator(`.take-folder-row[data-take-id="${t2}"] .take-play`)).toHaveAttribute("aria-pressed", "true", { timeout: 15000 });

  // Export still names the track, never a take.
  await waitForEngine(page);
  await page.locator("#t-export-btn").click();
  await page.locator("#t-export-mix").click();
  await page.evaluate(() => window.__e2e.choosePath());
  await expect.poll(() => page.evaluate(() => window.__e2e.callsFor("download_to_path").length)).toBe(1);
  const url = new URL(await page.evaluate(() => window.__e2e.callsFor("download_to_path")[0].args.url));
  const stems = url.searchParams.get("stems").split(",");
  expect(stems).toContain(trackId);
  for (const id of takes) expect(stems).not.toContain(id);
});

test("take rows carry no mute or solo; the track's M and S work as before", async ({ page, request }) => {
  const { trackId, takes } = await trackWithTakes(request, [0.3, 0.6]);
  await openWithLanes(page, trackId);
  await expect(takeRows(page, trackId)).toHaveCount(2);
  await expect(takeRows(page, trackId).locator(".mute, .solo, .mx-fader, .lane-dl")).toHaveCount(0);
  // Only the track is a playing lane; its takes are not.
  const lanes = await page.evaluate(async () => Object.keys((await import("/js/state.js")).trackIndex));
  expect(lanes).toContain(trackId);
  for (const id of takes) expect(lanes).not.toContain(id);

  const row = page.locator(`.mixer-column .lane-header[data-take="${trackId}"]`);
  const flag = (key) => page.evaluate(async ([id, k]) => (await import("/js/state.js")).mixerState[id]?.[k], [trackId, key]);
  await row.locator(".mute").click();
  await expect.poll(() => flag("muted")).toBe(true);
  await row.locator(".mute").click();
  await expect.poll(() => flag("muted")).toBe(false);
  await row.locator(".solo").click();
  await expect.poll(() => flag("soloed")).toBe(true);
  // Soloing the track did not touch the folder.
  await expect(takeRows(page, trackId)).toHaveCount(2);
  expectAligned(await stack(page));
});

test("the track menu keeps only track actions; a take's own menu has rename, nudge and delete", async ({ page, request }) => {
  const { trackId } = await trackWithTakes(request, [0.3, 0.6]);
  await openWithLanes(page, trackId);
  await page.locator(`.lane-header[data-take="${trackId}"] .take-menu-btn`).click();
  const menu = page.locator(".take-menu");
  await expect(menu.locator(".take-name-input")).toHaveValue("Track 1");
  await expect(menu.locator(".take-delete-track")).toBeVisible();
  await expect(menu.locator(".take-menu-takes, .take-nudge-input, .take-delete-take")).toHaveCount(0);
  await page.keyboard.press("Escape");

  await takeRows(page, trackId).nth(1).locator(".take-folder-menu-btn").click();
  await expect(menu.locator(".take-name-input")).toHaveValue("Take 1");
  await expect(menu.locator(".take-nudge-input")).toBeVisible();
  await expect(menu.locator(".take-delete-take")).toBeVisible();
  await expect(menu.locator(".take-delete-track")).toHaveCount(0);
});

test("nudge and rename apply to the take on that row, not the one that plays", async ({ page, request }) => {
  const log = watchTakes(page);
  const { trackId, takes } = await trackWithTakes(request, [0.2, 0.5, 0.9]);
  const [t1, , t3] = takes;
  await openWithLanes(page, trackId);

  const patched = page.waitForRequest((r) => r.method() === "PATCH" && r.url().endsWith(`/takes/${t1}`));
  await page.locator(`.take-folder-row[data-take-id="${t1}"] .take-folder-menu-btn`).click();
  const menu = page.locator(".take-menu");
  await menu.locator(".take-name-input").fill("Keeper");
  await menu.locator(".take-nudge-input").fill("12");
  await menu.locator(".take-apply").click();
  await patched;
  expect(log.patches).toEqual([{ label: "Keeper", user_nudge_ms: 12 }]);

  await expect.poll(async () => (await jobState(page)).takes.find((tk) => tk.id === t1)?.user_nudge_ms).toBe(12);
  const job = await jobState(page);
  const byId = Object.fromEntries(job.takes.map((tk) => [tk.id, tk]));
  expect(byId[t1].label).toBe("Keeper");
  expect(byId[t3].user_nudge_ms).toBe(0);
  expect(job.tracks[0].active_take_id).toBe(t3);
  await waitForEngine(page);
  const row = page.locator(`.take-folder-row[data-take-id="${t1}"]`);
  await expect(row.locator(".take-folder-name")).toHaveText("Keeper", { timeout: 15000 });
  await expect(row.locator(".take-folder-meta")).toHaveText("nudge +12 ms");
  await expect(row).not.toHaveClass(/playing/);
});

test("delete a take from its row: the folder and its lanes lose that take, the rest stay aligned", async ({ page, request }) => {
  const log = watchTakes(page);
  const { trackId, takes } = await trackWithTakes(request, [0.2, 0.5, 0.9]);
  const [t1, t2, t3] = takes;
  await openWithLanes(page, trackId);

  await page.locator(`.take-folder-row[data-take-id="${t2}"] .take-folder-menu-btn`).click();
  const del = page.locator(".take-menu .take-delete-take");
  await del.click();
  await expect(del).toHaveClass(/armed/);
  await del.click();
  await expect.poll(() => log.deletes).toEqual([t2]);
  await expect(takeRows(page, trackId)).toHaveCount(2, { timeout: 15000 });
  await expect(takeLanes(page, trackId)).toHaveCount(2, { timeout: 10000 });
  const ids = await takeRows(page, trackId).evaluateAll((els) => els.map((e) => e.dataset.takeId));
  expect(ids).toEqual([t3, t1]);
  expectAligned(await stack(page));

  // Deleting the take that plays hands the track to the newest one left.
  await waitForEngine(page);
  await page.locator(`.take-folder-row[data-take-id="${t3}"] .take-folder-menu-btn`).click();
  await del.click();
  await del.click();
  await expect.poll(async () => (await jobState(page)).tracks[0].active_take_id, { timeout: 10000 }).toBe(t1);
  // One take left, after the folder was open in this page: it stays open.
  await expect(takeRows(page, trackId)).toHaveCount(1, { timeout: 15000 });
  await expect(takeRows(page, trackId).first()).toHaveClass(/playing/, { timeout: 15000 });
  await expect.poll(async () => (await stack(page)).wave.length, { timeout: 10000 }).toBe(6);
  expectAligned(await stack(page));
});

test("a new recording lands in the folder as the newest take, and plays", async ({ page, request }) => {
  test.setTimeout(60_000);
  const log = watchTakes(page);
  const { trackId, takes } = await trackWithTakes(request, [0.4]);
  await openWithLanes(page, trackId);
  // The live waveform's anchor is in the track's own lane.
  const slot = page.locator(`.stem-waveform-row[data-stem="${trackId}"] > .take-live-slot[data-track-id="${trackId}"]`);
  await expect(slot).toHaveCount(1);

  await recordForAWhile(page, 1500);
  await expect.poll(() => log.finishStatus.length, { timeout: 20000 }).toBe(1);
  await page.locator("#recKeep").click();
  await page.keyboard.press("Escape");
  await waitForEngine(page);
  const job = await jobState(page);
  const recorded = job.tracks[0].active_take_id;
  expect(recorded).not.toBe(takes[0]);

  // Two takes now: the folder opens, the recording on top, playing.
  await expect(toggle(page, trackId)).toHaveAttribute("aria-expanded", "true", { timeout: 15000 });
  const ids = await takeRows(page, trackId).evaluateAll((els) => els.map((e) => e.dataset.takeId));
  expect(ids).toEqual([recorded, takes[0]]);
  await expect(takeRows(page, trackId).first()).toHaveClass(/playing/);
  await expect.poll(async () => (await stack(page)).wave.length, { timeout: 10000 }).toBe(7);
  expectAligned(await stack(page));
  await expect(slot).toHaveCount(1);
});

test("the live waveform slot survives a redraw of the lanes", async ({ page, request }) => {
  const { trackId } = await trackWithTakes(request, [0.3, 0.6]);
  await openWithLanes(page, trackId);
  const slot = `.take-live-slot[data-track-id="${trackId}"]`;
  await page.locator(slot).evaluate((el) => { el.dataset.e2eMark = "kept"; });
  // Closing the folder redraws every lane.
  await toggle(page, trackId).click();
  await expect(takeRows(page, trackId)).toHaveCount(0);
  await expect(page.locator(slot)).toHaveAttribute("data-e2e-mark", "kept");
  await expect(page.locator(slot)).toHaveCSS("pointer-events", "none");
});

test("Guide explains the arrow, a take's controls and a take lane, and a click there explains instead of acting", async ({ page, request }) => {
  const log = watchTakes(page);
  const { trackId, takes } = await trackWithTakes(request, [0.3, 0.6]);
  await openWithLanes(page, trackId);
  await page.locator("#helpModeBtn").click();
  await expect(page.locator("body")).toHaveClass(/help-mode/);
  const tip = page.locator("#helpTip");

  await toggle(page, trackId).hover();
  await expect(tip).toContainText("Shows or hides the takes of Track 1");
  await page.locator(`.take-folder-row[data-take-id="${takes[0]}"] .take-play`).hover();
  await expect(tip).toContainText("Makes Take 1 the take its track plays");
  await page.locator(`.take-folder-row[data-take-id="${takes[0]}"] .take-folder-menu-btn`).hover();
  await expect(tip).toContainText("Rename Take 1, nudge it in time, or delete it.");
  const lane = page.locator(`.take-folder-lane[data-take-id="${takes[0]}"]`);
  await lane.hover();
  await expect(tip).toContainText("One take of this track");
  await lane.click();
  await toggle(page, trackId).click();
  // Explained, not acted on: the folder is still open and no take was switched.
  await expect(takeRows(page, trackId)).toHaveCount(2);
  expect(log.trackPatches).toEqual([]);
});

test("on the streaming path the folder stays shut and the track menu lists the takes", async ({ page, request }) => {
  await page.addInitScript(() => {
    try { localStorage.setItem("stemdeck.audioEngine", "0"); } catch (e) { console.warn(e); }
  });
  const { trackId } = await trackWithTakes(request, [0.3, 0.6]);
  await page.setViewportSize({ width: 1600, height: 1000 });
  await openStudio(page);
  await expect(toggle(page, trackId)).toHaveClass(/off/);
  await expect(takeRows(page, trackId)).toHaveCount(0);
  await page.locator(`.lane-header[data-take="${trackId}"] .take-menu-btn`).click();
  await expect(page.locator(".take-menu .take-menu-take")).toHaveText(["Take 1", "Take 2"]);
});

