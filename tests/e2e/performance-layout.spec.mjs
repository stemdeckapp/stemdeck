// Performance's window layout at real window sizes (streamer/reviewModel.js
// reviewTier, streamer.css): the live window and the take review are both in
// view at every size above the hard minimum, side by side when two of 360 px
// fit across, stacked when not. At each size:
//
// Each size runs with the library open (the default, as in the maintainer's
// screenshot) and folded.
//
// 1. Both windows have a box, inside the viewport across, and the same size.
// 2. The live window never takes the height the strip and the Timeline /
//    Effects card need: stacked, the two windows stay within 65% of the
//    builder's height; side by side, the card keeps its least.
// 3. The card (Timeline and Effects) is at least 160 px high; the column
//    scrolls only past the hard minimum (reviewState().scroll, the narrow
//    band's last resort), where the live window keeps its 480 px and the
//    workspace may scroll sideways inside itself.
// 4. The windows, the strip and the card do not overlap.
// 5. The canvases are sized from their boxes, again after a resize.
//
// SHOTS_DIR, when set, saves a screenshot of each size there (for a person
// to look at); without it nothing is written.

import { test, expect } from "@playwright/test";
import path from "node:path";
import { openStudio, chooseMode } from "./helpers.mjs";

const sm = (page, sel) => page.locator(`#streamer ${sel}`);
const review = (page) => page.evaluate(async () => (await import("/js/streamer/streamer.js")).reviewState());

async function openPerformance(page) {
  await chooseMode(page, "streamer");
  await expect(page.locator("#streamer")).toBeVisible();
}

/** The boxes that matter, in viewport pixels, and whether the column scrolls. */
const layout = (page) => page.evaluate(() => {
  const q = (s) => document.querySelector(`#streamer ${s}`);
  const r = (s) => {
    const e = q(s);
    if (!e || !e.getClientRects().length) return null;
    const b = e.getBoundingClientRect();
    return { x: b.x, y: b.y, w: b.width, h: b.height };
  };
  const center = q(".sm-center");
  return {
    vw: window.innerWidth,
    vh: window.innerHeight,
    builder: r(".sm-builder"),
    live: r(".sm-preview"),
    review: r(".sm-review"),
    strip: r(".sm-strip"),
    card: r(".sm-tl"),
    scrolls: center.scrollHeight > center.clientHeight + 1,
    pageScrollsX: document.documentElement.scrollWidth > window.innerWidth,
  };
});

const overlaps = (a, b) => a.x < b.x + b.w - 0.5 && b.x < a.x + a.w - 0.5 && a.y < b.y + b.h - 0.5 && b.y < a.y + a.h - 0.5;

/** The canvases' backing size against their boxes. */
const canvasesFit = (page) => page.evaluate(() => {
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const fit = (boxSel, canvasSel) => {
    const box = document.querySelector(`#streamer ${boxSel}`);
    const c = document.querySelector(`#streamer ${canvasSel}`);
    return c.width === Math.round(box.clientWidth * dpr) && c.height === Math.round(box.clientHeight * dpr);
  };
  return { live: fit(".sm-preview", ".sm-canvas"), review: fit(".sm-review", ".sm-review-canvas") };
});

const SIZES = [[1920, 1080], [1240, 1010], [1024, 768], [800, 900], [600, 700], [1400, 600]];

