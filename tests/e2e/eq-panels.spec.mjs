// The EQ tab and panels in the studio (static/js/eq/), in muted Chromium on
// the real backend.
//
// The fixture's lanes are steady tones (vocals 220 Hz, drums 440, bass 660,
// other 880), so what a panel does to the sound is measured exactly: the
// lane's signal before and after its EQ, read from the engine's own taps,
// against the response the maths gives for the saved EQ at that tone.
//
// 1. Every lane has a tab, in its header in the mixer column (never on the
//    waveform lane); each opens its own kind of panel as a row under the
//    lane in both columns, and the columns stay aligned within 1 px with
//    panels open, closed, several at once, at other window heights, zoomed
//    and scrolled, and with a recording track's take folder open.
// 2. The graphical EQ: a click adds a band, a drag changes the curve and the
//    sound, keys, typed values, the type menu, undo, A/B, presets, solo,
//    bypass, and the export URL.
// 3. The pedal panel: a knob changes the sound, and its position, the EQ and
//    the open panel come back after a reload.
// 4. The keys, guitar and voice panels open on lanes that have them.
// 5. What the spectrum costs while it runs.

import { test, expect } from "@playwright/test";
import { openStudio, JOB_ID } from "./helpers.mjs";
import { removeTracks } from "./recording-helpers.mjs";

test.use({
  launchOptions: { args: ["--mute-audio", "--autoplay-policy=no-user-gesture-required"] },
  viewport: { width: 1600, height: 900 },
});

test.afterEach(async ({ request }) => removeTracks(request));

/** Page errors and console errors, minus the browser's generic network lines. */
function watchErrors(page) {
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    if (/^Failed to load resource/.test(m.text())) return;
    errors.push(m.text());
  });
  return errors;
}

const tab = (page, lane) => page.locator(`.eq-tab[data-eq-lane="${lane}"]`);
const waveRow = (page, lane) => page.locator(`.stem-waveform-row.eq-panel-lane[data-eq-lane="${lane}"]`);
const mixRow = (page, lane) => page.locator(`.mixer-column .eq-panel-row[data-eq-lane="${lane}"]`);

/** Every visible row of both columns, top to bottom, with what it is. */
async function stack(page) {
  return page.evaluate(() => {
    const id = (e) => (e.dataset.eqLane ? `eq:${e.dataset.eqLane}` : e.dataset.takeId ? `take:${e.dataset.takeId}` : e.dataset.stem || e.dataset.take || "?");
    const read = (sel) => [...document.querySelectorAll(sel)]
      .filter((e) => !e.classList.contains("hidden") && e.getBoundingClientRect().height > 0)
      .map((e) => { const b = e.getBoundingClientRect(); return { id: id(e), top: b.top, h: b.height }; })
      .sort((a, b) => a.top - b.top);
    return { mix: read(".mixer-column .lane-header"), wave: read(".stem-waveform-layer > .stem-waveform-row") };
  });
}

/** The two columns hold the same rows at the same place, within 1 px. */
async function expectAligned(page, label) {
  const s = await stack(page);
  expect(s.wave.map((r) => r.id), `${label}: rows`).toEqual(s.mix.map((r) => r.id));
  const worst = Math.max(0, ...s.mix.map((r, i) => Math.max(Math.abs(r.top - s.wave[i].top), Math.abs(r.h - s.wave[i].h))));
  expect(worst, `${label}: worst drift ${worst.toFixed(2)} px`).toBeLessThanOrEqual(1);
  return s;
}

async function play(page) {
  await page.locator("#t-play").click();
  await expect.poll(() => page.evaluate(async () => (await import("/js/state.js")).audioEngine?.isClockReady?.() === true), { timeout: 15000 }).toBe(true);
  // The waveform column's loading cover stays up for a moment after the
  // engine starts; a click at a point of a panel would land on it. (With the
  // tab on the lane, clicking the tab waited for it without saying so.)
  await expect(page.locator("#waveLoadingOverlay")).toBeHidden();
}

/**
 * The lane's level after its EQ against before, in dB, from one pair of
 * analyser reads of the same render quanta; and the maths for the saved EQ
 * at `hz`. A single tone, so the level ratio is the gain at that tone.
 */
