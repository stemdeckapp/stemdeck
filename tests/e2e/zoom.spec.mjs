// Scroll-wheel zoom on the mixer waveforms.
//
// The thing worth guarding here is not that a number changes: it is that the
// bars keep their width. The overview is an SVG whose viewBox width is the bar
// COUNT, so widening it without redrawing stretches every bar by the zoom
// factor. That looks like a zoom at a glance and is really just a fatter
// version of the same picture, which is exactly the failure this feature exists
// to avoid. Every assertion about "art" below is measuring that.

import { test, expect } from "@playwright/test";
import { openStudio, JOB_ID } from "./helpers.mjs";

const wheel = (page, dy, x = 900, y = 400) =>
  page.mouse.move(x, y).then(() => page.mouse.wheel(0, dy));

async function zoomState(page) {
  return page.evaluate(() => {
    const scroller = document.querySelector(".wave-scroll");
    const canvas = document.querySelector(".wave-canvas");
    const svg = document.querySelector(".stem-waveform-row[data-stem] .stem-waveform-svg");
    const rects = svg ? svg.querySelectorAll("rect") : [];
    const box = svg ? svg.getBoundingClientRect() : null;
    return {
      zoomVar: parseFloat(
        getComputedStyle(document.getElementById("lanes")).getPropertyValue("--zoom"),
      ) || 1,
      viewport: Math.round(scroller.clientWidth),
      content: Math.round(canvas.getBoundingClientRect().width),
      scrollLeft: Math.round(scroller.scrollLeft),
      bars: rects.length,
      // One bar occupies one viewBox unit, so its on-screen width is the
      // rendered svg width divided by the bar count. That is the number that
      // must not move when the zoom does.
      barSlotPx: box && rects.length ? +(box.width / rects.length).toFixed(3) : null,
      ticks: document.querySelectorAll("#ruler-time .tick").length,
      tickLabels: [...document.querySelectorAll("#ruler-time .tick-label")].slice(0, 3).map((e) => e.textContent),
      loopDisabled: document.getElementById("t-loop").disabled,
      loopStartDisabled: document.getElementById("t-loop-start").disabled,
    };
  });
}

// Deliberately wide: the bar-count maths only has room to prove itself when the
// panel can hold a few hundred bars.
test.use({ viewport: { width: 1600, height: 900 } });

