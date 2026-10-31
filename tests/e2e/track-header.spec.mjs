// The track header: each lane's row in the mixer column (mixer.js
// renderMixerRow, capture/takeLanes.js buildRow), in muted Chromium on the
// real backend.
//
// The side-rail header (#776): a card on the left (the lane's swatch and name,
// a short fader with its level, the meter under it; a recording track's menu
// on the name's line and its arm button and input picker on the bottom line),
// then two columns the full height of the row: the key (+, KEY, the offset,
// minus) and the rail (M, S, the EQ tab, download), in the same place on
// every row. Rows stay compact: at most 90 px, never under 72 px. These are
// measurements of that, at the three window sizes the header was designed for:
//
// 1. The rail's cells are stacked M, S, EQ, download, share the row's height
//    equally and sit at the same x on every row; the key's four cells are
//    level with them. Their labels are 13px bold and never cut; the name is
//    15px bold on one line.
// 2. Nothing leaves its row or the column, nothing overlaps; rows keep
//    --lane-h (72 to 90 px) and the column keeps --daw-col-w.
// 3. Rows and waveform lanes stay aligned within 1px, with a take folder and
//    an EQ panel open.
// 4. The longest stem names of every language fit the name's line.
//
// This replaces the measurements of the previous header (controls on two
// lines beside the name, 28px tall), which no longer exists.

import { test, expect } from "@playwright/test";
import { openStudio, JOB_ID } from "./helpers.mjs";
import { removeTracks } from "./recording-helpers.mjs";

test.use({
  launchOptions: {
    args: [
      "--mute-audio",
      "--autoplay-policy=no-user-gesture-required",
      "--use-fake-device-for-media-stream",
      "--use-fake-ui-for-media-stream",
    ],
  },
});

test.afterEach(async ({ request }) => removeTracks(request));

const VIEWPORTS = [[1280, 720], [1600, 1000], [1920, 1080]];

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

/** A recording track with two takes: its take folder opens by default. */
async function addTrackWithTakes(request) {
  const r = await request.post(`/api/jobs/${JOB_ID}/tracks`, { data: {} });
  const trackId = (await r.json()).id;
  for (let k = 0; k < 2; k++) {
    const t = await request.post(`/api/jobs/${JOB_ID}/takes`, { data: { track_id: trackId } });
    const takeId = (await t.json()).take_id;
    const buf = Buffer.alloc(48000 * 4);
    for (let i = 0; i < 48000; i++) buf.writeFloatLE(0.3 * Math.sin((2 * Math.PI * 330 * i) / 48000), i * 4);
    await request.put(`/api/jobs/${JOB_ID}/takes/${takeId}/parts/audio?seq=0`, { data: buf, headers: { "content-type": "application/octet-stream" } });
    const fin = await request.post(`/api/jobs/${JOB_ID}/takes/${takeId}/finish`, {
      data: { v: 1, timeline: { first_sample_song_sec: 0.5, tempo: 1.0 }, latency: { round_trip_ms: 0, source: "estimated", user_nudge_ms: 0 }, capture: { rate: 48000, channels: 1, device_label: "E2E tone" } },
    });
    expect(fin.status()).toBe(200);
  }
  return trackId;
}

// The rail, top to bottom, and the key column beside it.
const RAIL = ["mute", "solo", "eq", "dl"];
const KEY = ["keyUp", "keyLabel", "keyNum", "keyDown"];

