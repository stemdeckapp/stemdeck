// Memory during playback stays flat (chunkedAudioEngine.js, node onended).
//
// Every played chunk used to stay alive until Stop: 2.1 MB/s on a six stem
// song, measured here as V8 heap plus ArrayBuffer backing stores after a
// forced GC (the decoded audio lives in the backing stores, not the V8 heap).
//
// The 6 second fixture cannot show it, so this needs a real song of at least
// 90 seconds on the server under test, and is skipped otherwise:
//
//   STEMDECK_E2E_MEMORY_JOB=<job id> STEMDECK_E2E_PORT=<port of a server on a
//   library holding it> npx playwright test tests/e2e/playback-memory.spec.mjs
//
// About 90 s. tests/js/chunk-retention.test.mjs covers the same leak in the
// normal suite, against a fake clock.
import { test, expect } from "@playwright/test";

const JOB = process.env.STEMDECK_E2E_MEMORY_JOB || "";
const PLAY_SEC = 75;
const WARMUP_SEC = 15;
// The bug was 2.1 MB/s. A bounded window wobbles by one chunk (about 12 MB
// on six stems) with the prefetch phase, which reads as up to 0.2 MB/s over
// 60 s of samples; anything steadily growing is far above it.
const MAX_SLOPE_MB_PER_SEC = 0.3;

test.skip(!JOB, "set STEMDECK_E2E_MEMORY_JOB to a real song of 90 s or more");
test.use({ launchOptions: { args: ["--mute-audio", "--autoplay-policy=no-user-gesture-required"] } });

test("playing a real song for a minute does not keep growing the page's memory", async ({ page, context }) => {
  test.setTimeout((PLAY_SEC + 60) * 1000);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(`.cat-item[data-id="${JOB}"]`).first().click({ timeout: 30000 });
  await page.waitForFunction(async () => {
    const st = await import("/js/state.js");
    return !!st.audioEngine && (await st.audioEngine.ready) === true;
  }, null, { timeout: 60000 });
  const duration = await page.evaluate(async () => (await import("/js/state.js")).totalDuration);
  expect(duration, "the song must outlast the playback").toBeGreaterThan(PLAY_SEC + 5);

  const cdp = await context.newCDPSession(page);
  await cdp.send("HeapProfiler.enable");
  const usedMB = async () => {
    await cdp.send("HeapProfiler.collectGarbage");
    const u = await cdp.send("Runtime.getHeapUsage");
    return (u.usedSize + (u.backingStorageSize || 0)) / 1e6;
  };

  await page.locator("#t-play").click();
  const t0 = Date.now();
  const samples = [];
  while ((Date.now() - t0) / 1000 < PLAY_SEC) {
    await page.waitForTimeout(5000);
    const t = (Date.now() - t0) / 1000;
    if (t >= WARMUP_SEC) samples.push([t, await usedMB()]);
  }
  expect(await page.evaluate(async () => (await import("/js/state.js")).audioEngine.isPlaying())).toBe(true);
  await page.locator("#t-play").click();

  const n = samples.length;
  const mx = samples.reduce((a, s) => a + s[0], 0) / n;
  const my = samples.reduce((a, s) => a + s[1], 0) / n;
  const slope = samples.reduce((a, s) => a + (s[0] - mx) * (s[1] - my), 0)
    / samples.reduce((a, s) => a + (s[0] - mx) ** 2, 0);
  const span = Math.max(...samples.map((s) => s[1])) - Math.min(...samples.map((s) => s[1]));
  console.log(`playback memory: ${samples.map(([t, mb]) => `${t.toFixed(0)}s ${mb.toFixed(1)}`).join(", ")}; slope ${slope.toFixed(3)} MB/s, span ${span.toFixed(1)} MB`);
  expect(slope, "MB per second after warm-up").toBeLessThan(MAX_SLOPE_MB_PER_SEC);
});
