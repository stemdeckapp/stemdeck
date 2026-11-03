// The desktop setup wizard's model step (#405): it says which model it is on,
// offers Skip, and says what did not arrive before it leaves the page.
//
// desktop/ui is the page the desktop app opens before any backend exists, so
// it is served here from disk under a made-up origin, with a stand-in for the
// Tauri bridge that the test drives. No Rust runs: the shell's side (stopping
// the warmup, the stall deadline) is covered by its cargo tests.
import { test, expect } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const UI = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../desktop/ui");
const ORIGIN = "http://setup.test";

const ALL_READY = {
  demucsReady: true,
  beatThisReady: true,
  sectionsReady: true,
  vocalSplitReady: true,
  whisperReady: true,
  offline: false,
  noSpace: null,
};

async function openWizard(page) {
  await page.route(`${ORIGIN}/**`, async (route) => {
    const name = new URL(route.request().url()).pathname.slice(1);
    if (name === "started.html") {
      return route.fulfill({ contentType: "text/html", body: "<p>backend</p>" });
    }
    const types = { html: "text/html", js: "text/javascript", css: "text/css" };
    const body = await readFile(path.join(UI, path.basename(name)));
    return route.fulfill({ contentType: types[path.extname(name).slice(1)], body });
  });
  await page.addInitScript((origin) => {
    const listeners = {};
    let finishWarmup = null;
    const answers = {
      probe_runtime: { pythonReady: true, ffmpegReady: true, torchDevice: null, pythonPath: "python" },
      runtime_pack_status: { manifest: { version: "1" }, installedVersion: "1" },
      ensure_torch_device: { gpuDetected: false, torchDevice: "cpu", reason: "no-gpu" },
      repair_torchvision: { outcome: "ok" },
      start_backend: { url: `${origin}/started.html` },
    };
    window.__setup = {
      calls: [],
      emit(name, payload) {
        for (const fn of listeners[name] ?? []) fn({ payload });
      },
      finishWarmup(status) {
        finishWarmup(status);
      },
    };
    window.__TAURI__ = {
      core: {
        invoke(cmd) {
          window.__setup.calls.push(cmd);
          if (cmd === "warmup_models") return new Promise((resolve) => (finishWarmup = resolve));
          if (cmd === "skip_model_warmup") {
            // What the shell does: the warmup is stopped, so it returns with
            // nothing ready.
            finishWarmup({ demucsReady: false, beatThisReady: false, sectionsReady: false,
              vocalSplitReady: false, whisperReady: false, offline: false, noSpace: null });
          }
          return Promise.resolve(answers[cmd] ?? null);
        },
      },
      event: {
        async listen(name, fn) {
          (listeners[name] ??= []).push(fn);
          return () => {
            listeners[name] = listeners[name].filter((other) => other !== fn);
          };
        },
      },
    };
  }, ORIGIN);
  await page.goto(`${ORIGIN}/index.html`);
  await expect(page.locator('[data-step="model"]')).toHaveClass(/active/);
  await expect.poll(() => page.evaluate(() => window.__setup.calls)).toContain("warmup_models");
}

const finish = (page, status) => page.evaluate((s) => window.__setup.finishWarmup(s), status);

test("the model step names the model it is on, and Skip says what the first use costs", async ({ page }) => {
  await openWizard(page);
  const skip = page.locator("#skip-models");
  // Not before the warmup has said anything: until then it may not be running yet.
  await expect(skip).toBeHidden();

  await page.evaluate(() =>
    window.__setup.emit("model-warmup-progress", { model: "beat_this", number: 2, count: 5 })
  );
  await expect(page.locator("#status")).toContainText("Getting AI model 2 of 5: beat detection...");
  await expect(skip).toBeVisible();

  await skip.click();

  const notice = page.locator("#notice");
  await expect(notice).toContainText("AI model download skipped.");
  await expect(notice).toContainText("the first separation downloads the separation model before it starts");
  await expect(skip).toBeHidden();
  expect(await page.evaluate(() => window.__setup.calls)).toContain("skip_model_warmup");
  // Read before the backend step leaves the page.
  expect(await page.evaluate(() => window.__setup.calls)).not.toContain("start_backend");

  await page.locator("#continue").click();
  await expect(page).toHaveURL(`${ORIGIN}/started.html`);
});

test("offline: the notice names what did not arrive and why", async ({ page }) => {
  await openWizard(page);
  await finish(page, { ...ALL_READY, beatThisReady: false, sectionsReady: false, offline: true });

  const notice = page.locator("#notice");
  await expect(notice).toContainText("could not reach the model download servers");
  await expect(notice).toContainText(": beat detection, song sections.");
  // The separation model arrived, so nothing about the first separation.
  await expect(notice).not.toContainText("first separation");
  await page.locator("#continue").click();
  await expect(page).toHaveURL(`${ORIGIN}/started.html`);
});

test("a full disk: the notice says how much room the models need", async ({ page }) => {
  await openWizard(page);
  await finish(page, {
    ...ALL_READY,
    demucsReady: false,
    beatThisReady: false,
    sectionsReady: false,
    vocalSplitReady: false,
    whisperReady: false,
    noSpace: { needed: 1_800_000_000, free: 400_000_000 },
  });

  await expect(page.locator("#notice")).toContainText("about 1.8 GB needed, 0.4 GB free, so none was downloaded");
});

test("everything arrived: setup goes straight on", async ({ page }) => {
  await openWizard(page);
  await finish(page, ALL_READY);

  await expect(page).toHaveURL(`${ORIGIN}/started.html`);
});
