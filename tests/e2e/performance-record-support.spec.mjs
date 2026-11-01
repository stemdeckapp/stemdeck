// Performance video recording on an engine that cannot record it says why
// (#787). Stock Ubuntu's WebKitGTK has no MediaRecorder, or one with no
// encoder until the GStreamer plugins are installed; recording then failed
// with no word at all. The decision is video/recordSupport.js
// (tests/js/record-support.test.mjs); these check that the view shows it.

import { test, expect } from "@playwright/test";
import { openStudio, chooseMode } from "./helpers.mjs";
import { removeTracks } from "./recording-helpers.mjs";

test.afterEach(async ({ request }) => removeTracks(request));

const LINUX_UA = "Mozilla/5.0 (X11; Ubuntu; Linux x86_64) AppleWebKit/605.1.15 (KHTML, like Gecko)";
const WINDOWS_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36";

async function pressRecord(page) {
  await chooseMode(page, "streamer");
  await expect(page.locator("#streamer")).toBeVisible();
  await page.locator("#streamer .sm-rec").click();
}

test.describe("Performance recording without a video recorder", () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1800, height: 1020 });
  });

  test.describe("on Linux with no MediaRecorder", () => {
    test.use({ userAgent: LINUX_UA });
    test("names the GStreamer packages and starts no take", async ({ page }) => {
      await page.addInitScript(() => { delete window.MediaRecorder; });
      await openStudio(page);
      await pressRecord(page);
      const note = page.locator("#streamerNote");
      await expect(note).toBeVisible();
      await expect(note).toContainText("gstreamer1.0-plugins-good");
      await expect(note).toContainText("gstreamer1.0-libav");
      await expect(page.locator("#streamer .sm-rec")).not.toHaveClass(/recording/);
    });
  });

  test.describe("on Windows with a MediaRecorder that has no encoder", () => {
    test.use({ userAgent: WINDOWS_UA });
    test("says there is no video encoder and starts no take", async ({ page }) => {
      await page.addInitScript(() => {
        if (window.MediaRecorder) window.MediaRecorder.isTypeSupported = () => false;
      });
      await openStudio(page);
      await pressRecord(page);
      const note = page.locator("#streamerNote");
      await expect(note).toBeVisible();
      await expect(note).toContainText("no video encoder");
      await expect(page.locator("#streamer .sm-rec")).not.toHaveClass(/recording/);
    });
  });
});
