// Performance's window layout at real window sizes (streamer/reviewModel.js
// reviewTier and foldsLibrary, streamer/libraryFold.js, streamer.css): the
// live window and the take review are both in view at every size above the
// hard minimum, side by side down to two of 288 px, stacked only below that.
//
// Each size runs with the library open (the default, as in the maintainer's
// screenshot) and folded. With the library open in a window too narrow for
// two windows of 360 px beside it (under about 1140 px), Performance folds it
// while open, without saving that, and opens it again on leaving.
//
// At each size:
// 1. Both windows have a box, inside the viewport, and the same size.
// 2. The live window never takes the height the strip and the Timeline /
//    Effects card need: stacked, the two windows stay within 65% of the
//    builder's height; side by side, the card keeps its least.
// 3. The card (Timeline and Effects) is at least 160 px high. At the sizes
//    that have room (`fits`) nothing scrolls: the windows and the whole card
//    are in the viewport. Elsewhere the column scrolls only past the hard
//    minimum (reviewState().scroll), and only down.
// 4. The windows, the strip and the card do not overlap.
// 5. The canvases are sized from their boxes, again after a resize.
// 6. Nothing scrolls sideways: not the page, not the centre column.
// 7. Leaving Performance gives the library back as it was, and the saved
//    choice was never changed.
//
// SHOTS_DIR, when set, saves a screenshot of each size there (for a person
// to look at); without it nothing is written.

import { test, expect } from "@playwright/test";
import path from "node:path";
import { openStudio, chooseMode, leaveModeView } from "./helpers.mjs";

const sm = (page, sel) => page.locator(`#streamer ${sel}`);
const review = (page) => page.evaluate(async () => (await import("/js/streamer/streamer.js")).reviewState());

async function openPerformance(page) {
  await chooseMode(page, "streamer");
  await expect(page.locator("#streamer")).toBeVisible();
}

/** The library sidebar: folded or not, its width, and the saved choice. */
const library = (page) => page.evaluate(() => {
  let saved = null;
  try { saved = localStorage.getItem("stemdeck.catalog.collapsed"); } catch { saved = "unreadable"; }
  return {
    collapsed: document.querySelector(".app").classList.contains("cat-collapsed"),
    expanded: document.getElementById("sidebarCollapseBtn").getAttribute("aria-expanded"),
    width: Math.round(document.getElementById("catalogPanel").getBoundingClientRect().width),
    saved,
  };
});

/** The sidebar done sliding (66 folded, 390 open) and the windows placed. */
async function settled(page) {
  await expect.poll(async () => {
    const l = await library(page);
    return l.width === (l.collapsed ? 66 : 390);
  }).toBe(true);
  await expect.poll(async () => (await review(page)).tier).toMatch(/^[abs]$/);
  await page.waitForTimeout(300);
}

