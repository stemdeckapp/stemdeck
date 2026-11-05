// The Mode menu opens on hover (static/js/modeUi.js, rules in hoverMenu.js):
// pointing at the Mode button shows the modes, the gap to the menu never
// closes it, choosing one works, and leaving closes it. A click and the
// keyboard still open it, and the Guide keeps hover for explaining.

import { test, expect } from "@playwright/test";
import { openStudio } from "./helpers.mjs";

const menu = (page) => page.locator("#modeMenu");
const center = async (loc) => {
  const b = await loc.boundingBox();
  return [b.x + b.width / 2, b.y + b.height / 2];
};
// Far from the rail and the menu, on the studio.
const away = (page) => page.mouse.move(900, 500);

test.describe("Mode menu on hover", () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem("stemdeck.mode", "practice"));
    await openStudio(page);
    await away(page);
  });

  test("hover opens it, the menu stays open under the pointer, a choice works, leaving closes it", async ({ page }) => {
    const btn = page.locator("#modeBtn");
    await expect(menu(page)).toBeHidden();
    await page.mouse.move(...(await center(btn)));
    await expect(menu(page)).toBeVisible();
    await expect(btn).toHaveAttribute("aria-expanded", "true");
    // Hover does not take focus.
    expect(await page.evaluate(() => !!document.activeElement?.closest("#modeMenu"))).toBe(false);

    // Across the gap, in small steps, onto an option; then wait past the delay.
    const daw = page.locator('#modeMenu .mode-item[data-mode="daw"]');
    await page.mouse.move(...(await center(daw)), { steps: 15 });
    await page.waitForTimeout(400);
    await expect(menu(page)).toBeVisible();

    await page.mouse.down();
    await page.mouse.up();
    await expect(page.locator("html")).toHaveAttribute("data-mode", "daw");
    await expect(menu(page)).toBeHidden();
    await expect(btn).toHaveAttribute("aria-expanded", "false");

    // Open again by hover, then leave both: it closes.
    await away(page);
    await page.mouse.move(...(await center(btn)));
    await expect(menu(page)).toBeVisible();
    await away(page);
    await expect(menu(page)).toBeHidden();
    await expect(btn).toHaveAttribute("aria-expanded", "false");
  });

  test("a click keeps a hover-opened menu open, and a second click closes it", async ({ page }) => {
    const btn = page.locator("#modeBtn");
    await btn.click();
    await expect(menu(page)).toBeVisible();
    // Pinned by the click: leaving does not close it.
    await away(page);
    await page.waitForTimeout(400);
    await expect(menu(page)).toBeVisible();
    await btn.click();
    await expect(menu(page)).toBeHidden();
    await expect(btn).toHaveAttribute("aria-expanded", "false");
  });

  test("the keyboard still opens it, and Escape closes a hover-opened menu", async ({ page }) => {
    const btn = page.locator("#modeBtn");
    await btn.focus();
    await page.keyboard.press("Enter");
    await expect(menu(page)).toBeVisible();
    await expect(page.locator('#modeMenu .mode-item[data-mode="practice"]')).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(menu(page)).toBeHidden();
    await expect(btn).toBeFocused();

    await page.mouse.move(...(await center(btn)));
    await expect(menu(page)).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(menu(page)).toBeHidden();
  });

  test("a click outside closes a hover-opened menu", async ({ page }) => {
    await page.mouse.move(...(await center(page.locator("#modeBtn"))));
    await expect(menu(page)).toBeVisible();
    await page.mouse.move(900, 500, { steps: 5 });
    await page.mouse.down();
    await page.mouse.up();
    await expect(menu(page)).toBeHidden();
  });

  test("while the Guide is on, pointing at the button explains it and does not open the menu", async ({ page }) => {
    await page.locator("#helpModeBtn").click();
    await expect(page.locator("body")).toHaveClass(/help-mode/);
    await page.mouse.move(...(await center(page.locator("#modeBtn"))), { steps: 5 });
    await expect(page.locator("#helpTip")).toBeVisible();
    await page.waitForTimeout(400);
    await expect(menu(page)).toBeHidden();
    // A click still opens it in the Guide.
    await page.locator("#modeBtn").click();
    await expect(menu(page)).toBeVisible();
  });
});
