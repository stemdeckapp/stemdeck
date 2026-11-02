// An FFmpeg older than 5.1 cannot run any export: every mixdown ends in
// alimiter's latency option, which 5.1 added. /api/health reports the version
// as ffmpeg_too_old, and the page says so when it opens instead of at the
// first export. The real answer depends on this machine's ffmpeg, so each
// test fixes it.

import { test, expect } from "@playwright/test";

const MESSAGE =
  "StemDeck needs FFmpeg 5.1 or newer, and the FFmpeg it found is version 4.4.2-0ubuntu0.22.04.1. Exports will not work with it. Install FFmpeg 5.1 or newer, or set STEMDECK_FFMPEG to one, then restart StemDeck.";

async function stubFfmpegTooOld(page, value) {
  await page.route("**/api/health", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: { ...body, ffmpeg_too_old: value } });
  });
}

test("an FFmpeg older than 5.1 is named when the page opens", async ({ page }) => {
  await stubFfmpegTooOld(page, "4.4.2-0ubuntu0.22.04.1");
  await page.goto("/");
  const error = page.locator("#error");
  await expect(error).toBeVisible();
  await expect(error.locator(".error-msg")).toHaveText(MESSAGE);
  // Nothing to retry: only a newer FFmpeg helps.
  await expect(error.locator(".retry-btn")).toHaveCount(0);
});

test("a new enough FFmpeg shows nothing", async ({ page }) => {
  await stubFfmpegTooOld(page, null);
  const health = page.waitForResponse("**/api/health");
  await page.goto("/");
  await health;
  await page.waitForTimeout(500);
  await expect(page.locator("#error")).toBeHidden();
});
