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
  offline: [],
  noSpace: null,
};

/**
 * `platform` stands in for the OS the shell runs on, which decides whether
 * setup may mention an AMD GPU; `gpu` for what the shell's GPU step answers.
 * `cpuPackage` stands in for the `cpu-only` marker of the CPU download, as
 * build_target reports it. `holdBackend` keeps the backend from answering, so
 * the page stays on the status line the GPU step's summary goes into.
 */
async function openWizard(page, { platform, gpu, cpuPackage, holdBackend } = {}) {
  await page.route(`${ORIGIN}/**`, async (route) => {
    const name = new URL(route.request().url()).pathname.slice(1);
    if (name === "started.html") {
      return route.fulfill({ contentType: "text/html", body: "<p>backend</p>" });
    }
    const types = { html: "text/html", js: "text/javascript", css: "text/css" };
    const body = await readFile(path.join(UI, path.basename(name)));
    return route.fulfill({ contentType: types[path.extname(name).slice(1)], body });
  });
  await page.addInitScript(({ origin, platform, gpu, cpuPackage, holdBackend }) => {
    if (platform) {
      Object.defineProperty(Navigator.prototype, "platform", { get: () => platform });
      Object.defineProperty(Navigator.prototype, "userAgentData", { get: () => ({ platform }) });
    }
    const listeners = {};
    let finishWarmup = null;
    const answers = {
      probe_runtime: { pythonReady: true, ffmpegReady: true, torchDevice: null, pythonPath: "python" },
      runtime_pack_status: { manifest: { version: "1" }, installedVersion: "1" },
      build_target: { os: platform?.startsWith("Linux") ? "linux" : "windows", arch: "x64",
        gpu: cpuPackage ? "cpu" : "nvidia" },
      ensure_torch_device: gpu ?? { gpuDetected: false, torchDevice: "cpu", reason: "no-gpu-detected" },
      repair_torchvision: { outcome: "ok" },
      start_backend: { url: `${origin}/started.html` },
    };
    window.__setup = {
      calls: [],
      // Every status line, in order: most are replaced before a test could read them.
      statuses: [],
      emit(name, payload) {
        for (const fn of listeners[name] ?? []) fn({ payload });
      },
      finishWarmup(status) {
        finishWarmup(status);
      },
    };
    new MutationObserver((records) => {
      for (const record of records) {
        if (record.target.id !== "status") continue;
        for (const node of record.addedNodes) window.__setup.statuses.push(node.textContent);
      }
    }).observe(document, { childList: true, subtree: true });
    window.__TAURI__ = {
      core: {
        invoke(cmd) {
          window.__setup.calls.push(cmd);
          if (cmd === "warmup_models") return new Promise((resolve) => (finishWarmup = resolve));
          if (cmd === "start_backend" && holdBackend) return new Promise(() => {});
          if (cmd === "skip_model_warmup") {
            // What the shell does: the warmup is stopped during model 2, so
            // it returns with only the first one ready.
            finishWarmup({ demucsReady: true, beatThisReady: false, sectionsReady: false,
              vocalSplitReady: false, whisperReady: false, offline: [], noSpace: null });
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
  }, { origin: ORIGIN, platform, gpu, cpuPackage, holdBackend });
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
  await expect(notice).toContainText(
    "AI model download skipped, so these were not downloaded: beat detection, song sections, " +
      "karaoke split, lyrics transcription. StemDeck works without them: each one downloads " +
      "the first time a feature needs it."
  );
  // The separation model had arrived before Skip, so nothing about the first separation.
  await expect(notice).not.toContainText("first separation");
  await expect(page.locator("#status")).toHaveText("AI model step finished.");
  await expect(skip).toBeHidden();
  expect(await page.evaluate(() => window.__setup.calls)).toContain("skip_model_warmup");
  // Read before the backend step leaves the page.
  expect(await page.evaluate(() => window.__setup.calls)).not.toContain("start_backend");

  await page.locator("#continue").click();
  await expect(page).toHaveURL(`${ORIGIN}/started.html`);
});

test("offline: the notice names what did not arrive and why", async ({ page }) => {
  await openWizard(page);
  await finish(page, {
    ...ALL_READY,
    beatThisReady: false,
    sectionsReady: false,
    offline: ["beat_this", "sections"],
  });

  const notice = page.locator("#notice");
  await expect(notice).toContainText(
    "could not reach the model download servers (no internet connection, or the connection " +
      "stopped answering), so these AI models were not downloaded: beat detection, song sections."
  );
  await expect(notice).not.toContainText("These AI models could not be downloaded");
  // The separation model arrived, so nothing about the first separation.
  await expect(notice).not.toContainText("first separation");
  await page.locator("#continue").click();
  await expect(page).toHaveURL(`${ORIGIN}/started.html`);
});

test("a model that failed for another reason is not blamed on the connection", async ({ page }) => {
  await openWizard(page);
  await finish(page, { ...ALL_READY, demucsReady: false, vocalSplitReady: false, offline: ["demucs"] });

  const notice = page.locator("#notice");
  await expect(notice).toContainText("so these AI models were not downloaded: stem separation.");
  await expect(notice).toContainText("These AI models could not be downloaded: karaoke split.");
  await expect(notice).toContainText("the first separation downloads the separation model before it starts");
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

// An AMD GPU through ROCm (#825), as the shell reports one. The detection
// itself is covered by the shell's cargo tests against a fake sysfs.
const AMD = { gpuDetected: true, gpuName: "AMD Radeon RX 7900 XTX (gfx1100)", backend: "rocm",
  cudaVersion: "6.4", torchDevice: "cpu", cudaVerified: false };

test("Linux, AMD GPU without /dev/kfd access: setup says how to get it", async ({ page }) => {
  await openWizard(page, { platform: "Linux x86_64", gpu: { ...AMD, reason: "rocm-no-kfd-access" } });
  const details = page.locator("#details");
  await expect(details).toContainText("GPU detected (AMD Radeon RX 7900 XTX (gfx1100)), but StemDeck cannot open /dev/kfd");
  await expect(details).toContainText("sudo usermod -aG render,video $USER");
});

test("Linux, AMD GPU no ROCm build supports: setup names ROCm, not CUDA", async ({ page }) => {
  await openWizard(page, { platform: "Linux x86_64", gpu: { ...AMD, reason: "rocm-unsupported-gpu" } });
  await expect(page.locator("#details")).toContainText("none of the ROCm builds StemDeck can install support it");
});

test("Windows, NVIDIA GPU no CUDA build supports: setup names CUDA", async ({ page }) => {
  await openWizard(page, {
    platform: "Win32",
    gpu: { gpuDetected: true, gpuName: "NVIDIA GeForce GTX 750", backend: "cuda", cudaVersion: "12.4",
      torchDevice: "cpu", cudaVerified: false, reason: "cuda-unsupported-gpu" },
  });
  await expect(page.locator("#details")).toContainText("none of the CUDA builds StemDeck can install support it");
});

// The CPU download skips GPU detection altogether (#1011), so its summary may
// not say whether this computer has a GPU: nobody looked.
const CPU_PACKAGE = { gpuDetected: false, torchDevice: "cpu", cudaVerified: false, reason: "cpu-only-package" };

async function gpuSummary(page, options) {
  await openWizard(page, { ...options, holdBackend: true });
  await finish(page, ALL_READY);
  await expect.poll(() => page.evaluate(() => window.__setup.calls)).toContain("start_backend");
  return page.locator("#status").textContent();
}

test("Windows, CPU download: setup says it is the CPU download, not that there is no GPU", async ({ page }) => {
  const status = await gpuSummary(page, { platform: "Win32", gpu: CPU_PACKAGE, cpuPackage: true });
  expect(status).toBe(
    "This is the CPU download of StemDeck - stem separation will use CPU (to use an NVIDIA GPU, get the NVIDIA download) - starting backend..."
  );
});

test("Linux, CPU download: the GPU download it names covers AMD too", async ({ page }) => {
  const status = await gpuSummary(page, { platform: "Linux x86_64", gpu: CPU_PACKAGE, cpuPackage: true });
  expect(status).toBe(
    "This is the CPU download of StemDeck - stem separation will use CPU (to use an NVIDIA or AMD GPU, get the NVIDIA download) - starting backend..."
  );
});

for (const platform of ["Win32", "Linux x86_64"]) {
  test(`${platform}, CPU download: setup never says it is checking for a GPU or installing GPU support`, async ({ page }) => {
    await gpuSummary(page, { platform, gpu: CPU_PACKAGE, cpuPackage: true });
    const statuses = await page.evaluate(() => window.__setup.statuses);
    // The summary is there, so every line before it was recorded too.
    expect(statuses.at(-1)).toContain("This is the CPU download of StemDeck");
    expect(statuses.filter((line) => /GPU|acceleration/.test(line) && /Checking|Installing/i.test(line))).toEqual([]);
  });
}

test("NVIDIA download that found no GPU: setup says it found none", async ({ page }) => {
  const status = await gpuSummary(page, {
    platform: "Win32",
    gpu: { gpuDetected: false, torchDevice: "cpu", cudaVerified: false, reason: "no-gpu-detected" },
  });
  expect(status).toBe("No NVIDIA GPU found - stem separation will use CPU - starting backend...");
});

test("everything arrived: setup goes straight on", async ({ page }) => {
  await openWizard(page);
  await finish(page, ALL_READY);

  await expect(page).toHaveURL(`${ORIGIN}/started.html`);
});
