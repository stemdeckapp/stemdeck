// Settings > Logs > Export logs.
//
// In the desktop app the zip goes through the native save dialog, like every
// other export. A download inside the window is saved by WebView2 straight to
// the user's Downloads folder without asking, which puts a file outside a
// portable StemDeck's own folder in a place the user never chose.

import { test, expect } from "@playwright/test";
import { seedLibrary, stubTauri } from "./helpers.mjs";

async function openLogs(page) {
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  await page.locator('.settings-tab[data-tab="logs"]').click();
  await expect(page.locator(".settings-export-logs")).toBeVisible();
}

test("desktop: the logs zip is saved where the user picks, never by the window", async ({ page }) => {
  await stubTauri(page);
  await openLogs(page);
  const downloads = [];
  page.on("download", (download) => downloads.push(download.suggestedFilename()));

  const button = page.locator(".settings-export-logs");
  await button.click();
  await expect.poll(() => page.evaluate(() => window.__e2e.pickPending())).toBe(true);
  const [pick] = await page.evaluate(() => window.__e2e.callsFor("pick_export_destination"));
  expect(pick.args.filename).toMatch(/^stemdeck-logs-\d{14}\.zip$/);
  // Nothing is being exported while the dialog is open.
  await expect(button).toHaveText("Export logs");

  await page.evaluate(() => window.__e2e.choosePath());
  await expect.poll(() => page.evaluate(() => window.__e2e.savePending())).toBe(true);
  const [transfer] = await page.evaluate(() => window.__e2e.callsFor("download_to_path"));
  expect(transfer.args).toEqual({ token: "token-1", url: new URL("/api/logs.zip", page.url()).href });
  await expect(button).toBeDisabled();
  await expect(button).toHaveText("Preparing…");

  await page.evaluate(() => window.__e2e.finishSave());
  await expect(button).toBeEnabled();
  await expect(button).toHaveText("Export logs");
  expect(downloads).toEqual([]);
});

test("desktop: a cancelled save dialog exports nothing", async ({ page }) => {
  await stubTauri(page);
  await openLogs(page);
  const button = page.locator(".settings-export-logs");
  await button.click();
  await expect.poll(() => page.evaluate(() => window.__e2e.pickPending())).toBe(true);
  await page.evaluate(() => window.__e2e.cancelPick());
  await expect(button).toBeEnabled();
  await expect(button).toHaveText("Export logs");
  expect(await page.evaluate(() => window.__e2e.callsFor("download_to_path"))).toEqual([]);
  await expect(page.getByText("Could not export the logs.")).toHaveCount(0);
});

test("desktop: a failed save says so and gives the button back", async ({ page }) => {
  await stubTauri(page);
  await openLogs(page);
  const button = page.locator(".settings-export-logs");
  await button.click();
  await expect.poll(() => page.evaluate(() => window.__e2e.pickPending())).toBe(true);
  await page.evaluate(() => window.__e2e.choosePath());
  await expect.poll(() => page.evaluate(() => window.__e2e.savePending())).toBe(true);
  await page.evaluate(() => window.__e2e.failSave("backend returned HTTP 500"));
  await expect(button).toBeEnabled();
  await expect(button).toHaveText("Export logs");
  await expect(page.getByText("Could not export the logs.").first()).toBeVisible();
});

test("browser: the logs zip is a browser download", async ({ page }) => {
  await openLogs(page);
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.locator(".settings-export-logs").click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/^stemdeck-logs-\d{14}\.zip$/);
});
