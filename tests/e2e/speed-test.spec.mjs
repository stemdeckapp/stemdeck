// Settings > Separation > Speed test (separationSettings.js, speedTestView.js).
//
// No test runs a real model: the suite has no GPU and a real run takes about a
// minute. /api/speed-test is answered here, step by step, and the settings
// POST of "Apply recommendation" is answered here too, so nothing is written
// to the shared backend. The recommendation itself is covered by
// tests/test_speed_test.py, the words and numbers by
// tests/js/speed-test-view.test.mjs.

import { test, expect } from "@playwright/test";
import { seedLibrary } from "./helpers.mjs";

const RESULT = {
  version: 1,
  ts: 1_790_000_000,
  stale: false,
  hw: { cpu: "Test CPU", cores: 8, gpu: "Test GPU", vram_mb: 8192, devices: ["cpu", "cuda"], torch: "2.6.0" },
  disk: { write_mb_s: 812.4, skipped: null, slow: false },
  devices: {
    cpu: {
      htdemucs_6s: { rtf: 0.5, peak_mb: 900, measured: true },
      kim_mel_roformer: { rtf: 3.4, measured: false, estimated_from: "htdemucs_6s" },
    },
    cuda: {
      htdemucs_6s: { rtf: 0.068, peak_mb: 619, measured: true },
      kim_mel_roformer: { rtf: 0.105, measured: false, estimated_from: "htdemucs_6s" },
    },
  },
  recommendation: {
    device: "cuda",
    stems: { vocals: "kim_mel_roformer", drums: "htdemucs_6s", bass: "htdemucs_6s", guitar: "htdemucs_6s", piano: "htdemucs_6s" },
    estimate_minutes_4min: 0.69,
    budget_minutes: 3.0,
    reason: "fits",
  },
};

/**
 * Answer the speed test routes from `script`: the states GET returns in turn
 * (the last one repeats), what POST answers, and the stored result in
 * /api/settings. Records every POST body.
 */
async function stubSpeedTest(page, { states = [{ state: "idle", step: null, pct: 0, error: null, result: null }], start = { status: 202, json: { state: "running" } }, stored = null } = {}) {
  const log = { starts: 0, cancels: 0, settingsPosts: [] };
  let i = 0;
  let base = {};
  await page.route("**/api/speed-test", (route) => {
    if (route.request().method() === "POST") {
      log.starts++;
      return route.fulfill(start);
    }
    // The first GET is the check that this server has the routes.
    const s = states[Math.min(i, states.length - 1)];
    if (log.starts > 0 || i === 0) i++;
    return route.fulfill({ json: s });
  });
  await page.route("**/api/speed-test/cancel", (route) => {
    log.cancels++;
    return route.fulfill({ json: { state: "running", step: "cpu", pct: 50, error: null, result: null } });
  });
  await page.route("**/api/settings", async (route) => {
    const req = route.request();
    if (req.method() === "GET") {
      const res = await route.fetch();
      base = await res.json();
      return route.fulfill({ response: res, json: { ...base, speed_test: stored } });
    }
    const body = JSON.parse(req.postData() || "{}");
    if (!("stem_models" in body) && !("demucs_device" in body)) return route.continue();
    log.settingsPosts.push(body);
    return route.fulfill({
      json: {
        ...base,
        speed_test: stored,
        demucs_device: body.demucs_device ?? base.demucs_device,
        demucs_devices_available: ["cuda", "cpu"],
      },
    });
  });
  return log;
}

async function openSeparation(page) {
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  await page.locator('.settings-tab[data-tab="separation"]').click();
  await expect(page.locator('.settings-pane[data-pane="separation"]')).toBeVisible();
}

const group = (page) => page.locator(".speed-test-group");
const msg = (page) => page.locator(".set-speedtest-msg");