/** Every visible lane header, with the box, font and overflow of each control. */
async function headers(page) {
  return page.evaluate(() => {
    const CONTROLS = {
      mute: ".mx-rail .mute",
      solo: ".mx-rail .solo",
      eq: ".mx-rail .eq-tab",
      dl: ".mx-rail .lane-dl:not(.track-dl-off)",
      keyUp: ".mx-keyrail .lane-key-step.up",
      keyLabel: ".mx-keyrail .lane-key-label",
      keyNum: ".mx-keyrail .lane-key-num",
      keyDown: ".mx-keyrail .lane-key-step.down",
      arm: ".track-arm",
      menu: ".take-menu-btn",
      input: ".track-input",
      fader: ".mx-fader-input",
      val: ".mx-val",
      name: ".mx-head .mx-name",
      folder: ".take-folder-toggle",
    };
    const box = (e) => { const b = e.getBoundingClientRect(); return { l: b.left, t: b.top, r: b.right, b: b.bottom, w: b.width, h: b.height }; };
    const col = document.querySelector(".daw-stems-panel");
    const app = document.querySelector(".app");
    const rows = [...document.querySelectorAll(".mixer-column .lane-header[data-stem]")]
      .filter((e) => !e.classList.contains("hidden") && e.getBoundingClientRect().height > 0)
      .map((row) => {
        const controls = {};
        for (const [k, sel] of Object.entries(CONTROLS)) {
          const e = row.querySelector(sel);
          if (!e || e.getClientRects().length === 0) continue;
          const cs = getComputedStyle(e);
          if (cs.visibility === "hidden" || cs.display === "none") continue;
          controls[k] = {
            ...box(e),
            font: parseFloat(cs.fontSize),
            weight: Number(cs.fontWeight),
            lineH: parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.2,
            cut: e.scrollWidth > e.clientWidth + 1,
          };
        }
        const svg = row.querySelector(".lane-dl svg");
        const rail = row.querySelector(".mx-rail");
        return { stem: row.dataset.stem, track: !!row.dataset.take, row: box(row), rail: rail ? box(rail) : null, controls, dlIcon: svg ? box(svg).w : 0 };
      });
    return {
      rows,
      col: box(col),
      colVar: parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--daw-col-w")),
      laneH: parseFloat(getComputedStyle(app).getPropertyValue("--lane-h")),
    };
  });
}

/** Every visible row of both columns, top to bottom. */
async function stack(page) {
  return page.evaluate(() => {
    const read = (sel) => [...document.querySelectorAll(sel)]
      .filter((e) => !e.classList.contains("hidden") && e.getBoundingClientRect().height > 0)
      .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
    const mix = read(".mixer-column .lane-header");
    const wave = read(".stem-waveform-layer > .stem-waveform-row");
    return {
      mix: mix.map((e) => { const b = e.getBoundingClientRect(); return { top: b.top, h: b.height }; }),
      wave: wave.map((e) => { const b = e.getBoundingClientRect(); return { top: b.top, h: b.height }; }),
    };
  });
}

function expectAligned(s, label) {
  expect(s.mix.length, `${label}: rows`).toBe(s.wave.length);
  let worst = 0;
  s.mix.forEach((m, i) => {
    worst = Math.max(worst, Math.abs(m.top - s.wave[i].top), Math.abs(m.h - s.wave[i].h));
  });
  expect(worst, `${label}: worst row drift ${worst.toFixed(2)} px`).toBeLessThanOrEqual(1);
}

