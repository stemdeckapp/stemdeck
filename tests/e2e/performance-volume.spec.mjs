// Performance's volumes (static/js/streamer/levelSlider.js, streamer.js lane
// volumes, streamerFx.js Monitor level), against the real backend:
//
// 1. The Backing track's slider and the take's slider sit in their lane
//    labels. They are the Studio fader's stored level (mixerState[lane].volume)
//    in Performance's own record of the song: the Backing track's sets every
//    lane heard but the take, the take's the Performance track's lane only.
// 2. Keyboard: arrows, Shift, Page keys, Home and End; aria-valuetext in dB.
//    Pointer: a press along the slider sets the level there; a double-click
//    returns it to 0 dB.
// 3. The export's render request carries the same volumes (the backing
//    lanes' gains and take_gain), so what is exported is what is heard.
// 4. Both survive a reload of the page. Practice keeps its own mix (each mode
//    keeps its own record of a song, mixer.js), so its fader is not moved.
// 5. The Monitor level: default 0 dB, capped at 0 dB, kept after a reload,
//    heard as the capture's monitor gain while Hear myself is on, and Hear
//    myself is still off at every load.
// 6. The Guide explains all three.
//
// SHOTS_DIR, when set, saves the timeline and the Effects tab at 1240x1010,
// 1024x768 and 390 wide (for a person to look at).
//
// What this cannot check: how the levels sound, and that a change while
// playing does not click (the engines glide each gain with setTargetAtTime,
// which a human hears, the 6 s fixture does not measure).
import { test, expect } from "@playwright/test";
import path from "node:path";
import { openStudio, chooseMode, JOB_ID } from "./helpers.mjs";
import { removeTracks, waitForEngine } from "./recording-helpers.mjs";
import { TRANSLATIONS } from "../../static/js/i18n.js";

test.use({
  launchOptions: {
    args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--mute-audio"],
  },
  permissions: ["microphone", "camera"],
});

const RATE = 48000;
const sm = (page, sel) => page.locator(`#streamer ${sel}`);
const backing = (page) => sm(page, ".sm-tl-vol-backing");
const take = (page) => sm(page, ".sm-tl-vol-take");
const monitor = (page) => page.locator("#streamerMonitorLevel");

function tone(seconds, amp) {
  const n = Math.round(RATE * seconds);
  const buf = Buffer.alloc(n * 4);
  for (let i = 0; i < n; i++) buf.writeFloatLE(amp * Math.sin((2 * Math.PI * 220 * i) / RATE), i * 4);
  return buf;
}