async function measureGain(page, lane, hz) {
  return page.evaluate(async ({ lane, hz }) => {
    const eqApi = await import("/js/eqEngine.js");
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const pre = eqApi.getAnalyser(lane, "pre", { fftSize: 4096, smoothing: 0 });
    const post = eqApi.getAnalyser(lane, "post", { fftSize: 4096, smoothing: 0 });
    if (!pre || !post) return { error: "no analyser" };
    const a = new Float32Array(pre.fftSize);
    const b = new Float32Array(post.fftSize);
    const a2 = new Float32Array(pre.fftSize);
    const n = Math.min(a.length, b.length);
    const rms = (x) => { let s = 0; for (let i = x.length - n; i < x.length; i++) s += x[i] * x[i]; return Math.sqrt(s / n); };
    await sleep(350);
    for (let tries = 0; tries < 300; tries++) {
      pre.getFloatTimeDomainData(a);
      post.getFloatTimeDomainData(b);
      pre.getFloatTimeDomainData(a2);
      if (a.every((v, i) => v === a2[i]) && rms(a) > 0) {
        let exact = true;
        for (let i = 0; i < n; i++) if (a[a.length - n + i] !== b[b.length - n + i]) { exact = false; break; }
        return { db: 20 * Math.log10(rms(b) / rms(a)), maths: eqApi.getFrequencyResponse(lane, [hz])[0], exact };
      }
      await sleep(3);
    }
    return { error: "no stable read" };
  }, { lane, hz });
}

const laneEq = (page, lane) => page.evaluate(async (l) => (await import("/js/eqEngine.js")).getLaneEq(l), lane);

/** Pixel position of a frequency and gain on the open graph of `lane`. */
async function graphPoint(page, lane, hz, db) {
  return page.evaluate(async ({ lane, hz, db }) => {
    const m = await import("/js/eq/eqGraphMath.js");
    const cv = document.querySelector(`.eq-panel-lane[data-eq-lane="${lane}"] .eq-curve-canvas`);
    const box = cv.getBoundingClientRect();
    const r = m.plotRect(box.width, box.height);
    return { x: box.left + m.freqToX(hz, r), y: box.top + m.dbToY(db, r) };
  }, { lane, hz, db });
}