test("a server without the speed test routes shows no speed test, and search does not find it", async ({ page }) => {
  const errors = [];
  page.on("pageerror", (err) => errors.push(String(err)));
  await page.route("**/api/speed-test", (route) => route.fulfill({ status: 404, json: { detail: "Not Found" } }));
  await openSeparation(page);
  await expect(page.locator(".set-demucs-device")).toBeVisible();
  // In the page and hidden, so this cannot pass on a page without the group.
  await expect(group(page)).toHaveCount(1);
  await expect(group(page)).toBeHidden();
  expect(errors).toEqual([]);
  // A hidden group's row is not a search result that opens on nothing.
  await page.locator(".settings-search-input").fill("benchmark");
  await expect(page.locator(".settings-result", { hasText: "Run benchmark" })).toHaveCount(0);
});

test("a run shows its progress, then the result table, the reason and Apply", async ({ page }) => {
  const log = await stubSpeedTest(page, {
    states: [
      { state: "idle", step: null, pct: 0, error: null, result: null },
      { state: "running", step: "disk", pct: 5, error: null, result: null },
      { state: "running", step: "cpu", pct: 40, error: null, result: null },
      { state: "done", step: null, pct: 100, error: null, result: RESULT },
    ],
  });
  await openSeparation(page);
  await expect(group(page)).toBeVisible();
  await expect(page.locator(".set-speedtest-result")).toBeHidden();

  await page.locator(".set-speedtest-run").click();
  expect(log.starts).toBe(1);
  await expect(page.locator(".set-speedtest-cancel")).toBeVisible();
  await expect(page.locator(".set-speedtest-run")).toBeHidden();
  const bar = page.locator('.set-speedtest-progress[role="progressbar"]');
  await expect(bar).toBeVisible();
  await expect(msg(page)).toHaveText("Benchmarking CPU.", { timeout: 10_000 });
  await expect(bar).toHaveAttribute("aria-valuenow", "40");

  await expect(page.locator(".set-speedtest-result")).toBeVisible({ timeout: 10_000 });
  await expect(bar).toBeHidden();
  await expect(page.locator(".set-speedtest-run")).toBeVisible();
  const rows = page.locator(".set-speedtest-table tbody tr");
  await expect(rows).toHaveCount(4);
  await expect(rows.first()).toHaveAttribute("data-device", "cuda");
  // Three columns: no Speed column, the time is for a typical 4-minute song.
  await expect(page.locator(".set-speedtest-table thead th")).toHaveText(["Device", "Model", "Time for a typical 4-minute song"]);
  // Only the recommended device's rows show until "Show all devices".
  await expect(page.locator(".set-speedtest-table tbody tr:visible")).toHaveCount(2);
  const gpuKim = page.locator('.set-speedtest-table tr[data-device="cuda"][data-model="kim_mel_roformer"]');
  await expect(gpuKim).toContainText("(Recommended, estimate)");
  await expect(gpuKim.locator(".speed-test-device")).toHaveText("Graphics card CUDA (NVIDIA)");
  await expect(gpuKim.locator(".speed-test-time-text")).toHaveText("about 25 seconds");
  await expect(gpuKim.locator(".speed-test-rating")).toHaveText("Fast");
  await expect(gpuKim.locator(".speed-test-rating")).toHaveAttribute("data-rating", "fast");
  // The speed factor is the tooltip and part of the cell's own text for a
  // screen reader, never shown as "x real time".
  await expect(gpuKim.locator(".speed-test-time")).toHaveAttribute("title", "9.5 times faster than the song's length");
  await expect(gpuKim.locator(".speed-test-sr")).toHaveText("9.5 times faster than the song's length");
  await expect(page.locator(".set-speedtest-table")).not.toContainText("real time");
  await expect(page.locator(".set-speedtest-table")).not.toContainText(" min");
  // The plan rests on an estimated row, so the headline says "an estimated".
  await expect(page.locator(".speed-test-reason")).toHaveText(
    "Your computer separates a 4-minute song in an estimated 40 seconds. Recommended: your graphics card (NVIDIA), with the best vocal model plus the standard model.",
  );
  await expect(page.locator(".speed-test-why")).toBeHidden();
  await expect(page.locator(".speed-test-estimate-note")).toHaveText("Estimate: not measured on your computer, calculated from the measured one.");
  await expect(page.locator(".speed-test-disk")).toHaveText("The StemData folder writes 812 MB/s.");
  await expect(page.locator(".set-speedtest-apply")).toBeVisible();
  await expect(page.locator(".set-speedtest-stale")).toBeHidden();
});

