// The effects chain in the studio (Advanced mode), against the real backend.
//
// 1. Practice, Karaoke and Performance build no chain at all, even with a
//    chain saved in the song's Advanced record; Advanced builds it (both
//    engines). An export carries prints= only in Advanced.
// 2. The chain strip: add, keyboard reorder (focus stays, the live region
//    says where), drag reorder, per-pedal on and off, remove with Delete,
//    instrument override, presets, at most 8, saved with the song and back
//    after a reload, rows aligned in both columns.
// 3. The Guide explains the new controls.
// 4. An export prints the lane: the server's mixdown of the printed lane
//    nulls against the browser's own offline render below -60 dB.

import { test, expect } from "@playwright/test";
import { openStudio, chooseMode, leaveModeView, exportUi, JOB_ID } from "./helpers.mjs";
import { PEDALS, presetsFor, pedalDefaults, controlsFor } from "../../static/js/fx/pedalSpecs.js";
import { TRANSLATIONS } from "../../static/js/i18n.js";

test.use({
  launchOptions: { args: ["--mute-audio", "--autoplay-policy=no-user-gesture-required"] },
  viewport: { width: 1600, height: 900 },
});

const MIX_KEY = `stemdeck:mix:${JOB_ID}:daw`;
// One pedal of every type, each on its first bass preset: the gating test
// holds for every pedal, and the export test runs once per type.
const bassPreset = (type) => presetsFor(type, "bass")[0];
const pedalOf = (type, i) => ({ iid: `p${String(i).padStart(3, "0")}`, type, on: true, preset: bassPreset(type).id, p: { ...pedalDefaults(type, "bass"), ...bassPreset(type).p } });
const TYPES = Object.keys(PEDALS);
const chainOf = (types) => ({ id: "chain", v: 1, inst: null, on: true, trim: 0, pedals: types.map(pedalOf) });
const SAVED_CHAIN = chainOf(TYPES);

/** A saved Advanced record with a chain on the bass, before any script runs. */
async function seedChain(page, { mode = null, engine = null, chain = SAVED_CHAIN } = {}) {
  await page.addInitScript(({ key, chain, mode, engine }) => {
    if (sessionStorage.getItem("fx-seeded")) return;
    sessionStorage.setItem("fx-seeded", "1");
    localStorage.setItem(key, JSON.stringify({ bass: { volume: 1, muted: false, soloed: false, pitch: 0, fx: [chain] } }));
    if (mode) localStorage.setItem("stemdeck.mode", mode);
    if (engine) localStorage.setItem("stemdeck.audioEngine", engine);
  }, { key: MIX_KEY, chain, mode, engine });
}

const hostCount = (page) => page.evaluate(async () => (await import("/js/state.js")).audioEngine?.chainHostCount?.() ?? -1);
const chainLatency = (page) => page.evaluate(async () => (await import("/js/state.js")).audioEngine?.chainLatency?.() ?? -1);
const laneChain = (page, lane = "bass") => page.evaluate(async (l) => (await import("/js/fxEngine.js")).getLaneChain(l), lane);

/**
 * The URL the next Export Mix hands to the desktop transfer (stubTauri, so
 * the test settles the dialog and the transfer and the menu is free again).
 * With effects, the prints are made between the two: up to a few seconds.
 */
async function exportUrl(page) {
  const ui = exportUi(page);
  const before = await page.evaluate(() => window.__e2e.callsFor("download_to_path").length);
  await ui.open();
  await ui.fmt("wav").click();
  await ui.mix.click();
  await expect.poll(() => page.evaluate(() => window.__e2e.pickPending())).toBe(true);
  await page.evaluate(() => window.__e2e.choosePath());
  await expect.poll(() => page.evaluate(() => window.__e2e.callsFor("download_to_path").length), {
    timeout: 30000, message: "the transfer never started (a failed print shows in the error box)",
  }).toBe(before + 1);
  const url = await page.evaluate(() => window.__e2e.callsFor("download_to_path").at(-1).args.url);
  await page.evaluate(() => window.__e2e.finishSave());
  await expect(ui.label).toHaveText("Export Mix", { timeout: 8000 });
  return url;
}