test("every lane has an EQ tab that opens its own panel, and the columns stay aligned", async ({ page }) => {
  const errors = watchErrors(page);
  await openStudio(page);
  for (const lane of ["vocals", "drums", "bass", "other"]) {
    await expect(tab(page, lane)).toBeVisible();
    // In the lane's own header, in the rail under M and S.
    await expect(page.locator(`.mixer-column .lane-header[data-stem="${lane}"] .mx-rail > .mx-eq-slot > .eq-tab`)).toHaveAttribute("data-eq-lane", lane);
  }
  // The waveform lanes carry only their waveforms.
  await expect(page.locator(".waves-column .eq-tab")).toHaveCount(0);
  const tabBefore = await tab(page, "other").boundingBox();
  await expectAligned(page, "all closed");

  const kinds = { vocals: "voice", drums: "drum", bass: "bass", other: "graph" };
  const content = { voice: ".eq-voice-panel", drum: ".eq-graph-panel", bass: ".eq-pedal-panel", graph: ".eq-graph-panel" };
  for (const [lane, kind] of Object.entries(kinds)) {
    await tab(page, lane).click();
    await expect(waveRow(page, lane)).toHaveAttribute("data-eq-kind", kind);
    await expect(mixRow(page, lane)).toHaveCount(1);
    await expect(waveRow(page, lane).locator(content[kind])).toBeVisible();
    await expect(tab(page, lane)).toHaveAttribute("aria-expanded", "true");
    const s = await expectAligned(page, `${lane} open`);
    // Right under its lane, in both columns.
    const i = s.mix.findIndex((r) => r.id === lane);
    expect(s.mix[i + 1].id).toBe(`eq:${lane}`);
    await tab(page, lane).click();
    await expect(waveRow(page, lane)).toHaveCount(0);
    await expect(mixRow(page, lane)).toHaveCount(0);
    await expectAligned(page, `${lane} closed`);
  }

  // Several at once, at other window heights.
  const ruler0 = await page.evaluate(() => document.getElementById("ruler-time").getBoundingClientRect().width);
  for (const lane of ["drums", "bass", "vocals"]) await tab(page, lane).click();
  await expect(page.locator(".eq-panel-lane")).toHaveCount(3);
  for (const h of [720, 900, 1150]) {
    await page.setViewportSize({ width: 1600, height: h });
    await page.waitForTimeout(400);
    await expectAligned(page, `three open at ${h} px`);
  }
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.waitForTimeout(300);
  // The ruler is the time axis: opening panels does not change it.
  expect(await page.evaluate(() => document.getElementById("ruler-time").getBoundingClientRect().width)).toBeCloseTo(ruler0, 0);

  // Zoomed and scrolled: the rows stay aligned, the panel and the tabs stay
  // on the visible part of the lanes.
  await page.mouse.move(1100, 300);
  for (let i = 0; i < 3; i++) await page.mouse.wheel(0, -300);
  await page.waitForTimeout(500);
  const zoom = await page.evaluate(() => parseFloat(getComputedStyle(document.getElementById("lanes")).getPropertyValue("--zoom")));
  expect(zoom).toBeGreaterThan(1);
  await page.evaluate(() => { const s = document.getElementById("wave-scroll"); s.scrollLeft = s.scrollWidth / 2; });
  await page.waitForTimeout(300);
  await expectAligned(page, "zoomed and scrolled");
  const pos = await page.evaluate(() => {
    const scroll = document.getElementById("wave-scroll").getBoundingClientRect();
    const body = document.querySelector('.eq-panel-lane[data-eq-lane="bass"] .eq-panel-body').getBoundingClientRect();
    const t = document.querySelector('.eq-tab[data-eq-lane="other"]').getBoundingClientRect();
    const col = document.querySelector(".daw-stems-panel").getBoundingClientRect();
    return { scrollLeft: scroll.left, scrollW: document.getElementById("wave-scroll").clientWidth, bodyLeft: body.left, bodyW: body.width, tabLeft: t.left, tabRight: t.right, colLeft: col.left, colRight: col.right };
  });
  expect(Math.abs(pos.bodyLeft - pos.scrollLeft)).toBeLessThanOrEqual(1);
  expect(Math.abs(pos.bodyW - pos.scrollW)).toBeLessThanOrEqual(1);
  // The tab is in the mixer column: zoom and scroll leave it where it was.
  expect(pos.tabLeft).toBeGreaterThanOrEqual(pos.colLeft);
  expect(pos.tabRight).toBeLessThanOrEqual(pos.colRight);
  expect(Math.abs(pos.tabLeft - tabBefore.x)).toBeLessThanOrEqual(1);
  // Wheel over a panel does not zoom the lanes.
  const box = await waveRow(page, "vocals").boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.wheel(0, -300);
  await page.waitForTimeout(300);
  expect(await page.evaluate(() => parseFloat(getComputedStyle(document.getElementById("lanes")).getPropertyValue("--zoom")))).toBeCloseTo(zoom, 5);
  expect(errors).toEqual([]);
});

test("a recording track gets its EQ panel, aligned with its open take folder", async ({ page, request }) => {
  const errors = watchErrors(page);
  // A track with two takes: its folder opens by default.
  const r = await request.post(`/api/jobs/${JOB_ID}/tracks`, { data: {} });
  const trackId = (await r.json()).id;
  for (let k = 0; k < 2; k++) {
    const t = await request.post(`/api/jobs/${JOB_ID}/takes`, { data: { track_id: trackId } });
    const takeId = (await t.json()).take_id;
    const buf = Buffer.alloc(48000 * 2 * 4);
    for (let i = 0; i < 48000 * 2; i++) buf.writeFloatLE(0.3 * Math.sin((2 * Math.PI * 330 * i) / 48000), i * 4);
    await request.put(`/api/jobs/${JOB_ID}/takes/${takeId}/parts/audio?seq=0`, { data: buf, headers: { "content-type": "application/octet-stream" } });
    const fin = await request.post(`/api/jobs/${JOB_ID}/takes/${takeId}/finish`, {
      data: { v: 1, timeline: { first_sample_song_sec: 0.5, tempo: 1.0 }, latency: { round_trip_ms: 0, source: "estimated", user_nudge_ms: 0 }, capture: { rate: 48000, channels: 1, device_label: "E2E tone" } },
    });
    expect(fin.status()).toBe(200);
  }
  await openStudio(page);
  await expect(page.locator(`.take-folder-row[data-folder-of="${trackId}"]`)).toHaveCount(2, { timeout: 20000 });
  await tab(page, trackId).click();
  const kind = await page.evaluate(async (id) => (await import("/js/eq/eqRows.js")).laneKind(id), trackId);
  expect(["graph", "voice"]).toContain(kind);
  await expect(waveRow(page, trackId)).toHaveAttribute("data-eq-kind", kind);
  const s = await expectAligned(page, "track EQ with its folder open");
  const i = s.mix.findIndex((row) => row.id === trackId);
  // The track, then its EQ panel, then its takes.
  expect(s.mix[i + 1].id).toBe(`eq:${trackId}`);
  expect(s.mix[i + 2].id).toMatch(/^take:/);
  expect(s.mix[i + 3].id).toMatch(/^take:/);
  // Closing the folder keeps the panel and the alignment.
  await page.locator(`.take-folder-toggle[data-track="${trackId}"]`).click();
  await expect(page.locator(`.take-folder-row[data-folder-of="${trackId}"]`)).toHaveCount(0);
  await expectAligned(page, "track EQ, folder closed");
  expect(errors).toEqual([]);
});

