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

/** Answer GET /api/settings as the server does, on a GPU of the given backend. */
async function stubGpuBackend(page, backend) {
  await page.route("**/api/settings", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const response = await route.fetch();
    const json = await response.json();
    return route.fulfill({
      response,
      json: {
        ...json,
        gpu_backend: backend,
        demucs_device_resolved: "cuda",
        demucs_devices_available: ["cuda", "cpu"],
      },
    });
  });
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

  // A ROCm build of torch serves an AMD GPU as "cuda" (#825), so the server
  // says which GPU that is. Faked here: no AMD GPU is needed to see the words.
  test("on an AMD GPU, the settings name it and Auto covers it", async ({ page }) => {
    await stubGpuBackend(page, "rocm");
    await openSettings(page);
    await expect(page.locator(".set-transcribe-lyrics option").first()).toHaveText("Auto (NVIDIA/AMD GPU only)");
    await expect(page.locator(".set-transcribe-desc")).toContainText("only on an NVIDIA/AMD GPU");

    await page.locator('.settings-tab[data-tab="general"]').click();
    await expect(page.locator('.set-demucs-device option[value="cuda"]')).toHaveText("ROCm (AMD)");
    await expect(page.locator(".set-demucs-desc")).toContainText("(currently: cuda, ROCm)");
  });

  test("on an AMD GPU, the wording is translated too", async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem("stemdeck.language", JSON.stringify("de")));
    await stubGpuBackend(page, "rocm");
    await openSettings(page);
    await expect(page.locator(".set-transcribe-lyrics option").first()).toHaveText("Auto (nur NVIDIA/AMD-GPU)");
    await expect(page.locator(".set-transcribe-desc")).toContainText("nur auf einer NVIDIA/AMD-GPU");
  });

  test("on an NVIDIA GPU, nothing mentions AMD", async ({ page }) => {
    await stubGpuBackend(page, "cuda");
    await openSettings(page);
    await expect(page.locator(".set-transcribe-lyrics option").first()).toHaveText("Auto (NVIDIA GPU only)");
    await expect(page.locator(".set-transcribe-desc")).toContainText("only on an NVIDIA GPU");

    await page.locator('.settings-tab[data-tab="general"]').click();
    await expect(page.locator('.set-demucs-device option[value="cuda"]')).toHaveText("CUDA (NVIDIA)");
    await expect(page.locator(".set-demucs-desc")).toContainText("(currently: cuda)");
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
