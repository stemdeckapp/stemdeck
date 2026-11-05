// Performance's own track (static/js/modeStems.js shownTracks): the video
// track Performance records its takes into is a lane in Performance only.
// Practice and Advanced show no row, take folder or take for it and do not
// load it (so it is not heard and not exported there); Performance shows it
// as before. Switching keeps the page, the take that plays, the folder's open
// state, the track's mute in Performance's mix and the arm of other tracks.
//
// The takes are made through the takes API, as take-folders.spec.mjs does.
import { test, expect } from "@playwright/test";
import { openStudio, chooseMode, JOB_ID } from "./helpers.mjs";
import { removeTracks, waitForEngine } from "./recording-helpers.mjs";

test.use({
  launchOptions: {
    args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--mute-audio"],
  },
  permissions: ["microphone", "camera"],
});

test.afterEach(async ({ request }) => removeTracks(request));

const RATE = 48000;

function tone(seconds, amp) {
  const n = Math.round(RATE * seconds);
  const buf = Buffer.alloc(n * 4);
  for (let i = 0; i < n; i++) buf.writeFloatLE(amp * Math.sin((2 * Math.PI * 220 * i) / RATE), i * 4);
  return buf;
}

async function makeTrack(request, data = {}) {
  const r = await request.post(`/api/jobs/${JOB_ID}/tracks`, { data });
  expect(r.status()).toBe(201);
  return (await r.json()).id;
}