/** The boxes that matter, in viewport pixels, and what scrolls. */
const layout = (page) => page.evaluate(() => {
  const q = (s) => document.querySelector(`#streamer ${s}`);
  const r = (s) => {
    const e = q(s);
    if (!e || !e.getClientRects().length) return null;
    const b = e.getBoundingClientRect();
    return { x: b.x, y: b.y, w: b.width, h: b.height };
  };
  const center = q(".sm-center");
  const doc = document.scrollingElement;
  return {
    vw: window.innerWidth,
    vh: window.innerHeight,
    builder: r(".sm-builder"),
    live: r(".sm-preview"),
    review: r(".sm-review"),
    strip: r(".sm-strip"),
    card: r(".sm-tl"),
    scrolls: center.scrollHeight > center.clientHeight + 1,
    centerScrollsX: center.scrollWidth > center.clientWidth + 1,
    pageScrollsX: doc.scrollWidth > doc.clientWidth,
    pageScrollsY: doc.scrollHeight > doc.clientHeight,
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

// [width, height, fits (nothing scrolls, all in view), folds (an open library folds)]
const SIZES = [
  [1920, 1080, true, false],
  [1240, 1010, true, false],
  [1366, 768, true, false],
  [1280, 720, true, false],
  [1024, 768, true, true],
  [800, 900, true, true],
  [600, 700, false, true],
  [1400, 600, false, false],
];

test.describe("Performance window layout", () => {
  for (const [w, h, fits, folds] of SIZES) for (const lib of ["library open", "library folded"]) {
    test(`${w} x ${h}, ${lib}: the live window, the take review and the timeline are in view`, async ({ page }) => {
      await page.setViewportSize({ width: w, height: h });
      await openStudio(page);
      if (lib === "library folded") await page.locator("#sidebarCollapseBtn").click();
      const before = await library(page);
      expect(before.collapsed).toBe(lib === "library folded");
      await openPerformance(page);
      await settled(page);
      // 7. Folded for Performance only where two of 360 do not fit beside
      // it, and the saved choice untouched.
      const during = await library(page);
      expect(during.collapsed).toBe(lib === "library folded" || folds);
      expect(during.expanded).toBe(String(!during.collapsed));
      expect(during.saved).toBe(before.saved);
      const L = await layout(page);
      const { tier, scroll } = await review(page);
      if (process.env.SHOTS_DIR) await page.screenshot({ path: path.join(process.env.SHOTS_DIR, `perf-${w}x${h}-${lib.split(" ")[1]}-${tier}${scroll ? "-scroll" : ""}.png`) });
      // Only past the hard minimum does the column scroll.
      expect(L.scrolls).toBe(Boolean(scroll));
      if (fits) {
        expect(scroll).toBe(false);
        expect(L.pageScrollsY).toBe(false);
      }

      // 1. Both windows, in view, the same size.
      for (const k of ["live", "review"]) {
        expect(L[k], k).not.toBeNull();
        expect(L[k].w, k).toBeGreaterThan(200);
        expect(L[k].h, k).toBeGreaterThan(100);
        expect(L[k].x, k).toBeGreaterThanOrEqual(-0.5);
        expect(L[k].x + L[k].w, k).toBeLessThanOrEqual(L.vw + 0.5);
        expect(L[k].y, k).toBeGreaterThanOrEqual(-0.5);
        if (!L.scrolls) expect(L[k].y + L[k].h, k).toBeLessThanOrEqual(L.vh + 0.5);
      }
      expect(Math.abs(L.live.w - L.review.w)).toBeLessThanOrEqual(1);
      expect(Math.abs(L.live.h - L.review.h)).toBeLessThanOrEqual(1);
      // Stacked or side by side, as the tier says; side by side wherever
      // the window has room for everything.
      if (tier === "s") expect(L.review.y).toBeGreaterThanOrEqual(L.live.y + L.live.h - 0.5);
      else expect(Math.abs(L.review.y - L.live.y)).toBeLessThanOrEqual(1);
      if (fits) expect(tier).toMatch(/^[ab]$/);

      // 2. The live window's cap.
      if (!scroll) {
        if (tier === "s") expect(L.review.y + L.review.h - L.live.y).toBeLessThanOrEqual(0.65 * L.builder.h + 1);
        expect(L.live.h).toBeLessThanOrEqual(L.builder.h - L.strip.h - 160);
      }

      // 3. The card keeps a useful height (where the column scrolls, it is
      // the card's own height in the scrolled column), and where the window
      // has room it is whole in the viewport.
      expect(L.card.h).toBeGreaterThanOrEqual(160);
      if (fits) {
        expect(L.card.x).toBeGreaterThanOrEqual(-0.5);
        expect(L.card.x + L.card.w).toBeLessThanOrEqual(L.vw + 0.5);
        expect(L.card.y + L.card.h).toBeLessThanOrEqual(L.vh + 0.5);
      }

      // 4. No overlap, top to bottom: windows, strip, card.
      const parts = [["live", L.live], ["review", L.review], ["strip", L.strip], ["card", L.card]];
      for (let i = 0; i < parts.length; i++) {
        for (let j = i + 1; j < parts.length; j++) expect(overlaps(parts[i][1], parts[j][1]), `${parts[i][0]} / ${parts[j][0]}`).toBe(false);
      }
      expect(L.strip.y).toBeGreaterThanOrEqual(Math.max(L.live.y + L.live.h, L.review.y + L.review.h) - 0.5);
      expect(L.card.y).toBeGreaterThanOrEqual(L.strip.y + L.strip.h - 0.5);

      // 5. The canvases follow their boxes.
      await expect.poll(() => canvasesFit(page)).toEqual({ live: true, review: true });

      // 6. Nothing scrolls sideways.
      expect(L.pageScrollsX).toBe(false);
      expect(L.centerScrollsX).toBe(false);

      // 7. Leaving gives the library back as it was.
      await leaveModeView(page);
      await expect(page.locator("#streamer")).toBeHidden();
      await expect.poll(async () => (await library(page)).collapsed).toBe(before.collapsed);
      const after = await library(page);
      expect(after.expanded).toBe(String(!before.collapsed));
      expect(after.saved).toBe(before.saved);
    });
  }

  test("a library opened by hand in Performance stays open until Performance opens again", async ({ page }) => {
    await page.setViewportSize({ width: 1024, height: 768 });
    await openStudio(page);
    // The studio itself leaves the library alone at this size.
    expect((await library(page)).collapsed).toBe(false);
    await openPerformance(page);
    await expect.poll(async () => (await library(page)).collapsed).toBe(true);
    // Opened again by hand: Performance does not fold it back, not even on
    // a resize, and leaving keeps it open.
    await page.locator("#sidebarCollapseBtn").click();
    await settled(page);
    expect((await library(page)).collapsed).toBe(false);
    await page.setViewportSize({ width: 1000, height: 760 });
    await settled(page);
    expect((await library(page)).collapsed).toBe(false);
    expect((await layout(page)).pageScrollsX).toBe(false);
    await leaveModeView(page);
    await expect(page.locator("#streamer")).toBeHidden();
    expect((await library(page)).collapsed).toBe(false);
    // The next time Performance opens, it folds again.
    await openPerformance(page);
    await expect.poll(async () => (await library(page)).collapsed).toBe(true);
    await leaveModeView(page);
    await expect.poll(async () => (await library(page)).collapsed).toBe(false);
  });

  test("folded by hand in Performance, the library stays folded after it", async ({ page }) => {
    await page.setViewportSize({ width: 1366, height: 768 });
    await openStudio(page);
    await openPerformance(page);
    await settled(page);
    expect((await library(page)).collapsed).toBe(false);
    await page.locator("#sidebarCollapseBtn").click();
    await leaveModeView(page);
    await expect(page.locator("#streamer")).toBeHidden();
    const l = await library(page);
    expect(l.collapsed).toBe(true);
    expect(l.saved).toBe("1");
  });

  test("a window made wide enough in Performance gives the library back, and narrow folds it again", async ({ page }) => {
    await page.setViewportSize({ width: 1024, height: 768 });
    await openStudio(page);
    await openPerformance(page);
    await expect.poll(async () => (await library(page)).collapsed).toBe(true);
    await page.setViewportSize({ width: 1600, height: 900 });
    await expect.poll(async () => (await library(page)).collapsed).toBe(false);
    await settled(page);
    expect((await review(page)).tier).toMatch(/^[ab]$/);
    await page.setViewportSize({ width: 1024, height: 768 });
    await expect.poll(async () => (await library(page)).collapsed).toBe(true);
    await settled(page);
    expect((await review(page)).tier).toMatch(/^[ab]$/);
    await leaveModeView(page);
    await expect.poll(async () => (await library(page)).collapsed).toBe(false);
  });

  test("a resize from wide to narrow and back re-measures both windows", async ({ page }) => {
    await page.setViewportSize({ width: 1920, height: 1080 });
    await openStudio(page);
    await openPerformance(page);
    await expect.poll(async () => (await review(page)).tier).toMatch(/^[ab]$/);
    await page.setViewportSize({ width: 800, height: 900 });
    await settled(page);
    await expect(sm(page, ".sm-review")).toBeVisible();
    await expect.poll(() => canvasesFit(page)).toEqual({ live: true, review: true });
    const narrow = await layout(page);
    await page.setViewportSize({ width: 1920, height: 1080 });
    await settled(page);
    await expect.poll(async () => (await review(page)).tier).toMatch(/^[ab]$/);
    await expect.poll(() => canvasesFit(page)).toEqual({ live: true, review: true });
    const wide = await layout(page);
    expect(Math.abs(wide.review.y - wide.live.y)).toBeLessThanOrEqual(1);
    expect(wide.live.w).not.toBe(narrow.live.w);
  });
});
