// A song that plays on the streaming path drops the previous song's engine.
//
// The streaming path (stemdeck.audioEngine = "0", or a song too large for the
// full-decode engine) plays on the multitrack's <audio> elements. Opening such
// a song after one played by an engine left that engine in place: the
// transport reads `audioEngine ?? multitrack`, so Play, Stop and seek drove
// the previous song, and its buffers were never freed.
import { test, expect } from "@playwright/test";
import { SIBLING_JOB_ID, openStudio } from "./helpers.mjs";

test.use({ launchOptions: { args: ["--mute-audio", "--autoplay-policy=no-user-gesture-required"] } });

const state = (page, fn) => page.evaluate(fn);

test("Play on a streaming-path song plays that song, not the previous song's engine", async ({ page }) => {
  await openStudio(page);
  await page.waitForFunction(async () => {
    const st = await import("/js/state.js");
    if (!st.audioEngine || (await st.audioEngine.ready) !== true) return false;
    window.__previousEngine = st.audioEngine;
    return true;
  }, null, { timeout: 20000 });

  await state(page, () => localStorage.setItem("stemdeck.audioEngine", "0"));
  await page.locator(`.cat-item[data-id="${SIBLING_JOB_ID}"]`).first().click();
  await expect.poll(() => state(page, async () => (await import("/js/state.js")).currentJobId)).toBe(SIBLING_JOB_ID);
  await expect(page.locator(".app")).not.toHaveClass(/engine-waveforms/);
  await page.waitForFunction(
    () => !/\/\s*00:00\s*$/.test(document.querySelector("#t-time")?.textContent || "00:00 / 00:00"),
    null, { timeout: 20000 },
  );

  expect.soft(await state(page, async () => (await import("/js/state.js")).audioEngine === null)).toBe(true);
  await expect.soft.poll(() => state(page, () => window.__previousEngine.audioContext.state)).toBe("closed");

  await page.locator("#t-play").click();
  await expect.poll(() => state(page, async () => (await import("/js/state.js")).multitrack.isPlaying())).toBe(true);
  expect(await state(page, () => window.__previousEngine.isPlaying())).toBe(false);
});
