// Performance's two tabs over the timeline card (static/js/streamer/streamerTabs.js):
// Timeline (I play, the zoom, the sections, the ruler, the Video lane and the
// take lanes) and Effects (Hear myself and the live effects, which are no
// longer in the Recording & Output panel).
//
// 1. Timeline shows by default; the tabs are WAI-ARIA tabs (roles, the
//    panels linked, aria-selected, one tab in the Tab order).
// 2. The keyboard: Left and Right (wrapping), Home and End.
// 3. The tab chosen comes back after a reload; a store that throws still
//    renders Timeline and switches.
// 4. The Timeline shown again is measured again: the same zoomed width and
//    scroll as before, and the new width after a resize while it was hidden.
// 5. A narrow workspace: the tabs take a row of their own, nothing overlaps
//    and the page never scrolls sideways.
// 6. The playhead's time label sits in the sections' row, between the top row
//    (tabs, I play, zoom, Fit) and the ruler, whole and in view at both ends
//    and zoomed and scrolled; the section markers keep their row and click.
//
// The live chain and the monitor playing on across a switch, and a take
// recorded while Effects shows reaching the timeline, are in
// performance-input-fx.spec.mjs (it has the fake input they need).

import { test, expect } from "@playwright/test";
import { openStudio, chooseMode, leaveModeView, reopenLibraryInPerformance } from "./helpers.mjs";
import { removeTracks } from "./recording-helpers.mjs";

test.afterEach(async ({ request }) => removeTracks(request));

const sm = (page, sel) => page.locator(`#streamer ${sel}`);
const tab = (page, name) => page.locator(`#streamerTab-${name}`);

async function openPerformance(page) {
  await chooseMode(page, "streamer");
  await expect(page.locator("#streamer")).toBeVisible();
}

/** What the timeline measured: the song column's width, its scroll and the first lane's drawn width. */
const measured = (page) => page.evaluate(() => {
  const s = document.querySelector("#streamer .sm-tl-scroll");
  const lane = document.querySelector("#streamer .sm-tl-lane-0");
  const mask = document.querySelector("#streamer .sm-tl-mask");
  return {
    client: s.clientWidth,
    scrollW: s.scrollWidth,
    left: s.scrollLeft,
    lane: parseFloat(lane.style.width) || 0,
    label: mask.offsetWidth + 8,
    ticks: document.querySelectorAll("#streamer .sm-tl-ruler .sm-tick").length,
  };
});

const overlaps = (a, b) => a && b && a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;

const seek = (page, s) => page.evaluate(async (x) => (await import("/js/transport.js")).setPlayheadTime(x), s);
const duration = (page) => page.evaluate(async () => (await import("/js/state.js")).totalDuration);

/**
 * The playhead's time label against the top row and the ruler: it sits
 * under the tabs, I play, the zoom and Fit, over the ruler, and inside the
 * part of the song in view (right of the label column).
 */
async function flagPlace(page) {
  const box = (sel) => sm(page, sel).boundingBox();
  const flag = await box(".sm-tl-playhead-time");
  const line = await box(".sm-tl-playhead-line");
  const ruler = await box(".sm-tl-ruler");
  const scroll = await box(".sm-tl-scroll");
  const mask = await box(".sm-tl-mask");
  const top = [await box(".sm-tabs"), await box(".sm-part"), await box(".sm-zoom"), await box(".sm-fit")];
  const topBottom = Math.max(...top.map((b) => b.y + b.height));
  // The mask spans the label column and the gap after it (its margin-right
  // is -8 px), so its right edge is where the song's column shows from.
  const viewLeft = Math.max(ruler.x, mask.x + mask.width);
  const viewRight = Math.min(ruler.x + ruler.width, scroll.x + (await sm(page, ".sm-tl-scroll").evaluate((s) => s.clientWidth)));
  return {
    clear: top.every((b) => !overlaps(flag, b)),
    belowTop: flag.y >= topBottom - 0.5,
    aboveRuler: flag.y + flag.height <= ruler.y + 0.5,
    inView: flag.x >= viewLeft - 1 && flag.x + flag.width <= viewRight + 1 ? true : { flag: [flag.x, flag.x + flag.width], view: [viewLeft, viewRight] },
    onLine: line.x >= flag.x - 1 && line.x + line.width <= flag.x + flag.width + 1,
    // The line meets the label and runs down through the ruler.
    joined: line.y <= flag.y + flag.height + 0.5 && line.y + line.height > ruler.y + ruler.height,
  };
}

