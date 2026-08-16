// The click track, count-in and grid editor in a real browser.
//
// These could not be tested at all until the fixture's beat grid moved to
// stems/beats.json, where the API actually looks. Written to the job root it
// 404'd, the studio said "No beat grid for this track", and every control here
// stayed disabled -- while the fixture looked, from a glance at seed.py, as
// though it covered them. The first test is the one that would have caught it.

import { test, expect } from "@playwright/test";
import { openStudio, waitForClickTrack } from "./helpers.mjs";

const metro = (page) => ({
  toggle: page.locator("#t-metro"),
  panel: page.locator("#t-metro-panel"),
  countIn: page.locator("#t-metro-countin"),
  accent: page.locator("#t-metro-bar"),
  grid: page.locator("#t-metro-edit"),
  half: page.locator("#t-metro-half"),
  double: page.locator("#t-metro-double"),
  note: page.locator("#t-metro-note"),
});

test.describe("click track", () => {
  test("the fixture's beat grid reaches the studio", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page);
    const ui = metro(page);

    // Disabled here means the grid never arrived -- the exact symptom of a
    // beats.json the API cannot find.
    await expect(ui.toggle).toBeEnabled();
    await expect(ui.panel).not.toHaveClass(/hidden/);
    await expect(ui.note).toContainText("120.0 BPM");

    // Bar marks present, so the detected-meter accent mode is live rather than
    // silently degrading to "none found".
    await expect(page.locator('#t-metro-bar option[value="-1"]')).toHaveText("Auto (detected)");
    await expect(page.locator('#t-metro-bar option[value="-1"]')).toBeEnabled();
  });

  test("the click toggles on and off", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page);
    const ui = metro(page);

    await ui.toggle.click();
    await expect(ui.toggle).toHaveClass(/active/);
    await expect(ui.toggle).toHaveAttribute("aria-pressed", "true");

    await ui.toggle.click();
    await expect(ui.toggle).not.toHaveClass(/active/);
    await expect(ui.toggle).toHaveAttribute("aria-pressed", "false");
  });

  test("count-in is a toggle and its choice survives a reload", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page);
    await metro(page).countIn.click();
    await expect(metro(page).countIn).toHaveClass(/active/);

    await openStudio(page, { tauri: true });
    await waitForClickTrack(page);
    // Restored from the store, not merely left in the DOM.
    await expect(metro(page).countIn).toHaveClass(/active/);
    await expect(metro(page).countIn).toHaveAttribute("aria-pressed", "true");
  });

  test("the rate control reports the tempo it is actually clicking", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page);
    const ui = metro(page);

    await ui.double.click();
    await expect(ui.double).toHaveClass(/active/);
    await expect(ui.note).toContainText("240.0 BPM");

    await ui.half.click();
    await expect(ui.half).toHaveClass(/active/);
    await expect(ui.note).toContainText("60.0 BPM");
  });

  test("the accent choice is reflected in the note", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page);
    const ui = metro(page);

    await ui.accent.selectOption("3");
    await expect(ui.note).toContainText("accenting every 3 beats");

    await ui.accent.selectOption("-1");
    await expect(ui.note).toContainText("accenting 4/4 from the detected downbeat");
  });

  test("Grid opens and closes the beat-grid editor", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page);
    const ui = metro(page);
    const toolbar = page.locator("#beatgrid-toolbar");

    await ui.grid.click();
    await expect(toolbar).not.toHaveClass(/hidden/);
    await expect(ui.grid).toHaveClass(/active/);

    // Same button closes it; the editor's own Done must agree.
    await ui.grid.click();
    await expect(toolbar).toHaveClass(/hidden/);
    await expect(ui.grid).not.toHaveClass(/active/);

    await ui.grid.click();
    await page.locator("#bg-done").click();
    await expect(toolbar).toHaveClass(/hidden/);
    await expect(ui.grid).not.toHaveClass(/active/);
  });
});
