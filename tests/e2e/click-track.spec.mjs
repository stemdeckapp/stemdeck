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

  test("the count-in length is chosen from the select and survives a reload", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page);
    // #587 turned the count-in from an on/off button into a length select, so
    // "armed" is now a non-zero value rather than a pressed state. The tint
    // moved to the wrapper with it.
    await metro(page).countIn.selectOption("3");
    await expect(metro(page).countIn).toHaveValue("3");
    await expect(page.locator("#t-metro-countin").locator("xpath=..")).toHaveClass(/active/);

    await openStudio(page, { tauri: true });
    await waitForClickTrack(page);
    // Restored from the store, not merely left in the DOM.
    await expect(metro(page).countIn).toHaveValue("3");

    await metro(page).countIn.selectOption("0");
    await expect(page.locator("#t-metro-countin").locator("xpath=..")).not.toHaveClass(/active/);
  });

  test("a custom meter can be typed and drives the accent note", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page);
    const ui = metro(page);
    const custom = page.locator("#t-metro-bar-custom");

    // Presets leave the free-entry box hidden. 5 carries a grouping, so the
    // note describes the 3+2 it is actually playing rather than a flat accent.
    await ui.accent.selectOption("5");
    await expect(custom).toBeHidden();
    await expect(ui.note).toContainText("counting 5 in 3+2");

    // "Custom..." reveals it, and a typed value is what actually applies.
    await ui.accent.selectOption("custom");
    await expect(custom).toBeVisible();
    await custom.fill("11");
    await custom.blur();
    await expect(ui.note).toContainText("accenting every 11 beats");

    // Out of range is clamped to what the backend accepts, never rejected.
    await custom.fill("99");
    await custom.blur();
    await expect(custom).toHaveValue("32");
    await expect(ui.note).toContainText("accenting every 32 beats");
  });

  test("an odd meter exposes its grouping, and a bad one is refused", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page);
    const ui = metro(page);
    const group = page.locator("#t-metro-group");

    // Simple meters have one sensible reading, so there is nothing to show.
    await ui.accent.selectOption("4");
    await expect(group).toBeHidden();

    // 7 is played 3+2+2, and the box says so before the user touches it (#595).
    await ui.accent.selectOption("7");
    await expect(group).toBeVisible();
    await expect(group).toHaveValue("3+2+2");
    // The box is labelled and the note spells out what "3+2+2" does, so the
    // grouping is not a bare number the user has to decode.
    await expect(page.locator("#t-metro-group-label")).toBeVisible();
    await expect(ui.note).toContainText("counting 7 in 3+2+2");
    await expect(ui.note).toContainText("stress on 1, 4, 6");

    // A grouping that fits the bar is taken.
    await group.fill("2+2+3");
    await group.blur();
    await expect(group).toHaveValue("2+2+3");
    await expect(ui.note).toContainText("stress on 1, 3, 5");

    // One that does not is refused rather than repaired -- and says so, because
    // a box that snaps back on its own reads as broken rather than as refused.
    await group.fill("3+3");
    await group.blur();
    await expect(group).toHaveValue("3+2+2");
    await expect(group).toHaveClass(/invalid/);
    await expect(ui.note).toContainText("add up to 7");

    // Clearing the box is a deliberate "use the default", not a mistake, so it
    // must not be scolded.
    await group.fill("");
    await group.blur();
    await expect(group).toHaveValue("3+2+2");
    await expect(group).not.toHaveClass(/invalid/);

    // Leaving the odd meter puts the control away again, and the note goes back
    // to describing a plain accent rather than a grouping that is not playing.
    await ui.accent.selectOption("4");
    await expect(group).toBeHidden();
    await expect(page.locator("#t-metro-group-label")).toBeHidden();
    await expect(ui.note).toContainText("accenting every 4 beats");
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