/** Performance's track with one take, as this browser recorded it. */
async function makePerformanceTake(page, request) {
  const tr = await request.post(`/api/jobs/${JOB_ID}/tracks`, { data: { kind: "video", name: "Performance" } });
  expect(tr.status()).toBe(201);
  const trackId = (await tr.json()).id;
  const r = await request.post(`/api/jobs/${JOB_ID}/takes`, { data: { track_id: trackId } });
  expect(r.status()).toBe(201);
  const takeId = (await r.json()).take_id;
  const part = await request.put(`/api/jobs/${JOB_ID}/takes/${takeId}/parts/audio?seq=0`, {
    data: tone(3, 0.4),
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
  // The keys streamerRecord.js keeps for the song's Performance track and
  // its last take (as a take recorded here leaves them).
  await page.addInitScript(({ job, trackId: t, takeId: k }) => {
    localStorage.setItem(`stemdeck:streamer:track:${job}`, t);
    localStorage.setItem(`stemdeck:streamer:take:${job}`, k);
  }, { job: JOB_ID, trackId, takeId });
  return { trackId, takeId };
}

async function openPerformance(page) {
  await chooseMode(page, "streamer");
  await expect(page.locator("#streamer")).toBeVisible();
  await waitForEngine(page);
}

/** The Studio levels: { lanes: {name: volume}, track } for the open song. */
const levels = (page, trackId) => page.evaluate(async (id) => {
  const st = await import("/js/state.js");
  const lanes = {};
  for (const n of Object.keys(st.trackIndex)) lanes[n] = st.mixerState[n]?.volume;
  return { lanes, track: st.mixerState[id]?.volume, loaded: Object.keys(st.trackIndex) };
}, trackId);

const backingVolumes = async (page, trackId) => {
  const l = await levels(page, trackId);
  return Object.entries(l.lanes).filter(([n]) => n !== trackId).map(([, v]) => v);
};

/** Wait for every backing lane to be at `v`. */
async function expectBacking(page, trackId, v) {
  await expect.poll(async () => {
    const vols = await backingVolumes(page, trackId);
    return vols.length > 0 && vols.every((x) => Math.abs(x - v) < 1e-6);
  }).toBe(true);
}

async function reopen(page) {
  await page.reload({ waitUntil: "domcontentloaded" });
  const item = page.locator(`.cat-item[data-id="${JOB_ID}"]`).first();
  await item.click();
  await page.waitForFunction(
    () => !/\/\s*00:00\s*$/.test(document.querySelector("#t-time")?.textContent || "00:00 / 00:00"),
    null,
    { timeout: 20000 },
  );
  if (!(await page.locator("#streamer").isVisible())) await chooseMode(page, "streamer");
  await expect(page.locator("#streamer")).toBeVisible();
  await waitForEngine(page);
}

test.afterEach(async ({ request }) => removeTracks(request));

test("the Backing track and take volumes set the Studio levels, export them and survive a reload", async ({ page, request }) => {
  await page.setViewportSize({ width: 1240, height: 1010 });
  const { trackId } = await makePerformanceTake(page, request);
  await openStudio(page);
  await waitForEngine(page);
  await openPerformance(page);

  // 1. Both sliders show, in their labels, at 0 dB.
  await expect(backing(page)).toBeVisible({ timeout: 20000 });
  await expect(take(page)).toBeVisible({ timeout: 20000 });
  await expect(sm(page, ".sm-tl-label-stem .sm-tl-vol-backing")).toHaveCount(1);
  await expect(sm(page, ".sm-tl-label-stem .sm-tl-vol-take")).toHaveCount(1);
  for (const s of [backing(page), take(page)]) {
    await expect(s).toHaveAttribute("role", "slider");
    await expect(s).toHaveAttribute("aria-valuemin", "0");
    await expect(s).toHaveAttribute("aria-valuemax", "2");
    await expect(s).toHaveAttribute("aria-valuenow", "1.00");
    await expect(s).toHaveAttribute("aria-valuetext", "0.0 dB");
  }
  await expect(backing(page)).toHaveAttribute("aria-label", TRANSLATIONS.en["streamer.lane.backingVolume"]);
  await expect(take(page)).toHaveAttribute("aria-label", TRANSLATIONS.en["streamer.lane.takeVolume"]);
  await expect(backing(page)).toHaveAttribute("title", "Backing track volume: 0.0 dB. Double-click for 0 dB.");
  // Inside the label column: no wider than the label, no overflow.
  const box = await page.evaluate(() => {
    const s = document.querySelector("#streamer .sm-tl-vol-backing").getBoundingClientRect();
    const l = document.querySelector("#streamer .sm-tl-vol-backing").closest(".sm-tl-label").getBoundingClientRect();
    return { s: [s.left, s.right, s.top, s.bottom], l: [l.left, l.right, l.top, l.bottom] };
  });
  expect(box.s[0]).toBeGreaterThanOrEqual(box.l[0] - 0.5);
  expect(box.s[1]).toBeLessThanOrEqual(box.l[1] + 0.5);
  expect(box.s[3]).toBeLessThanOrEqual(box.l[3] + 0.5);

  // 2. Keyboard on the Backing track: every backing lane follows; the take does not.
  await backing(page).focus();
  for (let i = 0; i < 5; i++) await page.keyboard.press("ArrowLeft");
  await expectBacking(page, trackId, 0.8);
  await expect(backing(page)).toHaveAttribute("aria-valuetext", "-1.9 dB");
  expect((await levels(page, trackId)).track).toBe(1);
  await page.keyboard.press("Shift+ArrowLeft");
  await expectBacking(page, trackId, 0.6);
  await page.keyboard.press("End");
  await expectBacking(page, trackId, 2);
  await expect(backing(page)).toHaveAttribute("aria-valuetext", "+6.0 dB");
  await page.keyboard.press("ArrowRight");
  await expectBacking(page, trackId, 2);
  await page.keyboard.press("Home");
  await expectBacking(page, trackId, 0);
  await expect(backing(page)).toHaveAttribute("aria-valuetext", "-∞ dB");
  // The arrows did not move the playhead (the slider keeps them).
  // Double-click: back to 0 dB.
  await backing(page).dblclick();
  await expectBacking(page, trackId, 1);
  // A press a quarter along the track: -6 dB (0.5).
  const tr = await backing(page).locator(".sm-level-track").boundingBox();
  await page.mouse.click(tr.x + tr.width * 0.25, tr.y + tr.height / 2);
  await expectBacking(page, trackId, 0.5);
  await expect(backing(page)).toHaveAttribute("aria-valuetext", "-6.0 dB");

  // The take's slider: its track's lane only.
  await take(page).focus();
  await page.keyboard.press("PageDown");
  await page.keyboard.press("PageDown");
  await expect.poll(async () => (await levels(page, trackId)).track).toBeCloseTo(0.6, 6);
  await expectBacking(page, trackId, 0.5);
  await expect(take(page)).toHaveAttribute("aria-valuetext", "-4.4 dB");

  // 3. The export's request carries what is heard.
  const body = await page.evaluate(async (id) => {
    const ex = await import("/js/streamer/streamerExport.js");
    return ex.renderRequest({ trackId: id, settings: { recordMixedAudio: true, resolution: "1080", fps: 30 }, aspect: "16:9", range: null });
  }, trackId);
  expect(body.take_gain).toBeCloseTo(0.6, 6);
  expect(body.stems).not.toContain(trackId);
  expect(body.stems.length).toBeGreaterThan(0);
  for (const g of body.gains) expect(g).toBeCloseTo(0.5, 6);
  const alone = await page.evaluate(async (id) => {
    const ex = await import("/js/streamer/streamerExport.js");
    return ex.renderRequest({ trackId: id, settings: { recordMixedAudio: false, resolution: "1080", fps: 30 }, aspect: "16:9", range: null });
  }, trackId);
  expect(alone.take_gain).toBe(1);
  expect(alone.stems).toEqual([]);

  // 4. A reload keeps both.
  await page.waitForTimeout(800);
  await reopen(page);
  await expect(backing(page)).toHaveAttribute("aria-valuenow", "0.50", { timeout: 20000 });
  await expect(take(page)).toHaveAttribute("aria-valuenow", "0.60", { timeout: 20000 });
  await expectBacking(page, trackId, 0.5);

  // Practice keeps its own mix: its fader for a stem is not moved.
  await chooseMode(page, "practice");
  await expect(page.locator("#streamer")).toBeHidden();
  await waitForEngine(page);
  await expect(page.locator('.mixer-column .lane-knob[data-stem="drums"] .mx-fader-input')).toHaveValue("1");
  // And back in Performance, Performance's level is there again.
  await openPerformance(page);
  await expect(backing(page)).toHaveAttribute("aria-valuenow", "0.50", { timeout: 20000 });
});

test("the Monitor level: 0 dB by default and at most, kept, heard while Hear myself is on, which is off at load", async ({ page, request }) => {
  await page.setViewportSize({ width: 1240, height: 1010 });
  await makePerformanceTake(page, request);
  await openStudio(page);
  await waitForEngine(page);
  await openPerformance(page);
  await page.locator("#streamerTab-effects").click();
  await expect(monitor(page)).toBeVisible();
  await expect(page.locator("#streamerHear")).toHaveAttribute("aria-checked", "false");
  await expect(monitor(page)).toHaveAttribute("aria-valuemax", "1");
  await expect(monitor(page)).toHaveAttribute("aria-valuenow", "1.00");
  await expect(monitor(page)).toHaveAttribute("aria-label", TRANSLATIONS.en["streamer.fx.monitorLevel"]);
  // The cap: End and Right stay at 0 dB.
  await monitor(page).focus();
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("End");
  await expect(monitor(page)).toHaveAttribute("aria-valuenow", "1.00");
  // Down to 0.6 (-4.4 dB): Hear myself stays off.
  await page.keyboard.press("PageDown");
  await page.keyboard.press("PageDown");
  await expect(monitor(page)).toHaveAttribute("aria-valuenow", "0.60");
  await expect(monitor(page)).toHaveAttribute("aria-valuetext", "-4.4 dB");
  await expect(page.locator(".sm-monitor-level-value")).toHaveText("-4.4 dB");
  await expect(page.locator("#streamerHear")).toHaveAttribute("aria-checked", "false");

  // Hear myself on: the capture's monitor gain glides to the level; a move follows.
  await page.locator("#streamerHear").click();
  await expect(page.locator("#streamerHear")).toHaveAttribute("aria-checked", "true");
  const gain = () => page.evaluate(async () => {
    const m = await import("/js/streamer/streamerDevices.js");
    return m.getStreamerMedia().capture?.monitorNode?.gain.value ?? null;
  });
  await expect.poll(gain, { timeout: 15000 }).toBeGreaterThan(0.55);
  expect(await gain()).toBeLessThan(0.65);
  await monitor(page).focus();
  await page.keyboard.press("Home");
  await expect.poll(gain).toBeLessThan(0.01);
  await page.keyboard.press("End");
  await expect.poll(gain).toBeGreaterThan(0.95);
  expect(await gain()).toBeLessThanOrEqual(1.0001);
  await page.keyboard.press("PageDown");
  await page.keyboard.press("PageDown");
  await expect.poll(gain).toBeLessThan(0.65);

  // A reload: the level is kept, Hear myself is off.
  await page.waitForTimeout(500);
  await reopen(page);
  await page.locator("#streamerTab-effects").click();
  await expect(monitor(page)).toHaveAttribute("aria-valuenow", "0.60");
  await expect(page.locator("#streamerHear")).toHaveAttribute("aria-checked", "false");
  await expect.poll(gain).toBeLessThan(0.01);
  // Double-click: back to 0 dB.
  await monitor(page).dblclick();
  await expect(monitor(page)).toHaveAttribute("aria-valuenow", "1.00");
  await expect(page.locator("#streamerHear")).toHaveAttribute("aria-checked", "false");
});

test("the Guide explains the three volumes, and they fit at each window size", async ({ page, request }) => {
  await page.setViewportSize({ width: 1240, height: 1010 });
  await makePerformanceTake(page, request);
  await openStudio(page);
  await waitForEngine(page);
  await openPerformance(page);
  await expect(take(page)).toBeVisible({ timeout: 20000 });
  await page.evaluate(async () => (await import("/js/helpMode.js")).setHelpMode(true));
  const tip = page.locator("#helpTip");
  await backing(page).hover();
  await expect(tip).toContainText(TRANSLATIONS.en["help.smBackingVolume"]);
  await take(page).hover();
  await expect(tip).toContainText(TRANSLATIONS.en["help.smTakeVolume"]);
  // The Guide takes clicks while it is on: off to change tabs, then on again.
  await page.evaluate(async () => (await import("/js/helpMode.js")).setHelpMode(false));
  await page.locator("#streamerTab-effects").click();
  await expect(monitor(page)).toBeVisible();
  await page.evaluate(async () => (await import("/js/helpMode.js")).setHelpMode(true));
  await monitor(page).hover();
  await expect(tip).toContainText(TRANSLATIONS.en["help.smMonitorLevel"]);
  await page.locator("#streamerMonitorLevelLabel").hover();
  await expect(tip).toContainText(TRANSLATIONS.en["help.smMonitorLevel"]);
  await page.evaluate(async () => (await import("/js/helpMode.js")).setHelpMode(false));
  for (const key of ["streamer.lane.backingVolume", "streamer.lane.takeVolume", "streamer.vol.db", "streamer.vol.title", "streamer.fx.monitorLevel", "help.smBackingVolume", "help.smTakeVolume", "help.smMonitorLevel"]) {
    for (const lang of ["en", "pl", "ja", "ko", "zh-Hans", "de", "fr", "es", "pt", "id"]) expect(TRANSLATIONS[lang][key], `${lang} ${key}`).toBeTruthy();
  }

  // At each size: the sliders inside their labels, the labels' names not
  // covered, the Monitor level row inside the Effects tab, nothing sideways.
  for (const [w, h] of [[1240, 1010], [1024, 768], [390, 844]]) {
    await page.setViewportSize({ width: w, height: h });
    await page.locator("#streamerTab-timeline").click();
    await page.waitForTimeout(500);
    const fit = await page.evaluate(() => {
      const out = [];
      for (const s of document.querySelectorAll("#streamer .sm-tl-vol")) {
        if (s.hidden) continue;
        const b = s.getBoundingClientRect();
        const l = s.closest(".sm-tl-label").getBoundingClientRect();
        const name = s.closest(".sm-tl-label").querySelector(".sm-tl-label-name").getBoundingClientRect();
        out.push({ inside: b.left >= l.left - 0.5 && b.right <= l.right + 0.5 && b.bottom <= l.bottom + 0.5 && b.top >= l.top - 0.5, below: b.top >= name.bottom - 3, w: b.width });
      }
      return { out, sideways: document.scrollingElement.scrollWidth > window.innerWidth + 1 };
    });
    expect(fit.out.length, `${w}x${h}`).toBe(2);
    for (const f of fit.out) {
      expect(f.inside, `${w}x${h}`).toBe(true);
      expect(f.below, `${w}x${h}`).toBe(true);
      expect(f.w, `${w}x${h}`).toBeGreaterThan(30);
    }
    expect(fit.sideways, `${w}x${h}`).toBe(false);
    if (process.env.SHOTS_DIR) {
      await page.locator("#streamer .sm-tl-audio-labels").scrollIntoViewIfNeeded();
      await page.screenshot({ path: path.join(process.env.SHOTS_DIR, `timeline-${w}x${h}.png`) });
      await page.locator("#streamer .sm-tl").screenshot({ path: path.join(process.env.SHOTS_DIR, `timeline-card-${w}x${h}.png`) }).catch(() => {});
    }
    await page.locator("#streamerTab-effects").click();
    await expect(monitor(page)).toBeVisible();
    const row = await page.evaluate(() => {
      const r = document.querySelector("#streamer .sm-monitor-level").getBoundingClientRect();
      const p = document.querySelector("#streamerEffectsPanel").getBoundingClientRect();
      return r.left >= p.left - 0.5 && r.right <= p.right + 0.5;
    });
    expect(row, `${w}x${h}`).toBe(true);
    if (process.env.SHOTS_DIR) await page.screenshot({ path: path.join(process.env.SHOTS_DIR, `effects-${w}x${h}.png`) });
  }
});