test("graphical EQ: add, drag, keys, typing, type menu, undo, A/B, preset, solo, bypass and export", async ({ page }) => {
  test.setTimeout(90_000);
  const errors = watchErrors(page);
  await openStudio(page);
  await play(page);
  await tab(page, "other").click();
  const panel = waveRow(page, "other");
  await expect(panel.locator(".eq-curve-canvas")).toBeVisible();
  await page.waitForTimeout(300);
  const curve0 = await panel.locator(".eq-curve-canvas").evaluate((c) => c.toDataURL());
  // At the stems' own 44.1 kHz the export matches what plays: no note.
  if (await page.evaluate(async () => (await import("/js/eqEngine.js")).liveSampleRate()) === 44100) {
    await expect(mixRow(page, "other").locator(".eq-note")).toBeHidden();
  }

  // A click at 880 Hz, +9 dB adds a bell there: the "other" lane is an 880 Hz tone.
  const p = await graphPoint(page, "other", 880, 9);
  await page.mouse.click(p.x, p.y);
  let eq = await laneEq(page, "other");
  expect(eq.params.bands).toHaveLength(1);
  expect(eq.params.bands[0].type).toBe("bell");
  expect(Math.abs(Math.log2(eq.params.bands[0].f / 880))).toBeLessThan(0.02);
  expect(eq.params.bands[0].g).toBeCloseTo(9, 0);
  let m = await measureGain(page, "other", 880);
  expect(m.error).toBeUndefined();
  expect(Math.abs(m.db - m.maths)).toBeLessThan(0.1);
  expect(m.db).toBeGreaterThan(8);
  console.log(`graph click: +${m.db.toFixed(3)} dB measured at 880 Hz, maths ${m.maths.toFixed(3)} dB`);

  // Drag it down to -6 dB: the curve and the sound follow.
  const q = await graphPoint(page, "other", eq.params.bands[0].f, eq.params.bands[0].g);
  const to = await graphPoint(page, "other", 880, -6);
  await page.mouse.move(q.x, q.y);
  await page.mouse.down();
  await page.mouse.move(to.x, to.y, { steps: 8 });
  await page.mouse.up();
  eq = await laneEq(page, "other");
  expect(eq.params.bands[0].g).toBeCloseTo(-6, 0);
  const curve1 = await panel.locator(".eq-curve-canvas").evaluate((c) => c.toDataURL());
  expect(curve1).not.toBe(curve0);
  m = await measureGain(page, "other", 880);
  expect(Math.abs(m.db - m.maths)).toBeLessThan(0.1);
  expect(m.db).toBeLessThan(-5);
  console.log(`graph drag: ${m.db.toFixed(3)} dB measured at 880 Hz, maths ${m.maths.toFixed(3)} dB`);

  // Undo puts the click's +9 back, redo the drag.
  const undo = mixRow(page, "other").locator(".eq-undo");
  await undo.click();
  expect((await laneEq(page, "other")).params.bands[0].g).toBeCloseTo(9, 0);
  await mixRow(page, "other").locator(".eq-redo").click();
  expect((await laneEq(page, "other")).params.bands[0].g).toBeCloseTo(-6, 0);

  // Keys on the focused handle: right moves the frequency up a semitone.
  const handle = panel.locator(".eq-handle").first();
  await handle.focus();
  const f0 = (await laneEq(page, "other")).params.bands[0].f;
  await page.keyboard.press("ArrowRight");
  expect((await laneEq(page, "other")).params.bands[0].f).toBeCloseTo(f0 * 2 ** (1 / 12), 0);
  await page.keyboard.press("ArrowUp");
  expect((await laneEq(page, "other")).params.bands[0].g).toBeCloseTo(-5.5, 1);

  // Typed values in the band list.
  const freq = panel.locator(".eq-band-row .eq-band-f").first();
  await freq.fill("2.5k");
  await freq.press("Enter");
  expect((await laneEq(page, "other")).params.bands[0].f).toBe(2500);
  const gain = panel.locator(".eq-band-row .eq-band-g").first();
  await gain.fill("4,5");
  await gain.press("Enter");
  expect((await laneEq(page, "other")).params.bands[0].g).toBe(4.5);

  // Right-click on the handle: the type menu. A notch at 2.5 kHz.
  const h = await graphPoint(page, "other", 2500, 4.5);
  await page.mouse.click(h.x, h.y, { button: "right" });
  await expect(panel.locator(".eq-popover")).toBeVisible();
  await panel.locator(".eq-popover .eq-pop-item", { hasText: "Notch" }).click();
  expect((await laneEq(page, "other")).params.bands[0].type).toBe("notch");
  await page.keyboard.press("Escape");
  await expect(panel.locator(".eq-popover")).toHaveCount(0);

  // A/B: B starts as a copy of A; a change to B goes away when A comes back.
  const ab = mixRow(page, "other").locator(".eq-ab");
  await ab.click();
  await expect(ab).toHaveText("B");
  await panel.locator(".eq-band-row .eq-band-del").first().click();
  expect((await laneEq(page, "other")).params.bands).toHaveLength(0);
  await ab.click();
  await expect(ab).toHaveText("A");
  expect((await laneEq(page, "other")).params.bands[0].type).toBe("notch");

  // A preset replaces the bands.
  await mixRow(page, "other").locator(".eq-preset").selectOption("eq.preset.presence");
  eq = await laneEq(page, "other");
  expect(eq.params.bands.map((b) => [b.type, b.f, b.g])).toEqual([["bell", 3000, 2.5]]);

  // Solo plays a band pass at the band, without saving it.
  await panel.locator(".eq-band-row .eq-band-solo").first().click();
  const solo = await page.evaluate(async () => {
    const e = await import("/js/eqEngine.js");
    const g = await import("/js/eq/eqGraphMath.js");
    const { eqResponseDb } = await import("/js/eqMath.js");
    const want = eqResponseDb(e.normalizeEq(g.soloEq(e.getLaneEq("other").params.bands[0])), [880, 3000], e.liveSampleRate());
    for (let i = 0; i < 40; i++) {
      const node = e.getNodeResponse("other", [880, 3000]);
      if (node && Math.abs(node[0] - want[0]) < 0.01) return { node: Array.from(node), want: Array.from(want), saved: e.getLaneEq("other").params.bands[0].type };
      await new Promise((r) => setTimeout(r, 50));
    }
    return { node: Array.from(e.getNodeResponse("other", [880, 3000]) || []), want: Array.from(want) };
  });
  expect(Math.abs(solo.node[0] - solo.want[0])).toBeLessThan(0.01);
  expect(solo.saved).toBe("bell");
  await panel.locator(".eq-band-row .eq-band-solo").first().click();

  // Bypass: saved, and the lane plays bit for bit as without EQ.
  await mixRow(page, "other").locator(".eq-power").click();
  expect((await laneEq(page, "other")).bypass).toBe(true);
  m = await measureGain(page, "other", 880);
  expect(m.exact).toBe(true);
  await expect(tab(page, "other")).toHaveClass(/bypassed/);
  await mixRow(page, "other").locator(".eq-power").click();
  expect((await laneEq(page, "other")).bypass).toBe(false);
  await expect(tab(page, "other")).toHaveClass(/active/);

  // Delete on a focused handle removes the band and nothing else: the song
  // stays out of the Trash.
  await panel.locator(".eq-handle").first().focus();
  await page.keyboard.press("Delete");
  expect((await laneEq(page, "other")).params.bands).toHaveLength(0);
  const trash = await page.evaluate(() => JSON.parse(localStorage.getItem("stemdeck.folders")).folders.find((f) => f.id === "trash").items);
  expect(trash).toEqual([]);
  await mixRow(page, "other").locator(".eq-undo").click();
  expect((await laneEq(page, "other")).params.bands).toHaveLength(1);

  // The export URL carries the EQ.
  const download = page.waitForEvent("download");
  await page.evaluate(async () => (await import("/js/player.js")).downloadCurrentMix("wav"));
  const url = new URL((await download).url());
  const sent = JSON.parse(Buffer.from(url.searchParams.get("fx"), "base64url").toString("utf8"));
  expect(sent.lanes.other[0].b).toEqual([["bell", 3000, 2.5, 0.9, 12]]);
  expect(errors).toEqual([]);
});

