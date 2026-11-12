// Detect lyrics: whether the next imports look for lyrics (LRCLIB, then a
// transcript of the vocals). Off by default, and unlike Detect structure a
// standing choice: it is kept until changed, across reloads and song opens.
// The button and the server setting move together, since the setting is what
// a new job captures.

import { test, expect } from "@playwright/test";
import { openStudio, seedLibrary, stubExportEndpoints, stubUpdateCheck, JOB_ID } from "./helpers.mjs";

const button = (page) => page.locator("#autoLyricsBtn");

const setting = (page) =>
  page.evaluate(() =>
    fetch("/api/settings", { cache: "no-store" }).then((r) => r.json()).then((d) => d.auto_lyrics));

const putSetting = (page, on) =>
  page.evaluate((value) =>
    fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auto_lyrics: value }),
    }).then((r) => r.json()), on);

test.describe("detect lyrics toggle", () => {
  test.afterEach(async ({ page }) => {
    await putSetting(page, false);
  });

  test("off by default, beside Detect structure, with no Experimental label", async ({ page }) => {
    await seedLibrary(page);
    await stubExportEndpoints(page);
    await stubUpdateCheck(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await putSetting(page, false);
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(button(page)).toHaveAttribute("aria-pressed", "false");
    await expect(page.locator(".detect-toggles #autoSectionsBtn")).toBeVisible();
    await expect(page.locator(".detect-toggles #autoLyricsBtn")).toBeVisible();
    await expect(page.locator(".structure-toggle-note")).toHaveCount(0);
  });

  test("switched on, it is kept across a reload and a song being opened", async ({ page }) => {
    await openStudio(page);
    await putSetting(page, false);
    await page.reload({ waitUntil: "domcontentloaded" });
    await button(page).click();
    await expect(button(page)).toHaveAttribute("aria-pressed", "true");
    await expect.poll(() => setting(page)).toBe(true);

    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(button(page)).toHaveAttribute("aria-pressed", "true");
    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    await expect(button(page)).toHaveAttribute("aria-pressed", "true");
    expect(await setting(page)).toBe(true);

    await button(page).click();
    await expect(button(page)).toHaveAttribute("aria-pressed", "false");
    await expect.poll(() => setting(page)).toBe(false);
  });
});
