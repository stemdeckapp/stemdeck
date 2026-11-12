// OGG in the export menu follows what the backend's ffmpeg can encode (#824).
//
// libvorbis is optional: the macOS download and Homebrew's plain ffmpeg have
// none. /api/health reports export_formats, and without libvorbis the OGG
// button stays visible but disabled, with its reason in the menu. The real
// answer depends on this machine's ffmpeg, so every test here fixes it.

import { test, expect } from "@playwright/test";
import { openStudio, exportUi, stubExportFormats } from "./helpers.mjs";

const NO_VORBIS = { wav: true, mp3: true, flac: true, ogg: false };
const WITH_VORBIS = { wav: true, mp3: true, flac: true, ogg: true };
const REASON = "OGG export is unavailable: it needs an FFmpeg with Vorbis support.";

test("without libvorbis OGG is disabled, says why, and cannot be chosen", async ({ page }) => {
  await stubExportFormats(page, NO_VORBIS);
  await openStudio(page);
  const ui = exportUi(page);
  await ui.open();

  const ogg = ui.fmt("ogg");
  await expect(ogg).toBeVisible();
  await expect(ogg).toBeDisabled();
  await expect(ogg).toHaveAttribute("title", REASON);
  await expect(ui.panel.locator(".export-ogg-unavailable")).toBeVisible();
  await expect(ui.panel.locator(".export-ogg-unavailable")).toHaveText(REASON);

  // A click does nothing: WAV stays the format, so no export asks for OGG.
  await ogg.click({ force: true });
  await expect(ogg).toHaveAttribute("aria-checked", "false");
  await expect(ui.fmt("wav")).toHaveAttribute("aria-checked", "true");

  // The other formats are unaffected.
  await ui.fmt("mp3").click();
  await expect(ui.fmt("mp3")).toHaveAttribute("aria-checked", "true");
});

test("with libvorbis OGG is offered as before", async ({ page }) => {
  await stubExportFormats(page, WITH_VORBIS);
  await openStudio(page);
  const ui = exportUi(page);
  await ui.open();

  const ogg = ui.fmt("ogg");
  await expect(ogg).toBeEnabled();
  await expect(ogg).not.toHaveAttribute("title", REASON);
  await expect(ui.panel.locator(".export-ogg-unavailable")).toBeHidden();
  await ogg.click();
  await expect(ogg).toHaveAttribute("aria-checked", "true");
});

test("an unknown capability leaves OGG offered", async ({ page }) => {
  await stubExportFormats(page, null);
  await openStudio(page);
  const ui = exportUi(page);
  await ui.open();
  await expect(ui.fmt("ogg")).toBeEnabled();
  await expect(ui.panel.locator(".export-ogg-unavailable")).toBeHidden();
});