test("Bass EQ: a knob changes the sound; knobs, EQ and the open panel come back after a reload", async ({ page }) => {
  test.setTimeout(90_000);
  const errors = watchErrors(page);
  await openStudio(page);
  await play(page);
  await tab(page, "bass").click();
  const panel = waveRow(page, "bass");
  const knobs = panel.locator(".eq-knob-dial");
  await expect(knobs).toHaveCount(14);
  // Mid frequency (the 5th knob): to 660 Hz, the bass lane's tone, by keys.
  const midFreq = knobs.nth(4);
  await midFreq.focus();
  for (let i = 0; i < 40; i++) {
    const f = (await laneEq(page, "bass")).params.ui?.c?.mf ?? 400;
    if (f >= 655) break;
    await page.keyboard.press("ArrowUp");
  }
  // Mid gain (the 6th): up.
  await knobs.nth(5).focus();
  for (let i = 0; i < 20; i++) await page.keyboard.press("ArrowUp");
  const eq = await laneEq(page, "bass");
  expect(eq.params.ui.panel).toBe("bass");
  expect(eq.params.ui.c.mg).toBeGreaterThan(5);
  const m = await measureGain(page, "bass", 660);
  expect(m.error).toBeUndefined();
  expect(Math.abs(m.db - m.maths)).toBeLessThan(0.1);
  expect(m.db).toBeGreaterThan(4);
  console.log(`bass mid knob: +${m.db.toFixed(3)} dB measured at 660 Hz, maths ${m.maths.toFixed(3)} dB`);
  const text = await knobs.nth(5).getAttribute("aria-valuetext");
  await expect(tab(page, "bass")).toHaveClass(/active/);

  // Saved with the song (debounced), then a reload.
  await page.waitForFunction(() => JSON.parse(localStorage.getItem("stemdeck:mix:e2e0deadbeef:daw") || "null")?.bass?.fx?.[0]?.params?.ui?.c?.mg > 5, null, { timeout: 5000 });
  await page.reload();
  await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
  await expect(waveRow(page, "bass").locator(".eq-pedal-panel")).toBeVisible({ timeout: 20000 });
  await expect(tab(page, "bass")).toHaveAttribute("aria-expanded", "true");
  await expect(tab(page, "bass")).toHaveClass(/active/);
  await expect(waveRow(page, "bass").locator(".eq-knob-dial").nth(5)).toHaveAttribute("aria-valuetext", text);
  expect((await laneEq(page, "bass")).params.bands).toEqual(eq.params.bands);
  await expectAligned(page, "after reload, bass open");
  expect(errors).toEqual([]);
});