test.describe("effects chain in the studio", () => {
  test("Practice, Karaoke and Performance build no chain; Advanced does", async ({ page }) => {
    await seedChain(page, { mode: "practice" });
    await openStudio(page, { tauri: true });
    // Practice: nothing in the graph, no tab, and the export is the one of
    // 0.21.0 (no prints=).
    await expect.poll(() => hostCount(page)).toBe(0);
    await expect(page.locator(".fx-tab")).toHaveCount(0);
    expect(await exportUrl(page)).not.toContain("prints=");

    expect(await chainLatency(page)).toBe(0);
    await chooseMode(page, "daw");
    await expect.poll(() => hostCount(page), { timeout: 10000 }).toBeGreaterThan(0);
    // The distortion's 4x shaper: every lane is delayed to match (D8).
    await expect.poll(() => chainLatency(page)).toBeGreaterThan(0);
    // The rail's EQ tab carries the effects' mark with both rows closed (the
    // FX tab is in the EQ row, not a fifth rail cell).
    await expect(page.locator('.mx-rail .eq-tab[data-eq-lane="bass"]')).toHaveClass(/fx-on/);
    await expect(page.locator(".fx-tab")).toHaveCount(0);
    expect(await exportUrl(page)).toMatch(/prints=bass%3A[0-9a-f]{40}/);

    await chooseMode(page, "karaoke");
    await expect.poll(() => hostCount(page), { timeout: 10000 }).toBe(0);
    await leaveModeView(page);
    await expect.poll(() => hostCount(page), { timeout: 10000 }).toBeGreaterThan(0);

    await chooseMode(page, "streamer");
    await expect.poll(() => hostCount(page), { timeout: 10000 }).toBe(0);
    await leaveModeView(page);
    await expect.poll(() => hostCount(page), { timeout: 10000 }).toBeGreaterThan(0);

    // The saved chain was never touched by the other modes.
    const saved = await page.evaluate((k) => JSON.parse(localStorage.getItem(k)), MIX_KEY);
    expect(saved.bass.fx.find((x) => x.id === "chain").pedals.map((p) => p.type)).toEqual(TYPES);
  });

  test("the full-decode engine carries the chain too", async ({ page }) => {
    await seedChain(page, { engine: "fulldecode" });
    await openStudio(page);
    await expect.poll(() => hostCount(page), { timeout: 15000 }).toBeGreaterThan(0);
    await chooseMode(page, "practice");
    await expect.poll(() => hostCount(page), { timeout: 10000 }).toBe(0);
  });

  test("the chain strip: add, reorder by keys and by drag, on and off, remove, instrument, presets, the cap, saved", async ({ page }) => {
    await openStudio(page);
    // The FX tab is the second tab of the lane's EQ row: open that first.
    await page.locator('.eq-tab[data-eq-lane="bass"]').click();
    const tab = page.locator('.eq-panel-row[data-eq-lane="bass"] .eq-fx-slot > .fx-tab[data-fx-lane="bass"]');
    await tab.click();
    const row = page.locator('.fx-panel-lane[data-fx-lane="bass"]');
    await expect(row).toBeVisible();
    const add = row.locator(".fx-add");
    const addDelay = async () => { await add.click(); await row.locator('.fx-add-item[data-type="delay"]').click(); };
    await addDelay();
    await addDelay();
    await expect(row.locator(".fx-card")).toHaveCount(2);
    await expect.poll(() => hostCount(page), { timeout: 10000 }).toBeGreaterThan(0);
    let desc = await laneChain(page);
    const [first, second] = desc.pedals.map((p) => p.iid);

    // Keyboard: Alt+Left moves the second card first; focus stays on it.
    await row.locator(`.fx-card[data-iid="${second}"] .fx-card-main`).focus();
    await page.keyboard.press("Alt+ArrowLeft");
    await expect.poll(async () => (await laneChain(page)).pedals.map((p) => p.iid)).toEqual([second, first]);
    expect(await page.evaluate(() => document.activeElement?.closest(".fx-card")?.dataset.iid)).toBe(second);
    await expect(row.locator(".fx-live")).toContainText("position 1 of 2");

    // Drag: the card now second goes back in front.
    const from = await row.locator(`.fx-card[data-iid="${first}"] .fx-card-grip`).boundingBox();
    const to = await row.locator(`.fx-card[data-iid="${second}"]`).boundingBox();
    await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
    await page.mouse.down();
    await page.mouse.move(to.x + 4, to.y + to.height / 2, { steps: 8 });
    await page.mouse.up();
    await expect.poll(async () => (await laneChain(page)).pedals.map((p) => p.iid)).toEqual([first, second]);

    // On and off: a light and a word.
    const pw = row.locator(`.fx-card[data-iid="${first}"] .fx-card-power`);
    await pw.click();
    await expect(row.locator(`.fx-card[data-iid="${first}"] .fx-card-power`)).toHaveText("Off");
    expect((await laneChain(page)).pedals[0].on).toBe(false);

    // Delete removes the focused card.
    await row.locator(`.fx-card[data-iid="${first}"] .fx-card-main`).focus();
    await page.keyboard.press("Delete");
    await expect(row.locator(".fx-card")).toHaveCount(1);
    await expect(row.locator(".fx-live")).toContainText("removed");

    // Instrument: the bass lane defaults to bass; a voice override brings
    // Ducking and the voice presets.
    await expect(page.locator('.fx-panel-row[data-fx-lane="bass"] .fx-inst option').first()).toHaveText("Auto (Bass)");
    await expect(row.locator('.fx-knob[data-ctl="duck"]')).toHaveCount(0);
    await page.locator('.fx-panel-row[data-fx-lane="bass"] .fx-inst').selectOption("voice");
    await expect(row.locator('.fx-knob[data-ctl="duck"]')).toHaveCount(1);
    await row.locator(".fx-preset").selectOption("voice.delay.ducked");
    desc = await laneChain(page);
    expect(desc.inst).toBe("voice");
    expect(desc.pedals[0].p.duck).toBe(10);
    expect(desc.pedals[0].preset).toBe("voice.delay.ducked");

    // At most eight.
    for (let i = 0; i < 7; i++) await addDelay();
    await expect(row.locator(".fx-card")).toHaveCount(8);
    await expect(add).toBeDisabled();

    // Rows line up in both columns, the EQ row and the effects row under it.
    const s = await page.evaluate(() => {
      const id = (e) => (e.dataset.fxLane ? `fx:${e.dataset.fxLane}` : e.dataset.eqLane ? `eq:${e.dataset.eqLane}` : e.dataset.stem || "?");
      const read = (sel) => [...document.querySelectorAll(sel)]
        .filter((e) => !e.classList.contains("hidden") && e.getBoundingClientRect().height > 0)
        .map((e) => { const b = e.getBoundingClientRect(); return { id: id(e), top: b.top, h: b.height }; })
        .sort((a, b) => a.top - b.top);
      return { mix: read(".mixer-column .lane-header"), wave: read(".stem-waveform-layer > .stem-waveform-row") };
    });
    expect(s.wave.map((r) => r.id)).toEqual(s.mix.map((r) => r.id));
    const i = s.mix.findIndex((r) => r.id === "bass");
    expect(s.mix.slice(i, i + 3).map((r) => r.id)).toEqual(["bass", "eq:bass", "fx:bass"]);
    const worst = Math.max(...s.mix.map((r, k) => Math.max(Math.abs(r.top - s.wave[k].top), Math.abs(r.h - s.wave[k].h))));
    expect(worst).toBeLessThanOrEqual(1);

    // Closing the EQ row closes the effects row with it; opening it again
    // brings both back. The mark stays on the rail's EQ tab meanwhile.
    await page.locator('.eq-tab[data-eq-lane="bass"]').click();
    await expect(page.locator('.eq-panel-lane[data-eq-lane="bass"]')).toHaveCount(0);
    await expect(row).toHaveCount(0);
    await expect(page.locator('.mx-rail .eq-tab[data-eq-lane="bass"]')).toHaveClass(/fx-on/);
    await page.locator('.eq-tab[data-eq-lane="bass"]').click();
    await expect(row.locator(".fx-card")).toHaveCount(8);

    // Saved with the song, in the Advanced record, and back after a reload.
    await expect.poll(async () => page.evaluate((k) => JSON.parse(localStorage.getItem(k) || "null")?.bass?.fx?.find((x) => x.id === "chain")?.pedals?.length, MIX_KEY)).toBe(8);
    await openStudio(page);
    await expect(page.locator('.fx-panel-lane[data-fx-lane="bass"] .fx-card')).toHaveCount(8);
    expect((await laneChain(page)).inst).toBe("voice");
  });

  test("the Guide explains the effects controls", async ({ page }) => {
    await seedChain(page);
    await openStudio(page);
    await page.locator('.eq-tab[data-eq-lane="bass"]').click();
    await page.locator('.fx-tab[data-fx-lane="bass"]').click();
    const row = page.locator('.fx-panel-lane[data-fx-lane="bass"]');
    await expect(row.locator(".fx-card")).toHaveCount(TYPES.length);
    const tip = page.locator("#helpTip");
    // Guide on or off, checked: while it is on, a click explains instead of acting.
    const guide = async (on) => {
      const btn = page.locator("#helpModeBtn");
      if ((await btn.getAttribute("aria-pressed")) !== String(on)) await (on ? btn.click() : page.keyboard.press("Escape"));
      await expect(btn).toHaveAttribute("aria-pressed", String(on));
    };
    const en = (key) => TRANSLATIONS.en[key];
    await guide(true);
    const cases = [
      [page.locator('.fx-tab[data-fx-lane="bass"]'), "help.fxTab"],
      [page.locator('.mx-rail .eq-tab[data-eq-lane="bass"]'), "help.eqTab"],
      [row.locator(".fx-card-main").first(), "help.fxCard"],
      [row.locator(".fx-card-power").first(), "help.fxCardPower"],
      [row.locator(".fx-add"), "help.fxAdd"],
      [row.locator(".fx-preset"), "help.fxPreset"],
      [page.locator('.fx-panel-row[data-fx-lane="bass"] .fx-inst'), "help.fxInstrument"],
      [page.locator('.fx-panel-row[data-fx-lane="bass"] .fx-trim .eq-knob-dial'), "help.fxTrim"],
    ];
    for (const [loc, key] of cases) {
      await loc.hover();
      await expect(tip).toContainText(en(key));
    }
    // Every control of every pedal, each with its own sentence (a "Rate" of
    // one pedal is not explained by another's).
    for (const [i, type] of TYPES.entries()) {
      await guide(false);
      await row.locator(".fx-card-main").nth(i).click();
      await expect(row.locator(`.fx-pedal[data-type="${type}"]`)).toBeVisible();
      await guide(true);
      for (const c of controlsFor(type, "bass")) {
        const loc = c.kind === "select"
          ? row.locator(`.fx-pedal[data-type="${type}"] .fx-select[data-ctl="${c.id}"]`)
          : row.locator(`.fx-pedal[data-type="${type}"] .fx-knob[data-ctl="${c.id}"] .eq-knob-dial`);
        await loc.hover();
        await expect(tip, `${type} ${c.id}`).toContainText(en(c.help));
        await expect(tip.locator("b"), `${type} ${c.id} title`).toHaveText(en(c.name));
      }
    }
  });

  for (const type of TYPES) test(`an export prints a ${type}, and the server's render of it nulls against the offline render`, async ({ page }) => {
    await seedChain(page, { chain: chainOf([type]) });
    await openStudio(page, { tauri: true });
    await expect.poll(() => hostCount(page), { timeout: 10000 }).toBeGreaterThan(0);
    const url = await exportUrl(page);
    const id = /prints=bass%3A([0-9a-f]{40})/.exec(url)?.[1];
    expect(id, url).toBeTruthy();
    // The printed lane alone, rendered by the server (not stubbed: this is
    // the request context, outside the page's routes).
    const res = await page.request.get(`/api/jobs/${JOB_ID}/mixdown.wav?stems=bass&gains=1.000&prints=bass:${id}`);
    expect(res.status()).toBe(200);
    const b64 = (await res.body()).toString("base64");
    const r = await page.evaluate(async ({ b64, job }) => {
      const { renderChain } = await import("/js/fx/fxPrint.js");
      const fx = await import("/js/fxEngine.js");
      const [item] = fx.exportChainLanes(["bass"]);
      const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
      const exported = await new OfflineAudioContext(2, 1, 44100).decodeAudioData(bytes.buffer);
      const stem = await (await fetch(`/api/jobs/${job}/stems/bass.wav`)).arrayBuffer();
      const decoded = await new OfflineAudioContext(2, 1, 44100).decodeAudioData(stem);
      const { channels, frames } = await renderChain({ buffer: decoded, desc: item.desc, inst: item.inst, bpm: item.bpm, rate: 44100 });
      let num = 0, den = 0, peak = 0;
      for (let ch = 0; ch < 2; ch++) {
        const e = exported.getChannelData(ch);
        for (let i = 0; i < Math.min(frames, e.length); i++) { const d = e[i] - channels[ch][i]; num += d * d; den += channels[ch][i] ** 2; peak = Math.max(peak, Math.abs(channels[ch][i])); }
      }
      const { chainTailSec } = await import("/js/fx/chainModel.js");
      return { nullDb: 10 * Math.log10(num / den), exportedFrames: exported.length, frames, stemFrames: decoded.length, peak, tailFrames: Math.ceil(chainTailSec(item.desc, item.bpm) * 44100) };
    }, { b64, job: JOB_ID });
    console.log(`[fx-studio] ${type}: export null ${r.nullDb.toFixed(1)} dB; print ${r.frames} frames (stem ${r.stemFrames}); export ${r.exportedFrames}; peak ${r.peak.toFixed(3)}`);
    // Below the export limiter's -1 dBFS, so the comparison is fair.
    expect(r.peak).toBeLessThan(0.89);
    expect(r.exportedFrames).toBe(r.frames);
    // The print is the lane plus the chain's tail (a phaser has none).
    expect(r.frames).toBe(r.stemFrames + r.tailFrames);
    expect(r.nullDb).toBeLessThan(-60);
  });

  // The add menu is a popup anchored to "+ Add effect" (fx/chainStrip.js):
  // it used to sit in the strip's row, whose sideways scrolling cut off its
  // first and last items. Each item must be fully on screen and be what the
  // page has at its centre and 4 px inside each corner (the items' rounded corners are not hit-tested), at a normal and a
  // short window.
  for (const size of [{ width: 1280, height: 720 }, { width: 1280, height: 560 }]) {
    test(`the add menu is never clipped in a lane's FX row (${size.width}x${size.height})`, async ({ page }) => {
      await page.setViewportSize(size);
      await page.addInitScript(() => localStorage.setItem("stemdeck.mode", "daw"));
      await openStudio(page);
      await page.locator('.eq-tab[data-eq-lane="bass"]').click();
      await page.locator('.eq-panel-row[data-eq-lane="bass"] .eq-fx-slot > .fx-tab[data-fx-lane="bass"]').click();
      const row = page.locator('.fx-panel-lane[data-fx-lane="bass"]');
      await expect(row).toBeVisible();
      const add = row.locator(".fx-add");
      await add.scrollIntoViewIfNeeded();
      await add.click();
      const menu = row.locator(".fx-add-menu");
      await expect(menu).toBeVisible();
      await expect(add).toHaveAttribute("aria-expanded", "true");
      await expect(menu).toHaveAttribute("role", "menu");
      const r = await page.evaluate(() => {
        const m = document.querySelector(".fx-add-menu");
        const vw = window.innerWidth;
        const vh = window.innerHeight;
        const items = [...m.querySelectorAll(".fx-add-item")];
        const bad = [];
        for (const it of items) {
          const b = it.getBoundingClientRect();
          if (!(b.width > 0 && b.height > 0 && b.left >= 0 && b.top >= 0 && b.right <= vw && b.bottom <= vh)) {
            bad.push(`${it.dataset.type} outside the window (${Math.round(b.top)}..${Math.round(b.bottom)} of ${vh})`);
            continue;
          }
          for (const [x, y] of [[b.left + b.width / 2, b.top + b.height / 2], [b.left + 4, b.top + 4], [b.right - 4, b.top + 4], [b.left + 4, b.bottom - 4], [b.right - 4, b.bottom - 4]]) {
            const hit = document.elementFromPoint(x, y);
            if (!(hit === it || it.contains(hit))) { bad.push(`${it.dataset.type} covered at ${Math.round(x)},${Math.round(y)} by ${hit?.className || hit?.tagName}`); break; }
          }
        }
        const btn = document.querySelector('.fx-panel-lane[data-fx-lane="bass"] .fx-add').getBoundingClientRect();
        const mr = m.getBoundingClientRect();
        return { n: items.length, bad, scrolls: m.scrollHeight > m.clientHeight + 1, below: mr.top >= btn.bottom - 1, above: mr.bottom <= btn.top + 1 };
      });
      console.log(`[fx] add menu at ${size.width}x${size.height}: ${r.n} items, ${r.below ? "below" : r.above ? "above" : "beside"} the button, ${r.bad.length ? r.bad.join("; ") : "all on screen and uncovered"}`);
      expect(r.n).toBe(5);
      expect(r.bad).toEqual([]);
      expect(r.scrolls).toBe(false);
      expect(r.below || r.above).toBe(true);
      // Keyboard unchanged: Down and Up move, Escape closes and focus goes back.
      await expect(menu.locator(".fx-add-item").first()).toBeFocused();
      await page.keyboard.press("ArrowDown");
      await expect(menu.locator(".fx-add-item").nth(1)).toBeFocused();
      await page.keyboard.press("ArrowUp");
      await expect(menu.locator(".fx-add-item").first()).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(menu).toHaveCount(0);
      await expect(add).toBeFocused();
      // A press outside and a scroll close it; an item still adds its pedal.
      await add.click();
      await expect(menu).toBeVisible();
      await row.locator(".fx-io").first().click();
      await expect(menu).toHaveCount(0);
      await add.click();
      await expect(menu).toBeVisible();
      // A scroll event that did not move the button (one arriving a frame
      // late, from bringing the button into view) leaves the menu open.
      await page.evaluate(() => document.getElementById("lanes")?.dispatchEvent(new Event("scroll")));
      await page.waitForTimeout(100);
      await expect(menu).toBeVisible();
      // A scroll that moves the button closes it: the page's own scroller
      // under the button, moved for real (window as the last resort).
      const moved = await page.evaluate(() => {
        const btn = document.querySelector('.fx-panel-lane[data-fx-lane="bass"] .fx-add');
        const before = btn.getBoundingClientRect().top;
        for (let n = btn.parentElement; n; n = n.parentElement) {
          const oy = getComputedStyle(n).overflowY;
          if ((oy === "auto" || oy === "scroll") && n.scrollHeight > n.clientHeight + 2) {
            n.scrollTop += n.scrollTop > 0 ? -30 : 30;
            break;
          }
        }
        if (btn.getBoundingClientRect().top === before) window.scrollBy(0, 30);
        return btn.getBoundingClientRect().top - before;
      });
      if (moved !== 0) await expect(menu).toHaveCount(0);
      else {
        console.log("[fx] nothing under the button could scroll at this size: the moving-scroll case is not checked here");
        await page.keyboard.press("Escape");
      }
      await add.scrollIntoViewIfNeeded();
      await add.click();
      await row.locator('.fx-add-item[data-type="reverb"]').click();
      await expect(row.locator(".fx-card")).toHaveCount(1);
    });
  }
});
