// The slow speed is the player's to set (#701).
//
// Speed was a fixed 0.75x or 1x. Some fills want 0.75x and some only a nudge,
// so the slow button can be moved a hundredth at a time, by the wheel or the
// up and down arrow keys, between 0.50x and 0.99x, and it is remembered.

import { test, expect } from "@playwright/test";
import { openStudio } from "./helpers.mjs";

const slow = (page) => page.locator("#t-speed-075");
const rate = (page) => page.evaluate(async () => (await import("/js/state.js")).playbackSpeed);

test("scrolling over the slow button moves it a hundredth at a time and plays at it", async ({ page }) => {
  await openStudio(page);
  await expect(slow(page)).toHaveText("0.75x");
  await slow(page).hover();
  await page.mouse.wheel(0, -100);
  await page.mouse.wheel(0, -100);
  await expect(slow(page)).toHaveText("0.77x");
  await expect(slow(page)).toHaveAttribute("aria-checked", "true");
  await expect.poll(() => rate(page)).toBe(0.77);

  // 1x is still one press away, and the slow speed is kept for next time.
  await page.locator("#t-speed-1").click();
  await expect.poll(() => rate(page)).toBe(1);
  await slow(page).click();
  await expect.poll(() => rate(page)).toBe(0.77);
});

test("the arrow keys move it too, and it stops at 0.50x and 0.99x", async ({ page }) => {
  await openStudio(page);
  await slow(page).focus();
  for (let i = 0; i < 40; i++) await page.keyboard.press("ArrowDown");
  await expect(slow(page)).toHaveText("0.50x");
  for (let i = 0; i < 60; i++) await page.keyboard.press("ArrowUp");
  await expect(slow(page)).toHaveText("0.99x");
  await expect.poll(() => rate(page)).toBe(0.99);
});

test("the slow speed survives a reload", async ({ page }) => {
  await openStudio(page);
  await slow(page).hover();
  await page.mouse.wheel(0, 100);
  await expect(slow(page)).toHaveText("0.74x");
  await page.waitForTimeout(400);
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(slow(page)).toHaveText("0.74x");
  // A track opens at normal speed, as it always has.
  await expect(page.locator("#t-speed-1")).toHaveAttribute("aria-checked", "true");
});