test.describe("waveform zoom", () => {
  test("a track opens fitted, with the loop tools available", async ({ page }) => {
    await openStudio(page, { tauri: true });
    const s = await zoomState(page);
    expect(s.zoomVar).toBeCloseTo(1, 2);
    expect(s.content).toBe(s.viewport);
    expect(s.loopDisabled).toBe(false);
    expect(s.loopStartDisabled).toBe(false);
  });

  test("scrolling up zooms in and scrolling down comes back", async ({ page }) => {
    await openStudio(page, { tauri: true });
    const start = await zoomState(page);

    await wheel(page, -300);
    const zoomed = await zoomState(page);
    expect(zoomed.zoomVar).toBeGreaterThan(start.zoomVar);
    expect(zoomed.content).toBeGreaterThan(start.content);

    await wheel(page, 300);
    const back = await zoomState(page);
    expect(back.zoomVar).toBeCloseTo(start.zoomVar, 2);
    expect(back.content).toBe(start.content);
  });

  test("the bars keep their width; zooming buys detail, not fatter bars", async ({ page }) => {
    await openStudio(page, { tauri: true });
    const base = await zoomState(page);
    expect(base.bars).toBeGreaterThan(50);

    for (let i = 0; i < 12; i++) await wheel(page, -240);
    const zoomed = await zoomState(page);

    // The proof: same pixels per bar, more bars, wider content.
    expect(zoomed.barSlotPx).toBeCloseTo(base.barSlotPx, 1);
    expect(zoomed.bars).toBeGreaterThan(base.bars * 2);
    expect(zoomed.content).toBeGreaterThan(base.content * 2);
    // Bar count tracks content width, which is what "same art" means here.
    expect(zoomed.bars / base.bars).toBeCloseTo(zoomed.content / base.content, 1);
  });

  test("zoom stops at 5x and never goes below the fitted view", async ({ page }) => {
    await openStudio(page, { tauri: true });
    const fitted = await zoomState(page);

    for (let i = 0; i < 40; i++) await wheel(page, -240);
    const maxed = await zoomState(page);
    expect(maxed.content / fitted.content).toBeCloseTo(5, 1);

    for (let i = 0; i < 60; i++) await wheel(page, 240);
    const floored = await zoomState(page);
    expect(floored.content).toBe(fitted.content);
    expect(floored.scrollLeft).toBe(0);
  });

  test("the pointer stays over the same moment in the track", async ({ page }) => {
    await openStudio(page, { tauri: true });
    const rect = await page.locator(".wave-scroll").boundingBox();
    const anchorX = rect.x + rect.width * 0.75;

    const before = await zoomState(page);
    const timeUnderPointer = (before.scrollLeft + (anchorX - rect.x)) / before.content;

    for (let i = 0; i < 8; i++) await wheel(page, -240, anchorX, rect.y + 60);
    const after = await zoomState(page);
    const nowUnderPointer = (after.scrollLeft + (anchorX - rect.x)) / after.content;

    // Within half a percent of the track: the anchor holds, so zooming toward a
    // bar does not turn into chasing the scrollbar.
    expect(Math.abs(nowUnderPointer - timeUnderPointer)).toBeLessThan(0.005);
  });

  test("the loop tools are inert while zoomed and come back at 1x", async ({ page }) => {
    await openStudio(page, { tauri: true });

    // The ruler, not the lane body: the fixture keeps its loading overlay over
    // the lanes, which would swallow the drag and pass this test for the wrong
    // reason. The ruler is the canonical loop surface anyway.
    const ruler = await page.locator("#ruler-time").boundingBox();
    const dragRuler = async (fromFrac, toFrac) => {
      const y = ruler.y + ruler.height / 2;
      await page.mouse.move(ruler.x + ruler.width * fromFrac, y);
      await page.mouse.down();
      await page.mouse.move(ruler.x + ruler.width * toFrac, y, { steps: 8 });
      await page.mouse.up();
    };

    await dragRuler(0.2, 0.6);
    await expect(page.locator("#t-loop")).toHaveClass(/active/);
    const armed = await page.locator("#loop-region").getAttribute("style");

    await wheel(page, -300);
    const zoomed = await zoomState(page);
    expect(zoomed.loopDisabled).toBe(true);
    expect(zoomed.loopStartDisabled).toBe(true);
    await expect(page.locator("#t-loop")).not.toHaveClass(/active/);
    await expect(page.locator("#loop-region")).toHaveClass(/hidden/);

    // A drag while zoomed must not define a new region.
    await dragRuler(0.05, 0.35);
    await expect(page.locator("#loop-region")).toHaveClass(/hidden/);

    // Back at 1x the original loop returns, unchanged.
    for (let i = 0; i < 30; i++) await wheel(page, 300);
    const back = await zoomState(page);
    expect(back.content).toBe(back.viewport);
    expect(back.loopDisabled).toBe(false);
    await expect(page.locator("#t-loop")).toHaveClass(/active/);
    expect(await page.locator("#loop-region").getAttribute("style")).toBe(armed);
  });

  test("the ruler gets finer as you zoom, and the footer strip does not", async ({ page }) => {
    await openStudio(page, { tauri: true });
    const base = await zoomState(page);
    const footerBefore = await page.locator("#footer-wave-ticks .tick").count();

    for (let i = 0; i < 40; i++) await wheel(page, -240);
    const zoomed = await zoomState(page);

    // Same ticks spread over five screen widths would be a worse ruler the
    // further in you went. More of them, at a finer step.
    expect(zoomed.ticks).toBeGreaterThan(base.ticks);
    expect(zoomed.tickLabels[1]).not.toBe(base.tickLabels[1]);

    // The footer strip always shows the whole track, so its ticks must not move.
    expect(await page.locator("#footer-wave-ticks .tick").count()).toBe(footerBefore);
  });

  test("shift-scroll pans instead of zooming", async ({ page }) => {
    await openStudio(page, { tauri: true });
    for (let i = 0; i < 10; i++) await wheel(page, -240);
    const zoomed = await zoomState(page);

    await page.keyboard.down("Shift");
    await wheel(page, 200);
    await page.keyboard.up("Shift");
    const panned = await zoomState(page);

    expect(panned.content).toBe(zoomed.content);
    expect(panned.scrollLeft).not.toBe(zoomed.scrollLeft);
  });

  test("opening another track returns to the fitted view", async ({ page }) => {
    await openStudio(page, { tauri: true });
    for (let i = 0; i < 10; i++) await wheel(page, -240);
    expect((await zoomState(page)).zoomVar).toBeGreaterThan(1);

    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    await page.waitForTimeout(1500);
    const reopened = await zoomState(page);
    expect(reopened.zoomVar).toBeCloseTo(1, 2);
    expect(reopened.loopDisabled).toBe(false);
  });
});
