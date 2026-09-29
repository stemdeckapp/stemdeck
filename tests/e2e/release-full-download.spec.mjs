// The update dialog when a release cannot be applied in place (#720).
//
// A release that changes the Python dependencies cannot be applied by the
// one-click update, which keeps the installed runtime. The dialog then offers
// only Download, and has to say that this download is a full package rather
// than an update, or a missing "Update now" reads as a broken update.
import { expect, test } from "@playwright/test";
import { stubTauri } from "./helpers.mjs";

const ASSETS = [
  "StemDeck-Windows-x64.NVIDIA.zip",
  "StemDeck-Windows-x64-app.zip",
  "StemDeck-Windows-x64-app.zip.sha256",
  "StemDeck-Windows-x64-runtime-version.json",
].map((name) => ({ name, browser_download_url: `https://example.invalid/${name}` }));

async function installedAppOffered(page, check) {
  await stubTauri(page);
  // After stubTauri's script: a Windows NVIDIA install, and the updater's
  // answer for this release.
  await page.addInitScript((answer) => {
    const invoke = window.__TAURI__.core.invoke;
    window.__TAURI__.core.invoke = (cmd, args) => {
      if (cmd === "build_target") return Promise.resolve({ os: "windows", arch: "x64", gpu: "nvidia" });
      if (cmd === "check_app_update") return Promise.resolve(answer);
      return invoke(cmd, args);
    };
  }, check);
  await page.route("**/api/health**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ name: "StemDeck", status: "ok", version: "0.18.1", ffmpeg_configured: true }),
    }));
  await page.route("https://api.github.com/**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        tag_name: "v0.19.0",
        draft: false,
        prerelease: false,
        body: "notes",
        html_url: "https://example.invalid",
        assets: ASSETS,
      }),
    }));
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#notifBtn").click();
  await page.locator("#notifReleaseCard").click();
  await expect(page.locator("#releaseDialog")).toBeVisible();
}

test.describe("update dialog", () => {
  test("a release that needs a new runtime says it is a full download", async ({ page }) => {
    await installedAppOffered(page, {
      supported: false,
      reason: "python dependencies changed (py3.12-aaaaaaaaaaaaaaaa -> py3.12-bbbbbbbbbbbbbbbb)",
    });
    const note = page.locator("#releaseFullDownload");
    await expect(note).toBeVisible();
    await expect(note).toContainText("full download");
    await expect(page.locator("#releaseDownloadApp")).toBeHidden();
    await expect(page.locator("#releaseDownload")).toHaveAttribute(
      "href",
      "https://example.invalid/StemDeck-Windows-x64.NVIDIA.zip",
    );
  });

  test("any other reason for no in-app update says nothing about a full download", async ({ page }) => {
    await installedAppOffered(page, { supported: false, reason: "this install is not writable by the current user" });
    await expect(page.locator("#releaseDownload")).toBeVisible();
    await expect(page.locator("#releaseFullDownload")).toBeHidden();
  });

  test("a release that updates in place offers Update now and no note", async ({ page }) => {
    await installedAppOffered(page, { supported: true, appSha256: "0".repeat(64) });
    await expect(page.locator("#releaseDownloadApp")).toBeVisible();
    await expect(page.locator("#releaseFullDownload")).toBeHidden();
  });
});