test("a run while a track is being separated is refused and says so", async ({ page }) => {
  await stubSpeedTest(page, { start: { status: 409, json: { detail: "busy", code: "busy" } } });
  await openSeparation(page);
  await page.locator(".set-speedtest-run").click();
  await expect(msg(page)).toHaveText(
    "A track is being separated or a benchmark is already running. Try again when StemDeck is idle.",
  );
  await expect(msg(page)).toHaveClass(/error/);
  await expect(page.locator(".set-speedtest-cancel")).toBeHidden();
  await expect(msg(page)).toHaveAttribute("role", "status");
});

test("Cancel stops the run and says nothing was changed", async ({ page }) => {
  const log = await stubSpeedTest(page, {
    states: [
      { state: "idle", step: null, pct: 0, error: null, result: null },
      { state: "running", step: "cpu", pct: 30, error: null, result: null },
      { state: "running", step: "cpu", pct: 35, error: null, result: null },
      { state: "cancelled", step: null, pct: 35, error: null, result: null },
    ],
  });
  await openSeparation(page);
  await page.locator(".set-speedtest-run").click();
  await expect(page.locator(".set-speedtest-cancel")).toBeFocused();
  await page.locator(".set-speedtest-cancel").click();
  await expect.poll(() => log.cancels).toBe(1);
  await expect(msg(page)).toHaveText("The benchmark was cancelled. Nothing was changed.", { timeout: 10_000 });
  await expect(page.locator(".set-speedtest-run")).toBeVisible();
  await expect(page.locator(".set-speedtest-run")).toBeFocused();
});

test("a failed run says so", async ({ page }) => {
  await stubSpeedTest(page, {
    states: [
      { state: "idle", step: null, pct: 0, error: null, result: null },
      { state: "failed", step: null, pct: 20, error: "the separation engine could not start", result: null },
    ],
  });
  await openSeparation(page);
  await page.locator(".set-speedtest-run").click();
  await expect(msg(page)).toHaveText("The benchmark did not finish. Nothing was changed.", { timeout: 10_000 });
  await expect(msg(page)).toHaveClass(/error/);
});

test("Apply recommendation saves the device and every stem on auto", async ({ page }) => {
  const log = await stubSpeedTest(page, { stored: RESULT });
  await openSeparation(page);
  await expect(page.locator(".set-speedtest-result")).toBeVisible();
  await page.locator(".set-speedtest-apply").click();
  await expect.poll(() => log.settingsPosts.at(-1)).toEqual({
    demucs_device: "cuda",
    stem_models: { vocals: "auto", drums: "auto", bass: "auto", guitar: "auto", piano: "auto" },
  });
  await expect(msg(page)).toHaveText("Applied. The next track uses this device and the recommended models.");
  // The Compute device row shows what was saved.
  await expect(page.locator(".set-demucs-device")).toHaveValue("cuda");
});

test("a result from other hardware says run it again and offers no Apply", async ({ page }) => {
  await stubSpeedTest(page, { stored: { ...RESULT, stale: true } });
  await openSeparation(page);
  await expect(page.locator(".set-speedtest-result")).toBeVisible();
  await expect(page.locator(".set-speedtest-stale")).toHaveText("This result is from different hardware. Run the benchmark again.");
  await expect(page.locator(".set-speedtest-apply")).toBeHidden();
});

test("a test already running when Settings opens is followed", async ({ page }) => {
  await stubSpeedTest(page, {
    states: [
      { state: "running", step: "cuda", pct: 60, error: null, result: null },
    ],
  });
  await openSeparation(page);
  await expect(page.locator(".set-speedtest-cancel")).toBeVisible();
  await expect(msg(page)).toHaveText("Benchmarking CUDA (NVIDIA).");
});

test("search finds the speed test", async ({ page }) => {
  await stubSpeedTest(page);
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  // The group shows once the server has answered GET /api/speed-test.
  await expect(page.locator(".speed-test-group:not([hidden])")).toHaveCount(1);
  await page.locator(".settings-search-input").fill("benchmark");
  const result = page.locator(".settings-result").filter({ has: page.locator(".settings-result-title", { hasText: "Run benchmark" }) });
  await expect(result).toHaveCount(1);
  await result.click();
  await expect(page.locator(".settings-tab.active")).toHaveAttribute("data-tab", "separation");
});

