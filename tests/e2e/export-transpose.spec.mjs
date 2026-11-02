// A transposed mix in the export menu follows whether the backend's ffmpeg has
// the rubberband filter (#858).
//
// The macOS download and Homebrew's plain ffmpeg have none, and the export
// used to fail with a 422 only after the user had picked where to save it.
// /api/health reports transpose_export; on false, a mix with a key moved
// cannot be exported as Mix or Region, the menu says why, and the stems zip
// (never transposed) stays. The real answer depends on this machine's ffmpeg,
// so every test here fixes it.

import { test, expect } from "@playwright/test";
import { openStudio, exportUi, stubTransposeExport } from "./helpers.mjs";

const REASON =
  "A transposed mix cannot be exported: that needs an FFmpeg with Rubber Band support. Put every part back in the original key to export it.";

const keyUp = (page) => page.locator("#t-pitch-up");
const keyReset = (page) => page.locator("#t-pitch-reset");
const keyValue = (page) => page.locator("#t-pitch-value");
const reason = (ui) => ui.panel.locator(".export-transpose-unavailable");

/** Count the mixdown requests the page makes from here on. */
function watchMixdowns(page) {
  const asked = [];
  page.on("request", (req) => {
    if (/\/mixdown\.\w+\?/.test(req.url())) asked.push(req.url());
  });
  return asked;
}

test("without rubberband a transposed mix is held back with the reason", async ({ page }) => {
  await stubTransposeExport(page, false);
  await openStudio(page);
  await keyUp(page).click();
  await expect(keyValue(page)).toHaveText("+1");
  const asked = watchMixdowns(page);

  const ui = exportUi(page);
  await ui.open();
  await expect(ui.mix).toHaveAttribute("aria-disabled", "true");
  await expect(ui.mix).toHaveAttribute("title", REASON);
  await expect(ui.region).toHaveAttribute("aria-disabled", "true");
  await expect(reason(ui)).toBeVisible();
  await expect(reason(ui)).toHaveText(REASON);
  // The stems zip carries no key change, so it stays.
  await expect(ui.stems).not.toHaveAttribute("aria-disabled", "true");

  // A click starts nothing: no download, no request.
  let downloads = 0;
  page.on("download", () => downloads++);
  await ui.mix.click({ force: true });
  await expect(ui.label).toHaveText("Export Mix");
  await page.waitForTimeout(500);
  expect(downloads).toBe(0);
  expect(asked).toEqual([]);
});

test("without rubberband a mix with every key at 0 exports as before", async ({ page }) => {
  await stubTransposeExport(page, false);
  await openStudio(page);
  await keyUp(page).click();
  await expect(keyValue(page)).toHaveText("+1");
  await keyReset(page).click();
  await expect(keyValue(page)).toHaveText("0");

  const ui = exportUi(page);
  await ui.open();
  await expect(ui.mix).not.toHaveAttribute("aria-disabled", "true");
  await expect(ui.mix).not.toHaveAttribute("title", REASON);
  await expect(reason(ui)).toBeHidden();

  const download = page.waitForEvent("download");
  await ui.mix.click();
  const url = (await download).url();
  expect(url).toMatch(/\/mixdown\.wav\?/);
  expect(url).not.toContain("pitches=");
});

test("with rubberband a transposed mix is offered as before", async ({ page }) => {
  await stubTransposeExport(page, true);
  await openStudio(page);
  await keyUp(page).click();
  await expect(keyValue(page)).toHaveText("+1");

  const ui = exportUi(page);
  await ui.open();
  await expect(ui.mix).not.toHaveAttribute("aria-disabled", "true");
  await expect(reason(ui)).toBeHidden();

  const download = page.waitForEvent("download");
  await ui.mix.click();
  const url = (await download).url();
  expect(url).toMatch(/\/mixdown\.wav\?/);
  expect(url).toContain("pitches=");
});

test("an unknown answer leaves a transposed mix offered", async ({ page }) => {
  await stubTransposeExport(page, null);
  await openStudio(page);
  await keyUp(page).click();
  const ui = exportUi(page);
  await ui.open();
  await expect(ui.mix).not.toHaveAttribute("aria-disabled", "true");
  await expect(reason(ui)).toBeHidden();
});

test("the Guide explains the reason", async ({ page }) => {
  await stubTransposeExport(page, false);
  await openStudio(page);
  await keyUp(page).click();
  await page.locator("#helpModeBtn").click();
  await expect(page.locator("body")).toHaveClass(/help-mode/);
  const ui = exportUi(page);
  await ui.open();
  await reason(ui).hover();
  await expect(page.locator("#helpTip")).toContainText("Appears when a part is transposed");
});