/** The rail and key columns, sizes, containment, overlap and cut labels of every header. */
function expectHeaders(h, label) {
  expect(h.col.w, `${label}: column width`).toBeCloseTo(h.colVar, 0);
  expect(h.laneH, `${label}: --lane-h`).toBeGreaterThanOrEqual(72);
  expect(h.laneH, `${label}: --lane-h`).toBeLessThanOrEqual(90);
  const railX = new Set();
  for (const r of h.rows) {
    const at = `${label}, ${r.stem}`;
    const c = r.controls;
    expect(Math.abs(r.row.h - h.laneH), `${at}: row height ${r.row.h} against --lane-h ${h.laneH}`).toBeLessThanOrEqual(1);

    // The rail: its cells stacked in order, the rail's width, equal heights
    // filling it. A stem has all four; a recording track has download once
    // it has a take to download.
    const cells = RAIL.filter((k) => c[k]);
    expect(cells, `${at}: rail cells`).toEqual(r.track && !c.dl ? ["mute", "solo", "eq"] : RAIL);
    expect(r.rail, `${at}: rail`).toBeTruthy();
    railX.add(Math.round(r.rail.l));
    const each = r.rail.h / cells.length;
    cells.forEach((k, i) => {
      expect(c[k].w, `${at}: ${k} width`).toBeGreaterThanOrEqual(44);
      expect(Math.abs(c[k].h - each), `${at}: ${k} height ${c[k].h} against ${each}`).toBeLessThanOrEqual(1);
      expect(Math.abs(c[k].t - (r.rail.t + i * each)), `${at}: ${k} position`).toBeLessThanOrEqual(1);
      // A quarter of the shortest row (72 px) and up.
      expect(c[k].h, `${at}: ${k} height`).toBeGreaterThanOrEqual(17);
    });

    // The key: four cells, each level with the rail cell beside it.
    if (!r.track) {
      KEY.forEach((k, i) => {
        expect(c[k], `${at}: ${k}`).toBeTruthy();
        expect(Math.abs(c[k].t - c[RAIL[i]].t), `${at}: ${k} level with ${RAIL[i]}`).toBeLessThanOrEqual(1);
        expect(Math.abs(c[k].h - c[RAIL[i]].h), `${at}: ${k} height`).toBeLessThanOrEqual(1);
        expect(c[k].w, `${at}: ${k} width`).toBeGreaterThanOrEqual(32);
      });
    }

    // The labels: 13px bold, never cut.
    for (const k of ["mute", "solo", "eq", "keyNum", "val"]) {
      if (!c[k]) continue;
      expect(c[k].font, `${at}: ${k} font`).toBeGreaterThanOrEqual(13);
      expect(c[k].weight, `${at}: ${k} weight`).toBeGreaterThanOrEqual(700);
      expect(c[k].cut, `${at}: ${k} label cut`).toBe(false);
    }
    // The name: 15px bold on one line.
    expect(c.name.font, `${at}: name font`).toBeGreaterThanOrEqual(15);
    expect(c.name.weight, `${at}: name weight`).toBeGreaterThanOrEqual(700);
    expect(c.name.h, `${at}: name lines`).toBeLessThanOrEqual(c.name.lineH + 1);
    if (c.dl) expect(r.dlIcon, `${at}: download icon`).toBeGreaterThanOrEqual(16);
    expect(c.fader?.w, `${at}: fader length`).toBeGreaterThanOrEqual(60);
    // A recording track's own controls.
    if (r.track) {
      for (const k of ["arm", "menu"]) {
        expect(c[k], `${at}: ${k}`).toBeTruthy();
        expect(c[k].w, `${at}: ${k} width`).toBeGreaterThanOrEqual(22);
        expect(c[k].h, `${at}: ${k} height`).toBeGreaterThanOrEqual(22);
      }
      if (c.input) expect(c.input.font, `${at}: input font`).toBeGreaterThanOrEqual(13);
    }

    // Inside the row and the column.
    for (const [k, b] of Object.entries(c)) {
      expect(b.t, `${at}: ${k} above its row`).toBeGreaterThanOrEqual(r.row.t - 0.5);
      expect(b.b, `${at}: ${k} below its row`).toBeLessThanOrEqual(r.row.b + 0.5);
      expect(b.l, `${at}: ${k} left of the column`).toBeGreaterThanOrEqual(h.col.l - 0.5);
      expect(b.r, `${at}: ${k} right of the column`).toBeLessThanOrEqual(h.col.r + 0.5);
    }
    // Nothing on top of anything else.
    const keys = Object.keys(c);
    for (let i = 0; i < keys.length; i++) {
      for (let j = i + 1; j < keys.length; j++) {
        const a = c[keys[i]];
        const b = c[keys[j]];
        const ox = Math.min(a.r, b.r) - Math.max(a.l, b.l);
        const oy = Math.min(a.b, b.b) - Math.max(a.t, b.t);
        expect(ox > 0.5 && oy > 0.5, `${at}: ${keys[i]} overlaps ${keys[j]}`).toBe(false);
      }
    }
  }
  // The rail is in the same place on every row.
  expect([...railX], `${label}: rail x on every row`).toHaveLength(1);
}