test("Guitar EQ, Keys EQ and the voice placeholder open on lanes that have them", async ({ page }) => {
  const errors = watchErrors(page);
  // The fixture has no guitar or piano stem: the job state is given two more
  // lanes that play the bass and other files.
  await page.route(new RegExp(`/api/jobs/${JOB_ID}$`), async (route) => {
    const resp = await route.fetch();
    const json = await resp.json();
    if (Array.isArray(json.stems) && !json.stems.some((s) => s.name === "guitar")) {
      json.stems.push(
        { name: "guitar", url: `/api/jobs/${JOB_ID}/stems/bass.wav` },
        { name: "piano", url: `/api/jobs/${JOB_ID}/stems/other.wav` },
      );
    }
    await route.fulfill({ response: resp, json });
  });
  await openStudio(page);
  await expect(tab(page, "guitar")).toBeVisible({ timeout: 20000 });
  await tab(page, "guitar").click();
  await expect(waveRow(page, "guitar")).toHaveAttribute("data-eq-kind", "guitar");
  await expect(waveRow(page, "guitar").locator(".eq-plate-title")).toHaveText("Guitar EQ");
  await tab(page, "piano").click();
  await expect(waveRow(page, "piano")).toHaveAttribute("data-eq-kind", "keys");
  await expect(waveRow(page, "piano").locator(".eq-knob-dial")).toHaveCount(8);
  // Keys: Air up adds a high shelf on the saved EQ.
  const air = waveRow(page, "piano").locator(".eq-knob-dial").nth(5);
  await air.focus();
  for (let i = 0; i < 10; i++) await page.keyboard.press("ArrowUp");
  const keys = await laneEq(page, "piano");
  expect(keys.params.ui.panel).toBe("keys");
  expect(keys.params.bands[4]).toMatchObject({ type: "highshelf", f: 10000 });
  expect(keys.params.bands[4].g).toBeGreaterThan(1);
  await tab(page, "vocals").click();
  await expect(waveRow(page, "vocals").locator(".eq-voice-title")).toHaveText("Voice EQ coming soon");
  expect((await laneEq(page, "vocals")).params.bands).toEqual([]);
  await expectAligned(page, "guitar, keys and voice open");
  expect(errors).toEqual([]);
});

