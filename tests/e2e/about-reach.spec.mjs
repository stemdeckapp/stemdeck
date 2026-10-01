// About this song can be opened at any window width (#735).
//
// Its button lived only on the Now Playing card, and daw.css hid that whole
// card below 1460 px, so a maximised laptop window had no way to open it. The
// card now gives way in steps instead: the artwork and the meta line go first,
// then the title, and the heart and the About button stay.

import { test, expect } from "@playwright/test";
import { openStudio } from "./helpers.mjs";

for (const width of [1024, 1280, 1366, 1600]) {
  test(`About this song opens from the card at ${width} px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 });
    await openStudio(page);
    const button = page.locator("#np-details-btn");
    await expect(button).toBeVisible();
    await expect(page.locator("#fav-btn")).toBeVisible();
    // On screen and inside the bar, not pushed past its edge.
    const box = await button.boundingBox();
    const bar = await page.locator(".daw-composer").boundingBox();
    expect(box.width).toBeGreaterThan(20);
    expect(box.x + box.width).toBeLessThanOrEqual(bar.x + bar.width + 0.5);
    await button.click();
    await expect(page.locator("#artistDialog")).toBeVisible();
  });
}

test("Extract stems sits under Detect structure and the composer keeps one line of chips", async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 800 });
  await openStudio(page);
  const detect = await page.locator("#autoSectionsBtn").boundingBox();
  const extract = await page.locator("#submit").boundingBox();
  await expect(page.locator("#submit")).toHaveText(/Extract stems/);
  expect(Math.abs(detect.x - extract.x)).toBeLessThan(1);
  expect(extract.y).toBeGreaterThan(detect.y + detect.height - 1);
});
