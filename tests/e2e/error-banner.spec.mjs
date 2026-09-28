// The error box: the studio's one alert surface (#error, showError in
// job.js). It used to be a faint red strip across the top bar that could be
// neither read nor closed when a failure gave it no "Try again".

import { test, expect } from "@playwright/test";
import { openStudio } from "./helpers.mjs";

async function failAnExport(page) {
  await page.locator("#t-export-btn").click();
  await page.locator("#t-export-mix").click();
  await page.evaluate(() => window.__e2e.choosePath());
  await page.evaluate(() => window.__e2e.failSave("disk full"));
  await expect(page.locator("#error")).toBeVisible();
}

test.describe("the error box", () => {
  test("is a readable card that its close button dismisses", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await failAnExport(page);
    const box = page.locator("#error");
    await expect(box).toContainText("disk full");
    const look = await box.evaluate((el) => {
      const s = getComputedStyle(el);
      return { position: s.position, background: s.backgroundColor, color: s.color };
    });
    expect(look.position).toBe("fixed");
    // Opaque, so the page behind never shows through the words.
    expect(look.background).toMatch(/^rgb\(/);
    expect(look.color).toBe("rgb(232, 236, 240)");
    await box.getByRole("button", { name: "Dismiss" }).click();
    await expect(box).toBeHidden();
  });

  test("Escape closes it", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await failAnExport(page);
    await page.keyboard.press("Escape");
    await expect(page.locator("#error")).toBeHidden();
  });

  test("Escape closes it alone, leaving a popup beneath it open", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await page.locator("#settingsBtn").click();
    await page.locator('.settings-tab[data-tab="details"]').click();
    const settings = page.locator(".set-acoustid-key");
    await expect(settings).toBeVisible();
    await page.evaluate(async () => (await import("/js/job.js")).showError("Something failed.", null, { retry: false }));
    await expect(page.locator("#error")).toBeVisible();

    await page.keyboard.press("Escape");
    await expect(page.locator("#error")).toBeHidden();
    await expect(settings).toBeVisible();

    await page.keyboard.press("Escape");
    await expect(settings).toBeHidden();
  });
});