test("what the spectrum costs while it runs", async ({ page }) => {
  test.setTimeout(60_000);
  await openStudio(page);
  await play(page);
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Performance.enable");
  const metric = async () => {
    const { metrics } = await cdp.send("Performance.getMetrics");
    const get = (n) => metrics.find((x) => x.name === n)?.value || 0;
    return { script: get("ScriptDuration"), layout: get("LayoutDuration"), style: get("RecalcStyleDuration"), task: get("TaskDuration"), t: get("Timestamp") };
  };
  const span = async (ms) => {
    const a = await metric();
    await page.waitForTimeout(ms);
    const b = await metric();
    const wall = b.t - a.t;
    return {
      script: (100 * (b.script - a.script)) / wall,
      task: (100 * (b.task - a.task)) / wall,
      style: (100 * (b.style - a.style + b.layout - a.layout)) / wall,
    };
  };
  const closed = await span(4000);
  await tab(page, "drums").click();
  await expect(waveRow(page, "drums").locator(".eq-curve-canvas")).toBeVisible();
  await page.waitForTimeout(500);
  const open = await span(4000);
  const stats = await page.evaluate(async () => (await import("/js/eq/eqRows.js")).eqPanelStats("drums"));
  console.log(`main thread, % of wall time over 4 s while playing: closed script ${closed.script.toFixed(2)} task ${closed.task.toFixed(2)} style+layout ${closed.style.toFixed(2)}; `
    + `graph open script ${open.script.toFixed(2)} task ${open.task.toFixed(2)} style+layout ${open.style.toFixed(2)}; `
    + `spectrum frame ${stats.msPerFrame.toFixed(3)} ms over ${stats.frames} frames`);
  expect(stats.frames).toBeGreaterThan(30);
  // A frame of the spectrum must stay far under a 60 Hz frame (16.7 ms).
  expect(stats.msPerFrame).toBeLessThan(3);
  // Closing it stops the loop.
  await tab(page, "drums").click();
  await page.waitForTimeout(300);
  expect(await page.evaluate(async () => (await import("/js/eq/eqRows.js")).eqPanelStats("drums"))).toBeNull();
});

test("at 48 kHz the general EQ says exports can differ above 10 kHz", async ({ page }) => {
  const errors = watchErrors(page);
  // Every AudioContext of the page runs at 48 kHz, as on a device set to it.
  await page.addInitScript(() => {
    const Native = window.AudioContext;
    window.AudioContext = class extends Native {
      constructor(opts = {}) { super({ ...opts, sampleRate: 48000 }); }
    };
  });
  await openStudio(page);
  await expect.poll(() => page.evaluate(async () => (await import("/js/eqEngine.js")).liveSampleRate())).toBe(48000);
  await tab(page, "other").click();
  const note = mixRow(page, "other").locator(".eq-note");
  await expect(note).toBeVisible();
  await expect(note).toContainText("48.0 kHz");
  await expect(note).toContainText("10 kHz");
  expect(errors).toEqual([]);
});
