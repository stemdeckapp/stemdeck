// A count-in plays on every press of play, not only the first.
//
// It used to play once per track. The count-in is placed against the engine's
// clock for the start it precedes, and it was placed as soon as play() had
// returned. On the streaming engine, which is the default, that clock is only
// set once the chunk to start from is decoded, and chunks behind the playhead
// are evicted. So the first play started from a cached chunk and counted in,
// and every play after a stop, a pause or the end of the track took the fetch
// path, placed its clicks against the previous start's clock, and put every
// one of them in the past, where Web Audio drops them without a sound (#655).
//
// It plays with the click track off as well as on. The #655 reporter read the
// silence as the count-in being tied to the click; it was this.
//
// What is measured is where each click lands against the audio clock at the
// moment it is handed over: a click scheduled in the past never sounds.
import { test, expect } from "@playwright/test";
import { openStudio, waitForClickTrack } from "./helpers.mjs";

async function setUp(page, { click }) {
  await page.setViewportSize({ width: 1900, height: 1000 });
  await openStudio(page, { tauri: true });
  await waitForClickTrack(page);
  await page.locator("#t-metro-countin").selectOption("1");
  if (click) await page.locator("#t-metro").click();
  // Record every count-in the metronome is handed: how many clicks, and how
  // many of them are still ahead of the audio clock.
  await page.evaluate(async () => {
    const st = await import("/js/state.js");
    window.__countIns = [];
    const m = st.metronome;
    const handOver = m.playCountIn.bind(m);
    m.playCountIn = (clicks) => {
      const eng = st.audioEngine;
      const now = eng.audioContext.currentTime;
      window.__countIns.push({
        clicks: clicks.length,
        ahead: clicks.filter((c) => eng.sourceTimeToCtxTime(c.time) > now).length,
      });
      return handOver(clicks);
    };
  });
}

const countIns = (page) => page.evaluate(() => window.__countIns);
const play = (page) => page.keyboard.press("Space");

// Each case plays, does something that ends the first play, and plays again.
const endings = {
  "a pause": async (page) => {
    await play(page);
    await page.waitForTimeout(400);
  },
  "Stop": async (page) => {
    await page.locator("#t-stop").click();
    await page.waitForTimeout(400);
  },
  "the end of the track": async (page) => {
    // The fixture is 6 seconds, after a 2 second lead-in.
    await page.waitForTimeout(6000);
  },
};

for (const click of [true, false]) {
  for (const [after, end] of Object.entries(endings)) {
    test(`it counts in again after ${after}, with the click ${click ? "on" : "off"}`, async ({ page }) => {
      await setUp(page, { click });

      await play(page);
      await page.waitForTimeout(3500);
      await end(page);
      await play(page);
      await expect.poll(async () => (await countIns(page)).length).toBe(2);

      const [first, second] = await countIns(page);
      expect(first.clicks).toBeGreaterThan(0);
      expect(first.ahead).toBe(first.clicks);
      // The bug: the second was handed over with every click already past.
      expect(second.ahead).toBe(second.clicks);
    });
  }
}

test("a pause before the start drops a count-in that is still waiting", async ({ page }) => {
  // The count-in waits for the engine to schedule its start, which after a
  // Stop means waiting for a chunk to download. A pause that gets there first
  // must not leave the count-in to sound afterwards into a stopped track.
  await setUp(page, { click: false });
  await play(page);
  await page.waitForTimeout(3500);
  await page.locator("#t-stop").click();

  // Hold the next chunk download open, as a slow connection would.
  let release;
  const held = new Promise((r) => { release = r; });
  await page.route("**/stems/*.wav", async (route) => {
    await held;
    await route.continue();
  });

  await play(page); // waits on the download
  await page.waitForTimeout(200);
  await play(page); // and is paused before it lands
  release();
  await page.waitForTimeout(1500);

  // Only the first play's count-in: the waiting one was dropped.
  expect((await countIns(page)).length).toBe(1);
  expect(await page.evaluate(async () => (await import("/js/state.js")).audioEngine.isPlaying())).toBe(false);
});
