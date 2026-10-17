// The footer's position readout (elapsed / total) against every way a person
// moves the playhead: a click on the time ruler, the [ and ] keys, and playing.
// The readout, the engine's clock and the ruler playhead must agree; the seek
// path used to move nothing on the default engine (#515).
//
// The footer's own seek bar (#footer-scrub) is not tested here: daw.css hides
// the whole waveform strip it lives in (rule 7, display: none !important), so
// no person can reach it. The fixture is 6 s, enough for fractions of a song.

import { test, expect } from "@playwright/test";
import { openStudio } from "./helpers.mjs";

test.use({ viewport: { width: 1600, height: 900 } });

const fmt = (s) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

async function duration(page) {
  return page.evaluate(async () => (await import("/js/state.js")).totalDuration);
}

async function engineTime(page) {
  return page.evaluate(async () => (await import("/js/transport.js")).transport().getCurrentTime());
}

const elapsed = (page) => page.locator("#footer-time-elapsed");

async function clickRulerAt(page, frac) {
  const ruler = page.locator("#ruler-time");
  const box = await ruler.boundingBox();
  // Pointer events with nothing in between: a press and release in place is a
  // seek, travel would make a loop.
  await page.mouse.move(box.x + box.width * frac, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.up();
}

test.beforeEach(async ({ page }) => {
  await openStudio(page);
});

test("the total is the song's length and nothing has elapsed yet", async ({ page }) => {
  const d = await duration(page);
  expect(d).toBeGreaterThan(5);
  await expect(page.locator("#footer-time-total")).toHaveText(fmt(d));
  await expect(elapsed(page)).toHaveText("00:00");
});

test("a click on the time ruler moves the readout, the engine and the playhead", async ({ page }) => {
  const d = await duration(page);
  await clickRulerAt(page, 0.5);

  await expect(elapsed(page)).toHaveText(fmt(d * 0.5));
  expect(await engineTime(page)).toBeCloseTo(d * 0.5, 1);
  const left = await page.locator("#ruler-time .playhead-marker")
    .evaluate((el) => parseFloat(el.style.left));
  expect(left).toBeCloseTo(50, 0);
});

test("] and [ jump five seconds and stop at either end", async ({ page }) => {
  const d = await duration(page);
  await clickRulerAt(page, 0.1);
  const start = await engineTime(page);

  await page.keyboard.press("BracketRight");
  await expect(elapsed(page)).toHaveText(fmt(Math.min(d, start + 5)));
  await page.keyboard.press("BracketRight");
  expect(await engineTime(page)).toBeCloseTo(d, 1);
  await expect(elapsed(page)).toHaveText(fmt(d));

  await page.keyboard.press("BracketLeft");
  await expect(elapsed(page)).toHaveText(fmt(d - 5));
  await page.keyboard.press("BracketLeft");
  await expect(elapsed(page)).toHaveText("00:00");
  expect(await engineTime(page)).toBe(0);
});

test("playing advances the readout and pausing holds it", async ({ page }) => {
  await page.locator("#t-play").click();
  await expect(elapsed(page)).toHaveText("00:01", { timeout: 5000 });
  await page.locator("#t-play").click();
  const held = await engineTime(page);
  const shown = await elapsed(page).textContent();
  await page.waitForTimeout(1200);
  expect(await engineTime(page)).toBeCloseTo(held, 1);
  await expect(elapsed(page)).toHaveText(shown);
});