for (const [w, hgt] of VIEWPORTS) {
  test(`the header's rail and key line up, fit, and stay aligned at ${w}x${hgt}`, async ({ page, request }) => {
    const errors = watchErrors(page);
    await page.setViewportSize({ width: w, height: hgt });
    const trackId = await addTrackWithTakes(request);
    await openStudio(page);
    await expect(page.locator(`.take-folder-row[data-folder-of="${trackId}"]`)).toHaveCount(2, { timeout: 20000 });
    await expect(page.locator(`.eq-tab[data-eq-lane="${trackId}"]`)).toBeAttached();
    await page.waitForTimeout(300);

    const h = await headers(page);
    // The four stems, the track, and the original if shown.
    expect(h.rows.filter((r) => r.track)).toHaveLength(1);
    expect(h.rows.length).toBeGreaterThanOrEqual(5);
    for (const r of h.rows) expect(r.controls.eq, `${r.stem}: EQ tab`).toBeTruthy();
    expectHeaders(h, `${w}x${hgt}`);
    expectAligned(await stack(page), `${w}x${hgt}, take folder open`);

    // With an EQ panel open under a stem and under the track.
    await page.locator('.eq-tab[data-eq-lane="drums"]').click();
    await page.locator(`.eq-tab[data-eq-lane="${trackId}"]`).click();
    await expect(page.locator(".eq-panel-lane")).toHaveCount(2);
    await page.waitForTimeout(300);
    expectAligned(await stack(page), `${w}x${hgt}, EQ panels open`);
    expectHeaders(await headers(page), `${w}x${hgt}, EQ panels open`);
    await page.locator('.eq-tab[data-eq-lane="drums"]').click();
    await page.locator(`.eq-tab[data-eq-lane="${trackId}"]`).click();
    await expect(page.locator(".eq-panel-lane")).toHaveCount(0);
    expect(errors).toEqual([]);
  });
}

test("the longest stem names of every language fit the header", async ({ page, request }) => {
  test.setTimeout(90_000);
  const errors = watchErrors(page);
  await page.setViewportSize({ width: 1280, height: 720 });
  await addTrackWithTakes(request);
  await openStudio(page);
  await expect(page.locator(".mixer-column .track-lane")).toHaveCount(1, { timeout: 20000 });
  const codes = await page.evaluate(async () => (await import("/js/i18n.js")).LANGUAGES.map((l) => l.code));
  expect(codes.length).toBeGreaterThanOrEqual(10);
  for (const code of codes) {
    // Each visible header gets one of the language's longest stem names, the
    // longest first (the fixture has four stems; a split song shows the
    // lead and backing vocal names, the longest there are).
    await page.evaluate(async (c) => {
      const i18n = await import("/js/i18n.js");
      await i18n.setLanguage(c);
      const names = ["original", "vocals", "lead_vocals", "backing_vocals", "drums", "bass", "guitar", "piano", "other"]
        .map((s) => i18n.t(`stem.${s}`))
        .sort((a, b) => b.length - a.length);
      const els = [...document.querySelectorAll(".mixer-column .lane-header[data-stem]:not(.hidden) .mx-head .mx-name")];
      els.forEach((e, i) => { e.textContent = names[i % names.length]; });
    }, code);
    await page.waitForTimeout(50);
    const h = await headers(page);
    expectHeaders(h, `language ${code}`);
    // Whole on its line, not cut to an ellipsis.
    for (const r of h.rows) expect(r.controls.name.cut, `language ${code}, ${r.stem}: name cut`).toBe(false);
  }
  expect(errors).toEqual([]);
});
