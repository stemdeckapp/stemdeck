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

// Desktop exports go through a save dialog first. Picking a destination is what
// starts the transfer, so most tests have to answer the dialog before there is
// any busy state to assert on (#338).
const choosePath = (page) => page.evaluate(() => window.__e2e.choosePath());

test.describe("export menu, desktop (Tauri) mode", () => {
  test("every row is usable again after an export completes", async ({ page }) => {
    await openStudio(page, { tauri: true });
    const ui = exportUi(page);

    await ui.open();
    await ui.stems.click();
    await choosePath(page);

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
      await choosePath(page);
      await expect(ui.label).toHaveText(/Exporting/, { timeout: 5000 });
      await page.evaluate(() => window.__e2e.finishSave());
      await expect(ui.label).toHaveText("Export Mix");
      expect(
        await page.evaluate(() => window.__e2e.callsFor("download_to_path").length),
        `the transfer should have fired on pass ${pass}`,
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
    await choosePath(page);
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
    await choosePath(page);
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
    await choosePath(page);
    await page.evaluate(() => window.__e2e.failSave("nope"));

    await expect(ui.error).toBeVisible();
    await expect(ui.error).toContainText("Dismiss");
    await expect(ui.error).not.toContainText("Try again");
  });
});

test.describe("the save dialog phase (#338)", () => {
  test("the label does not claim to be exporting while the picker is open", async ({ page }) => {
    // The whole point of splitting the command. Awaiting one combined
    // save_audio_file meant the button read "Exporting..." from the moment it
    // was clicked, including however long the user spent choosing a folder,
    // when nothing was being exported yet.
    await openStudio(page, { tauri: true });
    const ui = exportUi(page);

    await ui.open();
    await ui.mix.click();

    await expect.poll(() => page.evaluate(() => window.__e2e.pickPending())).toBe(true);
    await expect(ui.label).toHaveText("Export Mix");
    await expect(ui.button).not.toHaveClass(/is-busy/);
    // Nothing has been transferred, so no transfer command has been issued.
    expect(await page.evaluate(() => window.__e2e.callsFor("download_to_path").length)).toBe(0);

    await choosePath(page);
    await expect(ui.label).toHaveText(/Exporting/);
    expect(await page.evaluate(() => window.__e2e.callsFor("download_to_path").length)).toBe(1);

    await page.evaluate(() => window.__e2e.finishSave());
    await expect(ui.label).toHaveText("Export Mix");
  });

  test("cancelling the dialog leaves the menu exactly as it was", async ({ page }) => {
    // No busy state is ever entered, so there is none to unwind.
    await openStudio(page, { tauri: true });
    const ui = exportUi(page);

    await ui.open();
    await ui.mix.click();
    await expect.poll(() => page.evaluate(() => window.__e2e.pickPending())).toBe(true);

    await page.evaluate(() => window.__e2e.cancelPick());

    await expect(ui.label).toHaveText("Export Mix");
    await expect(ui.button).not.toHaveClass(/is-busy/);
    expect(await page.evaluate(() => window.__e2e.callsFor("download_to_path").length)).toBe(0);
    await expect(ui.error).toHaveCount(0);

    // The panel was never closed: only entering the busy state does that, and
    // a cancelled pick never gets there. So the menu is still open and usable.
    await expect(ui.panel).not.toHaveClass(/hidden/);
    await ui.mix.click();
    await choosePath(page);
    await expect(ui.label).toHaveText(/Exporting/);
  });

  test("a second export cannot be queued while the picker is open", async ({ page }) => {
    // The dialog is app-modal on a real desktop, but the guard must not depend
    // on that: `busy` is deliberately still false during this phase.
    await openStudio(page, { tauri: true });
    const ui = exportUi(page);

    await ui.open();
    await ui.mix.click();
    await expect.poll(() => page.evaluate(() => window.__e2e.pickPending())).toBe(true);

    await ui.mix.click();
    await ui.button.click();
    expect(await page.evaluate(() => window.__e2e.callsFor("pick_export_destination").length)).toBe(1);
    expect(await page.evaluate(() => window.__e2e.callsFor("download_to_path").length)).toBe(0);
  });

  test("the transfer is told where to write by token, never by path", async ({ page }) => {
    // The destination stays in Rust. A path argument here would be an arbitrary
    // write primitive for anything running in the WebView.
    await openStudio(page, { tauri: true });
    const ui = exportUi(page);

    await ui.open();
    await ui.mix.click();
    await choosePath(page);

    const args = await page.evaluate(() => window.__e2e.callsFor("download_to_path")[0].args);
    expect(Object.keys(args).sort()).toEqual(["token", "url"]);
    expect(args.token).toBeTruthy();
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
    await choosePath(page);
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