async function makeTake(request, trackId, amp) {
  const r = await request.post(`/api/jobs/${JOB_ID}/takes`, { data: { track_id: trackId } });
  expect(r.status()).toBe(201);
  const takeId = (await r.json()).take_id;
  const part = await request.put(`/api/jobs/${JOB_ID}/takes/${takeId}/parts/audio?seq=0`, {
    data: tone(3, amp),
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

const trackRow = (page, id) => page.locator(`.mixer-column .lane-header[data-take="${id}"]`);
const waveRow = (page, id) => page.locator(`.stem-waveform-row[data-stem="${id}"]`);
const takeRows = (page, id) => page.locator(`.mixer-column .take-folder-row[data-folder-of="${id}"]`);
const takeLanes = (page, id) => page.locator(`.stem-waveform-row.take-folder-lane[data-folder-of="${id}"]`);
const loaded = (page, id) => page.evaluate(async (n) => (await import("/js/state.js")).trackIndex[n] !== undefined, id);
const muted = (page, id) => page.evaluate(async (n) => Boolean((await import("/js/state.js")).mixerState[n]?.muted), id);
const marker = (page) => page.evaluate(() => window.__noReload === true);

/** No row, folder, take or lane of `id`, and it is not loaded (not heard). */
async function expectHidden(page, id) {
  await expect(trackRow(page, id)).toHaveCount(0, { timeout: 20000 });
  await expect(waveRow(page, id)).toHaveCount(0);
  await expect(takeRows(page, id)).toHaveCount(0);
  await expect(takeLanes(page, id)).toHaveCount(0);
  await waitForEngine(page);
  expect(await loaded(page, id)).toBe(false);
}

async function openStudioAt(page, trackId) {
  await page.setViewportSize({ width: 1600, height: 1000 });
  await openStudio(page);
  await waitForEngine(page);
  await expect(waveRow(page, trackId)).toHaveCount(1, { timeout: 15000 });
  await page.evaluate(() => { window.__noReload = true; });
}

async function openPerformance(page) {
  await chooseMode(page, "streamer");
  await expect(page.locator("#streamer")).toBeVisible();
}

test("Performance takes show in Performance only; switching keeps the page, the take, the folder, the mute and the arm", async ({ page, request }) => {
  // Performance's track as another browser recorded it (no kept id here):
  // a video track called "Performance", two takes, the older one playing.
  const perf = await makeTrack(request, { kind: "video", name: "Performance" });
  const older = await makeTake(request, perf, 0.3);
  await makeTake(request, perf, 0.6);
  expect((await request.patch(`/api/jobs/${JOB_ID}/tracks/${perf}`, { data: { active_take_id: older } })).status()).toBe(200);
  // An ordinary recording track: every mode keeps showing it.
  const own = await makeTrack(request);
  await makeTake(request, own, 0.5);

  await openStudioAt(page, own);

  // Practice (the default).
  await expectHidden(page, perf);
  await expect(trackRow(page, own)).toHaveCount(1);

  // Advanced: still hidden, no reload; the ordinary track is armed.
  await chooseMode(page, "daw");
  await expect(page.locator("html")).toHaveClass(/mode-daw/);
  await expectHidden(page, perf);
  await expect(trackRow(page, own)).toHaveCount(1);
  await page.locator(`.track-arm[data-track="${own}"]`).click();
  await expect(page.locator(`.track-arm[data-track="${own}"]`)).toHaveAttribute("aria-pressed", "true");

  // Performance: the track, its open folder (two takes) and the older take playing.
  await openPerformance(page);
  await expect(trackRow(page, perf)).toHaveCount(1, { timeout: 20000 });
  await waitForEngine(page);
  await expect(takeRows(page, perf)).toHaveCount(2);
  await expect(takeLanes(page, perf)).toHaveCount(2);
  await expect(page.locator(`.take-folder-row.playing[data-folder-of="${perf}"]`)).toHaveAttribute("data-take-id", older);
  await expect.poll(() => loaded(page, perf)).toBe(true);
  // Performance's own mute on the track, and its folder closed.
  await page.evaluate(async (n) => (await import("/js/mixer.js")).toggleStemMute(n), perf);
  expect(await muted(page, perf)).toBe(true);
  await page.evaluate(async (n) => (await import("/js/capture/takeLanes.js")).setFolderOpen(n, false), perf);
  await expect(takeRows(page, perf)).toHaveCount(0);

  // Back to Advanced: hidden again, the ordinary track still armed.
  await chooseMode(page, "daw");
  await expect(page.locator("#streamer")).toBeHidden();
  await expectHidden(page, perf);
  await expect(trackRow(page, own)).toHaveCount(1);
  await expect(page.locator(`.track-arm[data-track="${own}"]`)).toHaveAttribute("aria-pressed", "true");

  // Practice: hidden.
  await chooseMode(page, "practice");
  await expect(page.locator("html")).toHaveClass(/mode-practice/);
  await expectHidden(page, perf);

  // Performance again: the folder is still closed, the mute still on, and
  // opened it shows the same two takes with the same one playing.
  await openPerformance(page);
  await expect(trackRow(page, perf)).toHaveCount(1, { timeout: 20000 });
  await waitForEngine(page);
  await expect(page.locator(`.take-folder-toggle[data-track="${perf}"]`)).toHaveAttribute("aria-expanded", "false");
  await expect(takeRows(page, perf)).toHaveCount(0);
  await expect.poll(() => muted(page, perf)).toBe(true);
  await page.evaluate(async (n) => (await import("/js/capture/takeLanes.js")).setFolderOpen(n, true), perf);
  await expect(takeRows(page, perf)).toHaveCount(2);
  await expect(page.locator(`.take-folder-row.playing[data-folder-of="${perf}"]`)).toHaveAttribute("data-take-id", older);
  const job = await (await request.get(`/api/jobs/${JOB_ID}`)).json();
  expect(job.tracks.find((tr) => tr.id === perf)?.active_take_id).toBe(older);
  expect(job.takes.filter((tk) => tk.track_id === perf)).toHaveLength(2);

  // The page never reloaded.
  expect(await marker(page)).toBe(true);
});

test("the track this browser kept for Performance is hidden whatever its name; other video tracks stay", async ({ page, request }) => {
  const perf = await makeTrack(request, { kind: "video", name: "Stage" });
  await makeTake(request, perf, 0.4);
  const camera = await makeTrack(request, { kind: "video", name: "Camera" });
  await makeTake(request, camera, 0.4);
  await page.addInitScript(([job, id]) => {
    try { localStorage.setItem(`stemdeck:streamer:track:${job}`, id); } catch { /* the test fails below */ }
  }, [JOB_ID, perf]);

  await openStudioAt(page, camera);
  await expectHidden(page, perf);
  await chooseMode(page, "daw");
  await expectHidden(page, perf);
  await expect(trackRow(page, camera)).toHaveCount(1);
  await openPerformance(page);
  await expect(trackRow(page, perf)).toHaveCount(1, { timeout: 20000 });
  await expect(trackRow(page, camera)).toHaveCount(1);
  expect(await marker(page)).toBe(true);
});

// The timeline's take lanes (streamer.js paintTakeRows): every take of the
// Performance track is its own row under the take lane, oldest on top. A
// click on a row (or its label) makes that take the one that plays.
const labelRows = (page) => page.locator("#streamer .sm-tl-takes-labels .sm-tl-take-label");
const waveRows = (page) => page.locator("#streamer .sm-tl-takes .sm-tl-take-row");
const takeLabel = (page, id) => page.locator(`#streamer .sm-tl-take-label[data-take-id="${id}"]`);
const takeWave = (page, id) => page.locator(`#streamer .sm-tl-take-row[data-take-id="${id}"]`);
const laneShows = (page, id) => page.locator(`#streamer .sm-tl-lane-1[data-take="${id}"]`);
const activeOnServer = async (request, id) => {
  const job = await (await request.get(`/api/jobs/${JOB_ID}`)).json();
  return job.tracks.find((tr) => tr.id === id)?.active_take_id ?? null;
};
const ids = (loc) => loc.evaluateAll((els) => els.map((e) => e.dataset.takeId));
const keepTake = (page, track, take) => page.addInitScript(([job, id, tk]) => {
  try {
    localStorage.setItem(`stemdeck:streamer:track:${job}`, id);
    localStorage.setItem(`stemdeck:streamer:take:${job}`, tk);
  } catch { /* the test fails below */ }
}, [JOB_ID, track, take]);

/** Only `id`'s row is marked as playing; every row is still there. */
async function expectPlaying(page, all, id) {
  for (const other of all) {
    const on = other === id;
    await expect(takeLabel(page, other)).toHaveAttribute("aria-pressed", String(on), { timeout: 20000 });
    if (on) await expect(takeWave(page, other)).toHaveClass(/(^| )playing( |$)/);
    else await expect(takeWave(page, other)).not.toHaveClass(/(^| )playing( |$)/);
  }
}

test("Performance stacks every take as its own row, oldest on top, and a click on a row makes that take play", async ({ page, request }) => {
  const perf = await makeTrack(request, { kind: "video", name: "Performance" });
  const oldest = await makeTake(request, perf, 0.3);
  const middle = await makeTake(request, perf, 0.45);
  const newest = await makeTake(request, perf, 0.6);
  const all = [oldest, middle, newest];
  // This browser recorded the newest take, and it plays (finish made it active).
  expect(await activeOnServer(request, perf)).toBe(newest);
  await keepTake(page, perf, newest);

  await page.setViewportSize({ width: 1600, height: 1000 });
  await openStudio(page);
  await waitForEngine(page);
  await page.evaluate(() => { window.__noReload = true; });
  await openPerformance(page);

  // One row per take, oldest first, labels and waves in the same order.
  await expect(waveRows(page)).toHaveCount(3, { timeout: 20000 });
  await expect(labelRows(page)).toHaveCount(3);
  expect(await ids(waveRows(page))).toEqual(all);
  expect(await ids(labelRows(page))).toEqual(all);
  await expect(labelRows(page).nth(0)).toHaveText(/1/);
  await expect(labelRows(page).nth(2)).toHaveText(/3/);
  await expectPlaying(page, all, newest);
  await expect(laneShows(page, newest)).toHaveCount(1, { timeout: 20000 });
  // Each label sits level with its wave, and the rows are small.
  for (const id of all) {
    const a = await takeLabel(page, id).boundingBox();
    const b = await takeWave(page, id).boundingBox();
    expect(Math.abs(a.y - b.y)).toBeLessThan(2);
    expect(b.height).toBeLessThan(30);
  }

  // A click on the oldest row's wave: the server has it, the take lane draws
  // it, and the mark moves to its row.
  await takeWave(page, oldest).click();
  await expect.poll(() => activeOnServer(request, perf), { timeout: 15000 }).toBe(oldest);
  await expectPlaying(page, all, oldest);
  await expect(laneShows(page, oldest)).toHaveCount(1, { timeout: 20000 });
  await expect(page.locator(`.take-folder-row.playing[data-folder-of="${perf}"]`)).toHaveAttribute("data-take-id", oldest);

  // A click on a label does the same; the order never changes.
  await takeLabel(page, middle).click();
  await expect.poll(() => activeOnServer(request, perf), { timeout: 15000 }).toBe(middle);
  await expectPlaying(page, all, middle);
  await expect(laneShows(page, middle)).toHaveCount(1, { timeout: 20000 });
  expect(await ids(waveRows(page))).toEqual(all);

  // The Guide explains a row, by its wave and by its label, and a click
  // there plays nothing.
  await page.locator("#helpModeBtn").click();
  await expect(page.locator("#helpModeBtn")).toHaveAttribute("aria-pressed", "true");
  await takeWave(page, newest).hover();
  await expect(page.locator("#helpTip")).toContainText("oldest on top");
  await expect(page.locator("#helpTip")).toContainText("Play Take 3");
  await takeLabel(page, oldest).click();
  await expect(page.locator("#helpTip")).toContainText("Take 1");
  await takeLabel(page, newest).hover();
  await takeDelete(page, newest).click();
  await expect(page.locator("#helpTip")).toContainText("cannot be undone");
  await expect(page.locator("#helpTip")).toContainText("Delete Take 3");
  await page.locator("#helpModeBtn").click();
  expect(await takesOnServer(request, perf)).toEqual(all);
  expect(await activeOnServer(request, perf)).toBe(middle);
  expect(await marker(page)).toBe(true);
});

test("the take lane follows a take chosen elsewhere", async ({ page, request }) => {
  const perf = await makeTrack(request, { kind: "video", name: "Performance" });
  const older = await makeTake(request, perf, 0.3);
  const newer = await makeTake(request, perf, 0.6);
  // This browser kept the newer take; another one made the older one play.
  expect((await request.patch(`/api/jobs/${JOB_ID}/tracks/${perf}`, { data: { active_take_id: older } })).status()).toBe(200);
  await keepTake(page, perf, newer);
  await page.setViewportSize({ width: 1600, height: 1000 });
  await openStudio(page);
  await waitForEngine(page);
  await openPerformance(page);
  await expect(waveRows(page)).toHaveCount(2, { timeout: 20000 });
  await expectPlaying(page, [older, newer], older);
  await expect(laneShows(page, older)).toHaveCount(1, { timeout: 20000 });
});

test("many takes scroll inside the timeline, and a new take's row scrolls into view", async ({ page, request }) => {
  const perf = await makeTrack(request, { kind: "video", name: "Performance" });
  const made = [];
  for (let i = 0; i < 8; i++) made.push(await makeTake(request, perf, 0.2 + i * 0.05));
  await page.setViewportSize({ width: 1280, height: 720 });
  await openStudio(page);
  await waitForEngine(page);
  await page.evaluate(() => { window.__noReload = true; });
  await openPerformance(page);
  await expect(waveRows(page)).toHaveCount(8, { timeout: 20000 });
  expect(await ids(waveRows(page))).toEqual(made);

  // The rows keep their height; the timeline scrolls, the page does not grow.
  const heights = await waveRows(page).evaluateAll((els) => els.map((e) => e.getBoundingClientRect().height));
  expect(Math.max(...heights) - Math.min(...heights)).toBeLessThan(1);
  const scroll = page.locator("#streamer .sm-tl-scroll");
  expect(await scroll.evaluate((e) => e.scrollHeight > e.clientHeight + 10)).toBe(true);
  const pageGrew = () => page.evaluate(() => document.documentElement.scrollHeight > window.innerHeight + 1);
  expect(await pageGrew()).toBe(false);
  const inView = async (id) => {
    const box = await scroll.boundingBox();
    const r = await takeWave(page, id).boundingBox();
    return r.y >= box.y - 1 && r.y + r.height <= box.y + box.height + 1;
  };
  // Shown anew, the row that plays (the newest) is in view.
  await expect.poll(() => inView(made[7])).toBe(true);

  // Scrolled back to the top, a take recorded now adds a row at the bottom
  // and scrolls it into view.
  await scroll.evaluate((e) => { e.scrollTop = 0; });
  await expect.poll(() => inView(made[7])).toBe(false);
  const added = await makeTake(request, perf, 0.7);
  await page.evaluate(async (job) => (await import("/js/capture/takeLanes.js")).requestTakesReload(job), JOB_ID);
  await expect(waveRows(page)).toHaveCount(9, { timeout: 20000 });
  expect(await ids(waveRows(page))).toEqual([...made, added]);
  await expect.poll(() => inView(added), { timeout: 10000 }).toBe(true);
  await expectPlaying(page, [...made, added], added);
  expect(await pageGrew()).toBe(false);
  expect(await marker(page)).toBe(true);
});

test("a song with one take shows no take rows", async ({ page, request }) => {
  const perf = await makeTrack(request, { kind: "video", name: "Performance" });
  await makeTake(request, perf, 0.4);
  await openStudio(page);
  await waitForEngine(page);
  await openPerformance(page);
  await expect(trackRow(page, perf)).toHaveCount(1, { timeout: 20000 });
  await waitForEngine(page);
  await expect(waveRows(page)).toHaveCount(0);
  await expect(page.locator("#streamer .sm-tl-takes")).toBeHidden();
  await expect(page.locator("#streamer .sm-tl-takes-labels")).toBeHidden();
});

// Each row's Delete (streamer.js buildTakeRows): two presses, no undo. The
// server makes the newest remaining take the one that plays.
const takeDelete = (page, id) => page.locator(`#streamer .sm-tl-take-del[data-take-id="${id}"]`);
const takesOnServer = async (request, id) => {
  const job = await (await request.get(`/api/jobs/${JOB_ID}`)).json();
  return job.takes.filter((tk) => tk.track_id === id).map((tk) => tk.id);
};

test("a row's Delete removes its take after a second press; deleting the playing take plays the newest one left", async ({ page, request }) => {
  const perf = await makeTrack(request, { kind: "video", name: "Performance" });
  const oldest = await makeTake(request, perf, 0.3);
  const middle = await makeTake(request, perf, 0.45);
  const newest = await makeTake(request, perf, 0.6);
  await keepTake(page, perf, newest);
  await page.setViewportSize({ width: 1600, height: 1000 });
  await openStudio(page);
  await waitForEngine(page);
  await page.evaluate(() => { window.__noReload = true; });
  await openPerformance(page);
  await expect(waveRows(page)).toHaveCount(3, { timeout: 20000 });
  await expectPlaying(page, [oldest, middle, newest], newest);

  // Hidden until the row is pointed at; the first press only asks.
  await expect(takeDelete(page, middle)).toHaveCSS("opacity", "0");
  await takeLabel(page, middle).hover();
  await expect(takeDelete(page, middle)).toHaveCSS("opacity", "1");
  await takeDelete(page, middle).click();
  await expect(takeDelete(page, middle)).toHaveText("Delete? Press again");
  expect(await takesOnServer(request, perf)).toEqual([oldest, middle, newest]);
  // The press on Delete played nothing.
  expect(await activeOnServer(request, perf)).toBe(newest);
  await takeDelete(page, middle).click();
  await expect.poll(() => takesOnServer(request, perf), { timeout: 15000 }).toEqual([oldest, newest]);
  await expect(waveRows(page)).toHaveCount(2, { timeout: 20000 });
  expect(await ids(waveRows(page))).toEqual([oldest, newest]);
  await expectPlaying(page, [oldest, newest], newest);
  await expect(labelRows(page).nth(1)).toHaveText(/2/);

  // By keyboard: the playing take. The newest left (the oldest) plays, the
  // rows hide with one take, and the take lane shows it.
  await takeDelete(page, newest).focus();
  await page.keyboard.press("Enter");
  await expect(takeDelete(page, newest)).toHaveText("Delete? Press again");
  await page.keyboard.press("Enter");
  await expect.poll(() => takesOnServer(request, perf), { timeout: 15000 }).toEqual([oldest]);
  expect(await activeOnServer(request, perf)).toBe(oldest);
  await expect(waveRows(page)).toHaveCount(0, { timeout: 20000 });
  await expect(page.locator("#streamer .sm-tl-takes")).toBeHidden();
  await expect(laneShows(page, oldest)).toHaveCount(1, { timeout: 20000 });

  // The last take gone too (deleted elsewhere): Performance stays usable.
  expect((await request.delete(`/api/jobs/${JOB_ID}/takes/${oldest}`)).status()).toBeLessThan(300);
  await page.evaluate(async (job) => (await import("/js/capture/takeLanes.js")).requestTakesReload(job), JOB_ID);
  await expect.poll(() => takesOnServer(request, perf), { timeout: 15000 }).toEqual([]);
  await expect(waveRows(page)).toHaveCount(0);
  await expect(page.locator("#streamer .sm-rec")).toBeEnabled();
  await expect(page.locator("#streamer")).toBeVisible();
  expect(await marker(page)).toBe(true);
});