test("a failed run gives the server's reason when it has one", async ({ page }) => {
  await stubSpeedTest(page, {
    states: [
      { state: "idle", step: null, pct: 0, error: null, error_code: null, result: null },
      { state: "failed", step: null, pct: 10, error: "fixed text", error_code: "model-missing", result: null },
    ],
  });
  await openSeparation(page);
  await page.locator(".set-speedtest-run").click();
  await expect(msg(page)).toHaveText(
    "The separation model is not downloaded yet. Separate one song first, then run the benchmark again.",
    { timeout: 10_000 },
  );
  await expect(msg(page)).toHaveClass(/error/);
});

test("a graphics card that failed gets its own line, and the reason does not say there is none", async ({ page }) => {
  const stored = {
    ...RESULT,
    devices: { cpu: RESULT.devices.cpu },
    failed: { cuda: "timeout" },
    recommendation: { ...RESULT.recommendation, device: "cpu", estimate_minutes_4min: 2, reason: "no-gpu" },
  };
  await stubSpeedTest(page, { stored });
  await openSeparation(page);
  const failed = page.locator('.set-speedtest-table tr.is-failed[data-device="cuda"]');
  await expect(failed).toHaveText("CUDA (NVIDIA) could not be tested. It took too long.");
  await expect(page.locator(".speed-test-reason")).toHaveText(
    "Your computer separates a 4-minute song in an estimated 2 minutes. Recommended: your processor, with the best vocal model plus the standard model.",
  );
  await expect(page.locator(".speed-test-why")).toHaveText("The graphics card could not be tested, so the processor is used.");
  await expect(page.locator(".speed-test-why")).not.toContainText("No graphics card");
  // Only the processor is left: nothing to fold, so no device button.
  await expect(page.locator(".set-speedtest-devices")).toBeHidden();
});

test("Show all devices reveals the other devices' rows, and hides them again", async ({ page }) => {
  await stubSpeedTest(page, { stored: RESULT });
  await openSeparation(page);
  const btn = page.locator(".set-speedtest-devices");
  const cpuRows = page.locator('.set-speedtest-table tr[data-device="cpu"]');
  await expect(btn).toBeVisible();
  await expect(btn).toHaveText("Show all devices");
  await expect(btn).toHaveAttribute("aria-expanded", "false");
  await expect(btn).toHaveAttribute("aria-controls", "settings-speedtest-table");
  await expect(page.locator("#settings-speedtest-table")).toHaveCount(1);
  await expect(cpuRows.first()).toBeHidden();

  await btn.click();
  await expect(btn).toHaveText("Hide other devices");
  await expect(btn).toHaveAttribute("aria-expanded", "true");
  await expect(cpuRows).toHaveCount(2);
  await expect(cpuRows.first()).toBeVisible();
  const cpuKim = page.locator('.set-speedtest-table tr[data-device="cpu"][data-model="kim_mel_roformer"]');
  await expect(cpuKim.locator(".speed-test-time-text")).toHaveText("about 14 minutes");
  await expect(cpuKim.locator(".speed-test-rating")).toHaveText("Slow");
  await expect(cpuKim.locator(".speed-test-time")).toHaveAttribute("title", "takes 3.4 times the song's length");
  const cpu6s = page.locator('.set-speedtest-table tr[data-device="cpu"][data-model="htdemucs_6s"]');
  await expect(cpu6s.locator(".speed-test-time-text")).toHaveText("about 2 minutes");
  await expect(cpu6s.locator(".speed-test-rating")).toHaveText("OK");

  // The keyboard works it too.
  await btn.focus();
  await page.keyboard.press("Enter");
  await expect(btn).toHaveAttribute("aria-expanded", "false");
  await expect(cpuRows.first()).toBeHidden();
});