test.describe("Performance window layout", () => {
  for (const [w, h] of SIZES) for (const lib of ["library open", "library folded"]) {
    test(`${w} x ${h}, ${lib}: the live window and the take review are both in view`, async ({ page }) => {
      await page.setViewportSize({ width: w, height: h });
      await openStudio(page);
      if (lib === "library folded") await page.locator("#sidebarCollapseBtn").click();
      await openPerformance(page);
      await expect.poll(async () => (await review(page)).tier).toMatch(/^[abs]$/);
      await page.waitForTimeout(300);
      const L = await layout(page);
      const { tier, scroll } = await review(page);
      if (process.env.SHOTS_DIR) await page.screenshot({ path: path.join(process.env.SHOTS_DIR, `perf-${w}x${h}-${lib.split(" ")[1]}-${tier}${scroll ? "-scroll" : ""}.png`) });
      // Only past the hard minimum does the column scroll.
      expect(L.scrolls).toBe(Boolean(scroll));

      // 1. Both windows, in view across, the same size.
      for (const k of ["live", "review"]) {
        expect(L[k], k).not.toBeNull();
        expect(L[k].w, k).toBeGreaterThan(200);
        expect(L[k].h, k).toBeGreaterThan(100);
        expect(L[k].x, k).toBeGreaterThanOrEqual(-0.5);
        if (!scroll) expect(L[k].x + L[k].w, k).toBeLessThanOrEqual(L.vw + 0.5);
        expect(L[k].y, k).toBeGreaterThanOrEqual(-0.5);
        if (!L.scrolls) expect(L[k].y + L[k].h, k).toBeLessThanOrEqual(L.vh + 0.5);
      }
      expect(Math.abs(L.live.w - L.review.w)).toBeLessThanOrEqual(1);
      expect(Math.abs(L.live.h - L.review.h)).toBeLessThanOrEqual(1);
      expect(L.pageScrollsX).toBe(false);
      // Stacked or side by side, as the tier says.
      if (tier === "s") expect(L.review.y).toBeGreaterThanOrEqual(L.live.y + L.live.h - 0.5);
      else expect(Math.abs(L.review.y - L.live.y)).toBeLessThanOrEqual(1);

      // 2. The live window's cap.
      if (!scroll) {
        if (tier === "s") expect(L.review.y + L.review.h - L.live.y).toBeLessThanOrEqual(0.65 * L.builder.h + 1);
        expect(L.live.h).toBeLessThanOrEqual(L.builder.h - L.strip.h - 160);
      }

      // 3. The card keeps a useful height (where the column scrolls, it is
      // the card's own height in the scrolled column).
      expect(L.card.h).toBeGreaterThanOrEqual(160);

      // 4. No overlap, top to bottom: windows, strip, card.
      const parts = [["live", L.live], ["review", L.review], ["strip", L.strip], ["card", L.card]];
      for (let i = 0; i < parts.length; i++) {
        for (let j = i + 1; j < parts.length; j++) expect(overlaps(parts[i][1], parts[j][1]), `${parts[i][0]} / ${parts[j][0]}`).toBe(false);
      }
      expect(L.strip.y).toBeGreaterThanOrEqual(Math.max(L.live.y + L.live.h, L.review.y + L.review.h) - 0.5);
      expect(L.card.y).toBeGreaterThanOrEqual(L.strip.y + L.strip.h - 0.5);

      // 5. The canvases follow their boxes.
      await expect.poll(() => canvasesFit(page)).toEqual({ live: true, review: true });
    });
  }

  test("a resize from wide to narrow and back re-measures both windows", async ({ page }) => {
    await page.setViewportSize({ width: 1920, height: 1080 });
    await openStudio(page);
    await openPerformance(page);
    await expect.poll(async () => (await review(page)).tier).toMatch(/^[ab]$/);
    await page.setViewportSize({ width: 800, height: 900 });
    await expect.poll(async () => (await review(page)).tier).not.toMatch(/^[ab]$/);
    await expect(sm(page, ".sm-review")).toBeVisible();
    await expect.poll(() => canvasesFit(page)).toEqual({ live: true, review: true });
    const narrow = await layout(page);
    await page.setViewportSize({ width: 1920, height: 1080 });
    await expect.poll(async () => (await review(page)).tier).toMatch(/^[ab]$/);
    await expect.poll(() => canvasesFit(page)).toEqual({ live: true, review: true });
    const wide = await layout(page);
    expect(Math.abs(wide.review.y - wide.live.y)).toBeLessThanOrEqual(1);
    expect(wide.live.w).not.toBe(narrow.live.w);
  });
});
