// Speed is a stepped fader and a box to type into.
//
// Both take any hundredth from 0.50x (#701). The fader stops at 1.00x and is
// notched every 0.05x, and the wheel over it jumps notch to notch. The box
// takes up to 1.50x, the range Karaoke Mode's presets reach, so it always
// reads the speed that is playing; above 1x the fader is pinned at its end.
// The parsing and the notch arithmetic are tested in Node
// (tests/js/speed-parse.test.mjs); this covers the wiring.

import { test, expect } from "@playwright/test";
import { JOB_ID, openStudio } from "./helpers.mjs";

const slider = (page) => page.locator("#t-speed-slider");
const box = (page) => page.locator("#t-speed-input");
const rate = (page) => page.evaluate(async () => (await import("/js/state.js")).playbackSpeed);
const savedSpeed = (page) => page.evaluate(
  async (id) => (await (await import("/js/utils.js")).storeGet(`stemdeck:mix:${id}:daw`, null))?.__global?.speed,
  JOB_ID,
);
const type = async (page, text) => {
  await box(page).fill(text);
  await box(page).press("Enter");
};

test("a track opens at normal speed", async ({ page }) => {
  await openStudio(page);
  await expect(box(page)).toHaveValue("1.00");
  await expect(slider(page)).toHaveValue("1");
  await expect(page.locator(".speed-value")).not.toHaveClass(/active/);
  await expect.poll(() => rate(page)).toBe(1);
});

test("the fader moves a hundredth at a time and the box follows", async ({ page }) => {
  await openStudio(page);
  await slider(page).focus();
  for (let i = 0; i < 17; i++) await page.keyboard.press("ArrowLeft");
  await expect(box(page)).toHaveValue("0.83");
  await expect(slider(page)).toHaveAttribute("aria-valuetext", "0.83x");
  await expect(page.locator(".speed-value")).toHaveClass(/active/);
  await expect.poll(() => rate(page)).toBe(0.83);

  // The wheel over it goes to the next notch, not 0.05x past where it was.
  await slider(page).hover();
  await page.mouse.wheel(0, -100);
  await expect(box(page)).toHaveValue("0.85");
  await page.mouse.wheel(0, 100);
  await page.mouse.wheel(0, 100);
  await expect(box(page)).toHaveValue("0.75");
  await expect.poll(() => rate(page)).toBe(0.75);

  // It stops at the ends.
  for (let i = 0; i < 30; i++) await page.keyboard.press("ArrowLeft");
  await expect(box(page)).toHaveValue("0.50");
  await expect.poll(() => rate(page)).toBe(0.5);

  // A double-click is back to normal.
  await slider(page).dblclick();
  await expect(box(page)).toHaveValue("1.00");
  await expect.poll(() => rate(page)).toBe(1);
});

test("typing a speed plays at exactly that speed", async ({ page }) => {
  await openStudio(page);
  await type(page, "0.83");
  await expect.poll(() => rate(page)).toBe(0.83);
  await expect(box(page)).toHaveValue("0.83");
  await expect(slider(page)).toHaveValue("0.83");

  // A comma, a trailing x, or a percentage all read the same way.
  for (const [typed, want] of [["0,6", 0.6], ["0.7x", 0.7], ["90%", 0.9], ["83", 0.83]]) {
    await type(page, typed);
    await expect.poll(() => rate(page)).toBe(want);
  }
});

test("out-of-range speeds are clamped and nonsense is ignored", async ({ page }) => {
  await openStudio(page);
  await type(page, "0.2");
  await expect(box(page)).toHaveValue("0.50");
  await expect.poll(() => rate(page)).toBe(0.5);

  await type(page, "1.8");
  await expect(box(page)).toHaveValue("1.50");
  await expect.poll(() => rate(page)).toBe(1.5);

  await type(page, "fast");
  await expect(box(page)).toHaveValue("1.50");
  await expect.poll(() => rate(page)).toBe(1.5);

  // Escape puts back what was showing.
  await box(page).fill("0.6");
  await box(page).press("Escape");
  await expect(box(page)).toHaveValue("1.50");
  await expect.poll(() => rate(page)).toBe(1.5);
});

test("the wheel and arrow keys on the box move it a hundredth at a time", async ({ page }) => {
  await openStudio(page);
  await box(page).hover();
  await page.mouse.wheel(0, 100);
  await page.mouse.wheel(0, 100);
  await expect(box(page)).toHaveValue("0.98");
  await expect.poll(() => rate(page)).toBe(0.98);

  await box(page).focus();
  await page.keyboard.press("ArrowDown");
  await expect(box(page)).toHaveValue("0.97");
  await page.keyboard.press("Shift+ArrowDown");
  await expect(box(page)).toHaveValue("0.92");
  await expect.poll(() => rate(page)).toBe(0.92);
});

test("a speed above 1x shows its real value, with the fader pinned at its end", async ({ page }) => {
  await openStudio(page);
  // Karaoke Mode's presets set the speed this way.
  await page.evaluate(async () => (await import("/js/transport.js")).setSpeed(1.25));
  await expect(box(page)).toHaveValue("1.25");
  await expect(slider(page)).toHaveValue("1");
  await expect(slider(page)).toHaveAttribute("aria-valuetext", "1.25x");
  await expect.poll(() => rate(page)).toBe(1.25);

  // The box nudges from the real value, not from where the fader sits.
  await box(page).focus();
  await page.keyboard.press("ArrowUp");
  await expect(box(page)).toHaveValue("1.26");
  await expect.poll(() => rate(page)).toBe(1.26);
  await box(page).press("Escape");

  // Up on the fader has nowhere to go; down comes back onto its notches.
  await slider(page).hover();
  await page.mouse.wheel(0, -100);
  await page.waitForTimeout(300);
  await expect(box(page)).toHaveValue("1.26");
  await page.mouse.wheel(0, 100);
  await expect(box(page)).toHaveValue("1.00");
  await expect.poll(() => rate(page)).toBe(1);
});

test("a speed set in the footer is kept in the open mode's record of the song", async ({ page }) => {
  await openStudio(page);
  await type(page, "0.83");
  await expect.poll(() => savedSpeed(page)).toBe(0.83);

  // A drag is recorded once it settles, like the rate itself.
  await slider(page).focus();
  await page.keyboard.press("ArrowLeft");
  await expect.poll(() => savedSpeed(page)).toBe(0.82);
});