test("the table is not a scroll box of its own", async ({ page }) => {
  await stubSpeedTest(page, { stored: RESULT });
  await openSeparation(page);
  await page.locator(".set-speedtest-devices").click();
  const wrap = page.locator(".speed-test-table-wrap");
  await expect(wrap).toBeVisible();
  const box = await wrap.evaluate((el) => {
    const cs = getComputedStyle(el);
    return { overflowX: cs.overflowX, overflowY: cs.overflowY, maxHeight: cs.maxHeight, clipped: el.scrollHeight > el.clientHeight + 1 || el.scrollWidth > el.clientWidth + 1 };
  });
  expect(box).toEqual({ overflowX: "visible", overflowY: "visible", maxHeight: "none", clipped: false });
});

test("a plan that is over the goal keeps its reason line, in words", async ({ page }) => {
  const stored = {
    ...RESULT,
    devices: { cpu: RESULT.devices.cpu },
    recommendation: {
      ...RESULT.recommendation,
      device: "cpu",
      stems: { vocals: "htdemucs_6s", drums: "htdemucs_6s", bass: "htdemucs_6s", guitar: "htdemucs_6s", piano: "htdemucs_6s" },
      estimate_minutes_4min: 4.5,
      reason: "cpu-over-budget",
      reasons: ["cpu-over-budget"],
    },
  };
  await stubSpeedTest(page, { stored });
  await openSeparation(page);
  await expect(page.locator(".speed-test-reason")).toHaveText(
    "Your computer separates a 4-minute song in about 5 minutes. Recommended: your processor, with the standard model.",
  );
  await expect(page.locator(".speed-test-why")).toHaveText("Even with the standard model this is more than the goal of 3 minutes.");
});

test("a disk that was not measured says why", async ({ page }) => {
  await stubSpeedTest(page, { stored: { ...RESULT, disk: { write_mb_s: null, skipped: "low-space", slow: false } } });
  await openSeparation(page);
  await expect(page.locator(".speed-test-disk")).toHaveText("The StemData folder was not measured: it has less than 512 MB free.");
});

test("when the server stops answering, the page stops asking and offers Run again", async ({ page }) => {
  await stubSpeedTest(page, {
    states: [
      { state: "idle", step: null, pct: 0, error: null, result: null },
      { state: "running", step: "cpu", pct: 20, error: null, result: null },
    ],
  });
  await openSeparation(page);
  await page.locator(".set-speedtest-run").click();
  await expect(msg(page)).toHaveText("Benchmarking CPU.", { timeout: 10_000 });
  // Every poll from now on fails: eight in a row, about eight seconds.
  await page.route("**/api/speed-test", (route) => route.abort());
  await expect(msg(page)).toHaveText(
    "StemDeck stopped answering, so this page no longer follows the benchmark. Open Settings again to see its result.",
    { timeout: 20_000 },
  );
  await expect(page.locator(".set-speedtest-run")).toBeVisible();
  await expect(page.locator(".set-speedtest-cancel")).toBeHidden();
});

test("a failed Apply says the device and models are unchanged", async ({ page }) => {
  await stubSpeedTest(page, { stored: RESULT });
  await openSeparation(page);
  await page.route("**/api/settings", (route) =>
    route.request().method() === "POST" ? route.fulfill({ status: 500, json: {} }) : route.fallback());
  await page.locator(".set-speedtest-apply").click();
  await expect(msg(page)).toHaveText("Could not apply the recommendation. The compute device and the models are as they were.");
});

test("at phone width the result table scrolls inside its box, not the page", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await stubSpeedTest(page, { stored: RESULT });
  await openSeparation(page);
  await expect(page.locator(".set-speedtest-table")).toBeVisible();
  const pane = page.locator('.settings-pane[data-pane="separation"]');
  expect(await pane.evaluate((el) => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(0);
  const wrap = await page.locator(".speed-test-table-wrap").boundingBox();
  expect(wrap.x).toBeGreaterThanOrEqual(0);
  expect(wrap.x + wrap.width).toBeLessThanOrEqual(375);
  for (const sel of [".set-speedtest-run", ".set-speedtest-apply"]) {
    const box = await page.locator(sel).boundingBox();
    expect(box.x + box.width).toBeLessThanOrEqual(375);
  }
});
