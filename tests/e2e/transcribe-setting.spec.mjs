// The lyrics transcription setting in Settings > Song details.
//
// Driven in a real browser because the control is the risk: the select has to
// show what the server holds (Auto by default), and a change has to reach the
// server, since the pipeline reads the setting per job and nothing else would
// ever say the choice was lost.

import { test, expect } from "@playwright/test";
import { seedLibrary } from "./helpers.mjs";

async function openSettings(page) {
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  await page.locator('.settings-tab[data-tab="details"]').click();
  await expect(page.locator(".set-transcribe-lyrics")).toBeVisible();
}

const serverValue = async (page) =>
  (await (await page.request.get("/api/settings")).json()).transcribe_lyrics;

test.describe("transcribe lyrics", () => {
  test.beforeEach(async ({ page }) => {
    await page.request.post("/api/settings", { data: { transcribe_lyrics: "auto" } });
  });

  // The backend is shared across the suite; leave it as every other spec expects.
  test.afterEach(async ({ page }) => {
    await page.request.post("/api/settings", { data: { transcribe_lyrics: "auto" } });
  });

  test("shows the server's choice and offers all three", async ({ page }) => {
    await openSettings(page);
    const select = page.locator(".set-transcribe-lyrics");
    await expect(select).toHaveValue("auto");
    await expect(select.locator("option")).toHaveText(["Auto (NVIDIA GPU only)", "On", "Off"]);
  });

  test("a change reaches the server and survives a reopen", async ({ page }) => {
    await openSettings(page);
    await page.locator(".set-transcribe-lyrics").selectOption("off");
    await expect.poll(() => serverValue(page)).toBe("off");

    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator("#settingsBtn").click();
    await page.locator('.settings-tab[data-tab="details"]').click();
    await expect(page.locator(".set-transcribe-lyrics")).toHaveValue("off");
  });
});
