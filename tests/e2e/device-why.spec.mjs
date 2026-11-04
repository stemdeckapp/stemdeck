// Why the NVIDIA GPU is not used, under the Compute device picker (#247).
//
// Settings used to say only "currently: cpu", so a CPU-only PyTorch, a CUDA
// that cannot start and a failed CUDA install all looked the same. The server
// now sends one code and a diagnostics line. This suite's backend has whatever
// GPU the runner has, so /api/settings is answered from the real response with
// only those two fields set; the code itself is covered in
// tests/test_device_reason.py.

import { test, expect } from "@playwright/test";
import { seedLibrary } from "./helpers.mjs";

const DIAGNOSTICS =
  "gpu=NVIDIA GeForce RTX 3080, driver=616.64, driver_cuda=13.4, torch=2.6.0+cu124, " +
  "torch_cuda=12.4, cuda_available=no, setup=none, why=cuda-unavailable";

async function openSettings(page, why) {
  await page.route("**/api/settings", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const real = await (await route.fetch()).json();
    return route.fulfill({
      json: { ...real, demucs_device_why: why, device_diagnostics: DIAGNOSTICS },
    });
  });
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  await expect(page.locator(".library-editor")).toBeVisible();
}

test("the reason the GPU is not used shows under the Compute device picker", async ({ page }) => {
  await openSettings(page, "cuda-unavailable");
  const row = page.locator(".device-why");
  await expect(row).toBeVisible();
  await expect(row.locator(".set-demucs-why")).toHaveText(
    "Separation runs on the processor: GPU support is installed, but it could not start on the " +
      "NVIDIA graphics card. Restarting the computer or updating the NVIDIA driver can fix this.",
  );
});

test("Copy GPU details puts the diagnostics line on the clipboard", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await openSettings(page, "cpu-torch-build");
  await page.locator(".set-device-copy").click();
  await expect(page.locator(".set-device-copy-status")).toHaveText(
    "Copied. Paste it into your bug report.",
  );
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(DIAGNOSTICS);
});

test("nothing is shown while the GPU is in use or the machine has none", async ({ page }) => {
  await openSettings(page, null);
  await expect(page.locator(".set-demucs-device")).toBeVisible();
  await expect(page.locator(".device-why")).toBeHidden();
});

test("the reason follows the language", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("stemdeck.language", JSON.stringify("de")));
  await openSettings(page, "driver-too-old");
  await expect(page.locator(".set-demucs-why")).toHaveText(
    "Das Zerlegen läuft auf dem Prozessor: Der NVIDIA-Treiber ist zu alt für die installierte " +
      "GPU-Unterstützung. Ein neuerer NVIDIA-Treiber behebt das.",
  );
});
