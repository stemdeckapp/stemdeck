// Regression tests for the export menu (#335, #337).
//
// #335: "Export All Stems" became permanently unclickable after one export, for
// every track, until the app restarted. It shipped in alpha 15 and a user found
// it. It reproduced only in the desktop build, because in a browser the
// synthetic <a>.click() closes the chip panel before the busy state is applied
// and the bug hides -- so the Tauri-mode cases below are the ones that matter.
//
// #337 fixed it and turned up three more defects in the same state machine,
// each covered here.

import { test, expect } from "@playwright/test";
import { openStudio, exportUi } from "./helpers.mjs";

test.describe("export menu, desktop (Tauri) mode", () => {
  test("every row is usable again after an export completes", async ({ page }) => {
    await openStudio(page, { tauri: true });
    const ui = exportUi(page);

    await ui.open();
    await ui.stems.click();

    // Mid-export: the menu is busy and says so.
    await expect(ui.label).toHaveText(/Exporting/);
    await expect(ui.stems).toHaveAttribute("aria-disabled", "true");
    expect(await page.evaluate(() => window.__e2e.savePending())).toBe(true);

    await page.evaluate(() => window.__e2e.finishSave());

    // #335 itself: without a symmetric reset this row stays disabled forever.
    await expect(ui.label).toHaveText("Export Mix");
    await expect(ui.stems).not.toHaveAttribute("aria-disabled", "true");
    await expect(ui.mix).not.toHaveAttribute("aria-disabled", "true");
  });

  test("a second export still works after the first", async ({ page }) => {
    await openStudio(page, { tauri: true });
    const ui = exportUi(page);

    for (const pass of [1, 2]) {
      await ui.open();
      await ui.stems.click();
      await expect(ui.label).toHaveText(/Exporting/, { timeout: 5000 });
      await page.evaluate(() => window.__e2e.finishSave());
      await expect(ui.label).toHaveText("Export Mix");
      expect(
        await page.evaluate(() => window.__e2e.callsFor("save_audio_file").length),
        `save_audio_file should have fired on pass ${pass}`,
      ).toBe(pass);
    }
  });

  test("the busy state waits for the save, not a fixed timer", async ({ page }) => {
    // #337: the reset used to run on a timer, so a slow save looked finished
    // while it was still writing, and a failed one looked identical to success.
    await openStudio(page, { tauri: true });
    const ui = exportUi(page);

    await ui.open();
    await ui.mix.click();
    await expect(ui.label).toHaveText(/Exporting/);

    await page.waitForTimeout(3000); // comfortably past the 1200 ms guess
    await expect(ui.label).toHaveText(/Exporting/);
    expect(await page.evaluate(() => window.__e2e.savePending())).toBe(true);

    await page.evaluate(() => window.__e2e.finishSave());
    await expect(ui.label).toHaveText("Export Mix");
  });

  test("a failed export says so and leaves the menu usable", async ({ page }) => {
    await openStudio(page, { tauri: true });
    const ui = exportUi(page);

    await ui.open();
    await ui.mix.click();
    await expect(ui.label).toHaveText(/Exporting/);

    await page.evaluate(() => window.__e2e.failSave("disk full"));

    await expect(ui.error).toBeVisible();
    await expect(ui.error).toContainText(/disk full/i);
    // The state machine has to recover from the failure, not just report it.
    await expect(ui.label).toHaveText("Export Mix");
    await expect(ui.mix).not.toHaveAttribute("aria-disabled", "true");
  });

  test("an export failure does not offer to retry the import", async ({ page }) => {
    // #337: export errors reused the import error box, whose "Try again" button
    // sends the user to the URL field -- which has nothing to do with a failed
    // save and loses the studio they were working in.
    await openStudio(page, { tauri: true });
    const ui = exportUi(page);

    await ui.open();
    await ui.mix.click();
    await page.evaluate(() => window.__e2e.failSave("nope"));

    await expect(ui.error).toBeVisible();
    await expect(ui.error).toContainText("Dismiss");
    await expect(ui.error).not.toContainText("Try again");
  });
});

test.describe("export menu, browser mode", () => {
  test("rows recover after the fire-and-forget download path", async ({ page }) => {
    // No Tauri bridge: _triggerDownload falls back to a synthetic <a download>,
    // which reports nothing back, so the reset runs on the timer instead.
    await openStudio(page);
    const ui = exportUi(page);

    await ui.open();
    await ui.stems.click();

    await expect(ui.label).toHaveText("Export Mix", { timeout: 8000 });
    await ui.open();
    await expect(ui.stems).not.toHaveAttribute("aria-disabled", "true");
  });
});

test.describe("format switching", () => {
  test("picking a format updates the radio group", async ({ page }) => {
    await openStudio(page, { tauri: true });
    const ui = exportUi(page);

    await ui.open();
    await expect(ui.fmt("wav")).toHaveAttribute("aria-checked", "true");

    await ui.fmt("flac").click();
    await expect(ui.fmt("flac")).toHaveAttribute("aria-checked", "true");
    await expect(ui.fmt("wav")).toHaveAttribute("aria-checked", "false");
    await expect(ui.fmt("flac")).toHaveClass(/active/);
  });

  test("MP4 is not offered for a track with no video", async ({ page }) => {
    // The video format only appears once the track actually has one
    // (#footer-export-wrap.has-video). Offering it otherwise produces an export
    // that cannot succeed.
    await openStudio(page, { tauri: true });
    const ui = exportUi(page);

    await ui.open();
    await expect(ui.fmt("mp4")).toBeHidden();
    await expect(ui.stems).toBeVisible();
  });
});

test.describe("busy state", () => {
  test("the menu cannot be reopened mid-export", async ({ page }) => {
    // flashBusy closes the panel and the button ignores clicks while busy, so
    // there is no way to change format or fire a second export underneath the
    // first. This is what makes the "hidden row left disabled" case unreachable
    // from the UI; the reset clears every row regardless.
    await openStudio(page, { tauri: true });
    const ui = exportUi(page);

    await ui.open();
    await ui.mix.click();
    await expect(ui.label).toHaveText(/Exporting/);
    await expect(ui.panel).toHaveClass(/hidden/);

    await ui.button.click();
    await expect(ui.panel).toHaveClass(/hidden/);

    await page.evaluate(() => window.__e2e.finishSave());
    await expect(ui.label).toHaveText("Export Mix");

    // ...and it works again immediately afterwards.
    await ui.open();
    await expect(ui.panel).not.toHaveClass(/hidden/);
  });
});