const allTrue = (o) => Object.entries(o).filter(([, v]) => v !== true).map(([k, v]) => (v === false ? k : `${k} ${JSON.stringify(v)}`));

test.describe("Performance tabs", () => {
  test("Timeline shows by default, as WAI-ARIA tabs; the live effects are not in the side panel", async ({ page }) => {
    await page.setViewportSize({ width: 1800, height: 1020 });
    await openStudio(page);
    await page.evaluate(() => localStorage.removeItem("stemdeck.streamer.tab"));
    await openPerformance(page);
    const list = sm(page, ".sm-tabs");
    await expect(list).toHaveAttribute("role", "tablist");
    await expect(list).toHaveAttribute("aria-label", "Timeline and effects");
    await expect(tab(page, "timeline")).toHaveText("Timeline");
    await expect(tab(page, "effects")).toHaveText("Effects");
    for (const [name, panel] of [["timeline", "#streamerTimelinePanel"], ["effects", "#streamerEffectsPanel"]]) {
      await expect(tab(page, name)).toHaveAttribute("role", "tab");
      await expect(tab(page, name)).toHaveAttribute("aria-controls", panel.slice(1));
      await expect(page.locator(panel)).toHaveAttribute("role", "tabpanel");
      await expect(page.locator(panel)).toHaveAttribute("aria-labelledby", `streamerTab-${name}`);
    }
    await expect(tab(page, "timeline")).toHaveAttribute("aria-selected", "true");
    await expect(tab(page, "timeline")).toHaveAttribute("tabindex", "0");
    await expect(tab(page, "effects")).toHaveAttribute("aria-selected", "false");
    await expect(tab(page, "effects")).toHaveAttribute("tabindex", "-1");
    // Timeline: I play, the zoom, the ruler, the Video lane and the lanes.
    for (const sel of [".sm-part", ".sm-zoom", ".sm-tl-ruler", ".sm-tl-label-video", ".sm-tl-lane-0"]) await expect(sm(page, sel)).toBeVisible();
    await expect(page.locator("#streamerEffectsPanel")).toBeHidden();
    // Hear myself and the live effects are in the Effects panel only.
    await expect(sm(page, ".sm-side #streamerHear, .sm-side #streamerFx")).toHaveCount(0);
    await expect(page.locator("#streamerEffectsPanel #streamerHear")).toHaveCount(1);
    await expect(page.locator("#streamerEffectsPanel #streamerFx")).toHaveCount(1);
    // The side panel keeps the sources, the meter and the output settings.
    for (const sel of ["#streamerVideoSrc", "#streamerAudioSrc", ".sm-meter", "#streamerRes", "#streamerFps"]) await expect(sm(page, `.sm-side ${sel}`)).toBeVisible();
    // Effects: its panel shows, the timeline's hides (kept, not removed).
    await tab(page, "effects").click();
    await expect(tab(page, "effects")).toHaveAttribute("aria-selected", "true");
    await expect(page.locator("#streamerEffectsPanel")).toBeVisible();
    await expect(page.locator("#streamerHear")).toBeVisible();
    await expect(page.locator("#streamerFx .fx-add")).toBeVisible();
    await expect(page.locator("#streamerTimelinePanel")).toBeHidden();
    await expect(sm(page, ".sm-tl-ruler")).toHaveCount(1);
    // The tabs and I play never cover one another.
    await tab(page, "timeline").click();
    expect(overlaps(await sm(page, ".sm-tabs").boundingBox(), await sm(page, ".sm-part").boundingBox())).toBe(false);
    expect(overlaps(await sm(page, ".sm-part").boundingBox(), await sm(page, ".sm-zoom").boundingBox())).toBe(false);
  });

  test("the keyboard: Left and Right wrap, Home and End, focus follows", async ({ page }) => {
    await openStudio(page);
    await openPerformance(page);
    await tab(page, "timeline").focus();
    const t0 = await page.evaluate(async () => (await import("/js/transport.js")).transport().getCurrentTime());
    await page.keyboard.press("ArrowRight");
    await expect(tab(page, "effects")).toBeFocused();
    await expect(tab(page, "effects")).toHaveAttribute("aria-selected", "true");
    await expect(page.locator("#streamerEffectsPanel")).toBeVisible();
    await page.keyboard.press("ArrowRight");
    await expect(tab(page, "timeline")).toBeFocused();
    await expect(tab(page, "timeline")).toHaveAttribute("aria-selected", "true");
    await page.keyboard.press("ArrowLeft");
    await expect(tab(page, "effects")).toHaveAttribute("aria-selected", "true");
    await page.keyboard.press("Home");
    await expect(tab(page, "timeline")).toHaveAttribute("aria-selected", "true");
    await page.keyboard.press("End");
    await expect(tab(page, "effects")).toHaveAttribute("aria-selected", "true");
    await expect(tab(page, "effects")).toBeFocused();
    // The arrows and Home and End stayed the tabs': the playhead did not move.
    const t1 = await page.evaluate(async () => (await import("/js/transport.js")).transport().getCurrentTime());
    expect(Math.abs(t1 - t0)).toBeLessThan(0.05);
  });

  test("the tab chosen comes back after a reload and after leaving Performance", async ({ page }) => {
    await openStudio(page);
    await openPerformance(page);
    await tab(page, "effects").click();
    await expect.poll(() => page.evaluate(() => localStorage.getItem("stemdeck.streamer.tab"))).toBe("effects");
    await leaveModeView(page);
    await expect(page.locator("#streamer")).toBeHidden();
    await openPerformance(page);
    await expect(tab(page, "effects")).toHaveAttribute("aria-selected", "true");
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(".cat-item").first().click();
    await page.waitForFunction(() => !/\/\s*00:00\s*$/.test(document.querySelector("#t-time")?.textContent || "00:00 / 00:00"), null, { timeout: 20000 });
    await openPerformance(page);
    await expect(tab(page, "effects")).toHaveAttribute("aria-selected", "true");
    await expect(page.locator("#streamerEffectsPanel")).toBeVisible();
    await tab(page, "timeline").click();
    await expect.poll(() => page.evaluate(() => localStorage.getItem("stemdeck.streamer.tab"))).toBe("timeline");
  });

  test("a store that throws: Timeline shows and the tabs still switch", async ({ page }) => {
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.addInitScript(() => {
      const get = Storage.prototype.getItem;
      const set = Storage.prototype.setItem;
      Storage.prototype.getItem = function (k) {
        if (k === "stemdeck.streamer.tab") throw new Error("blocked");
        return get.call(this, k);
      };
      Storage.prototype.setItem = function (k, v) {
        if (k === "stemdeck.streamer.tab") throw new Error("blocked");
        return set.call(this, k, v);
      };
    });
    await openStudio(page);
    await openPerformance(page);
    await expect(tab(page, "timeline")).toHaveAttribute("aria-selected", "true");
    await tab(page, "effects").click();
    await expect(tab(page, "effects")).toHaveAttribute("aria-selected", "true");
    await expect(page.locator("#streamerEffectsPanel")).toBeVisible();
    await tab(page, "timeline").click();
    await expect(sm(page, ".sm-tl-ruler")).toBeVisible();
    expect(errors).toEqual([]);
  });

  test("the Timeline shown again is measured again: zoom, scroll, Fit and a resize while hidden", async ({ page }) => {
    // A narrow window leaves the 6 s fixture room to zoom (streamer.spec.mjs),
    // with the library that Performance folds there opened again by hand.
    await page.setViewportSize({ width: 1024, height: 900 });
    await openStudio(page);
    await openPerformance(page);
    await reopenLibraryInPerformance(page);
    for (let i = 0; i < 10; i++) await sm(page, ".sm-zoom-in").click();
    await expect(sm(page, ".sm-zoom-track")).toHaveAttribute("aria-valuenow", "100");
    await expect.poll(async () => { const m = await measured(page); return m.scrollW > m.client + 40; }).toBe(true);
    await page.evaluate(() => {
      const s = document.querySelector("#streamer .sm-tl-scroll");
      s.scrollLeft = Math.round((s.scrollWidth - s.clientWidth) / 2);
    });
    await page.waitForTimeout(200);
    const before = await measured(page);
    expect(before.left).toBeGreaterThan(10);
    expect(before.lane).toBeGreaterThan(0);
    await tab(page, "effects").click();
    await page.waitForTimeout(300);
    await tab(page, "timeline").click();
    // The same width, scroll position, lane and ruler as before it hid.
    await expect.poll(async () => (await measured(page)).scrollW).toBe(before.scrollW);
    await expect.poll(async () => Math.abs((await measured(page)).left - before.left)).toBeLessThanOrEqual(2);
    await expect.poll(async () => Math.abs((await measured(page)).lane - before.lane)).toBeLessThanOrEqual(1);
    const back = await measured(page);
    console.log(`[perf-tabs] before ${JSON.stringify(before)}, back ${JSON.stringify(back)}`);
    expect(back.ticks).toBe(before.ticks);
    // Fit still fits.
    await sm(page, ".sm-fit").click();
    await expect.poll(async () => { const m = await measured(page); return m.scrollW <= m.client + 1; }).toBe(true);
    const fitted = await measured(page);
    // A wider window while Effects shows: back on Timeline, the lane is
    // drawn to the new width (the fitted view less its 16 px ends).
    await tab(page, "effects").click();
    await page.setViewportSize({ width: 1400, height: 900 });
    await page.waitForTimeout(300);
    await tab(page, "timeline").click();
    await expect.poll(async () => (await measured(page)).client).toBeGreaterThan(fitted.client + 100);
    await expect.poll(async () => {
      const m = await measured(page);
      return Math.abs(m.lane - (m.client - m.label - 32));
    }).toBeLessThanOrEqual(2);
  });

  test("the playhead's time sits between the top row and the ruler: at both ends, zoomed and scrolled", async ({ page }) => {
    await page.setViewportSize({ width: 1800, height: 1020 });
    await openStudio(page);
    await openPerformance(page);
    await page.evaluate(async () => { await (await import("/js/state.js")).audioEngine?.ready; });
    // No "No sections yet" line in Performance: the row is the label's.
    await expect(sm(page, ".sm-tl-no-sections")).toHaveCount(0);
    await expect(sm(page, ".sm-tl-markers")).toBeVisible();
    const dur = await duration(page);
    for (const at of [0, dur / 2, dur]) {
      await seek(page, at);
      await page.waitForTimeout(150);
      await expect.poll(async () => allTrue(await flagPlace(page)), { message: `at ${at}s` }).toEqual([]);
    }
    // Zoomed in and scrolled: the label stays in view, right of the labels.
    await page.setViewportSize({ width: 1024, height: 900 });
    await reopenLibraryInPerformance(page);
    for (let i = 0; i < 10; i++) await sm(page, ".sm-zoom-in").click();
    await expect.poll(async () => { const m = await measured(page); return m.scrollW > m.client + 40; }).toBe(true);
    await page.evaluate(() => {
      const s = document.querySelector("#streamer .sm-tl-scroll");
      s.scrollLeft = Math.round((s.scrollWidth - s.clientWidth) / 2);
    });
    const leftEdge = await page.evaluate(async () => {
      const s = document.querySelector("#streamer .sm-tl-scroll");
      const { pixelsPerSecond } = await import("/js/streamer/streamerModel.js");
      const { totalDuration } = await import("/js/state.js");
      const label = document.querySelector("#streamer .sm-tl-mask").offsetWidth + 8;
      return (s.scrollLeft + 4) / pixelsPerSecond(totalDuration, s.clientWidth - label, 1);
    });
    await seek(page, leftEdge);
    await expect.poll(async () => allTrue(await flagPlace(page))).toEqual([]);
    // The Effects tab: the timeline, its playhead with it, does not show.
    await tab(page, "effects").click();
    await expect(sm(page, ".sm-tl-playhead-time")).toBeHidden();
    await tab(page, "timeline").click();
    await expect(sm(page, ".sm-tl-playhead-time")).toBeVisible();
  });

  test("with sections: the markers keep their row and their click, the time label passes over them", async ({ page }) => {
    await page.setViewportSize({ width: 1800, height: 1020 });
    await openStudio(page);
    await page.evaluate(async () => { await (await import("/js/state.js")).audioEngine?.ready; });
    // Sections set in memory only (nothing is saved without an edit).
    await page.evaluate(async () => {
      const { initSections } = await import("/js/sections.js");
      const { currentJobId, totalDuration } = await import("/js/state.js");
      const d = totalDuration;
      initSections(currentJobId, [
        { id: "a", start: 0, end: d / 2, kind: "verse", color: "#4a7fff" },
        { id: "b", start: d / 2, end: d, kind: "chorus", color: "#ff7a4a" },
      ], d);
    });
    await openPerformance(page);
    await expect(sm(page, ".sm-marker")).toHaveCount(2);
    await expect(sm(page, ".sm-tl-no-sections")).toHaveCount(0);
    const dur = await duration(page);
    await seek(page, dur / 2);
    await expect.poll(async () => allTrue(await flagPlace(page))).toEqual([]);
    // The label over the second marker never takes its click.
    await seek(page, dur * 0.9);
    await sm(page, ".sm-marker").nth(1).click();
    await expect.poll(async () => Math.abs((await page.evaluate(async () => (await import("/js/transport.js")).transport().getCurrentTime())) - dur / 2)).toBeLessThan(0.1);
    // Both markers sit in the row over the ruler, under the top row.
    const ruler = await sm(page, ".sm-tl-ruler").boundingBox();
    const tabsBox = await sm(page, ".sm-tabs").boundingBox();
    for (let i = 0; i < 2; i++) {
      const m = await sm(page, ".sm-marker").nth(i).boundingBox();
      expect(m.y).toBeGreaterThanOrEqual(tabsBox.y + tabsBox.height - 0.5);
      expect(m.y + m.height).toBeLessThanOrEqual(ruler.y + 0.5);
    }
  });

  test("a narrow workspace: the tabs take their own row, nothing overlaps, no sideways scroll", async ({ page }) => {
    // The layer here is under 680 px wide (the library beside it).
    await page.setViewportSize({ width: 1000, height: 900 });
    await openStudio(page);
    await openPerformance(page);
    const tabs = await sm(page, ".sm-tabs").boundingBox();
    const part = await sm(page, ".sm-part").boundingBox();
    const zoom = await sm(page, ".sm-zoom").boundingBox();
    expect(tabs && part && zoom).toBeTruthy();
    expect(overlaps(tabs, part)).toBe(false);
    expect(overlaps(tabs, zoom)).toBe(false);
    expect(overlaps(part, zoom)).toBe(false);
    await expect(sm(page, ".sm-tl-ruler")).toBeVisible();
    // The playhead's time stays under the tabs' own row and I play, over the ruler.
    await expect.poll(async () => allTrue(await flagPlace(page))).toEqual([]);
    await tab(page, "effects").click();
    await expect(page.locator("#streamerHear")).toBeVisible();
    await page.locator("#streamerFx .fx-add").scrollIntoViewIfNeeded();
    await expect(page.locator("#streamerFx .fx-add")).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  });
});
