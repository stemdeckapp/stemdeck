// The Song structure toggle is a choice about the next import, not a saved
// preference. It enables a CPU pass measured in minutes, so it has to be
// something the user asked for just now rather than something an earlier
// session, or the previous song, left switched on.
//
// The button and the server setting have to move together. The setting is what
// the runner reads, so a button that merely looks off would leave the next
// import still paying for a pass nobody asked for -- which is why every
// assertion here checks both.

import { test, expect } from "@playwright/test";
import { openStudio, seedLibrary, stubExportEndpoints, stubUpdateCheck, JOB_ID } from "./helpers.mjs";

const button = (page) => page.locator("#autoSectionsBtn");

const setting = (page) =>
  page.evaluate(() =>
    fetch("/api/settings", { cache: "no-store" }).then((r) => r.json()).then((d) => d.auto_sections));

const putSettingOn = (page) =>
  page.evaluate(() =>
    fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auto_sections: true }),
    }).then((r) => r.json()));

test.describe("song structure toggle", () => {
  test("starts off, and clears a setting an earlier session left on", async ({ page }) => {
    await seedLibrary(page);
    await stubExportEndpoints(page);
    await stubUpdateCheck(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });

    // Leave it on the way a previous session would have.
    await putSettingOn(page);
    expect(await setting(page)).toBe(true);

    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(button(page)).toHaveAttribute("aria-pressed", "false");
    // The button being off is not the point on its own: the setting behind it
    // is what the next import would have read.
    await expect.poll(() => setting(page)).toBe(false);
  });

  test("switching it on holds until a song is opened, then clears", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await expect(button(page)).toHaveAttribute("aria-pressed", "false");

    await button(page).click();
    await expect(button(page)).toHaveAttribute("aria-pressed", "true");
    // It has to survive long enough to be used: this is the state an import
    // submitted right now would capture.
    await expect.poll(() => setting(page)).toBe(true);

    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    await expect(button(page)).toHaveAttribute("aria-pressed", "false");
    await expect.poll(() => setting(page)).toBe(false);
  });

  // Two controls, one word between them. This one runs a pass at import time;
  // the sections bar is shown and hidden by "Sections" in the Collapse row. A
  // reporter read the first as the second, clicked it on an open track, and saw
  // a button change colour while nothing else moved (#634).
  test("it does not show or hide the sections bar, which has its own control", async ({ page }) => {
    await openStudio(page, { tauri: true });
    const app = page.locator(".app");
    const ribbon = page.locator(".daw-section-ribbon");
    const panelToggle = page.locator('.daw-panel-toggle[data-panel="sections"]');

    await expect(ribbon).toBeVisible();

    // Both directions: the bar is still there whether the toggle goes on or off.
    await button(page).click();
    await expect(button(page)).toHaveAttribute("aria-pressed", "true");
    await expect(ribbon).toBeVisible();
    await button(page).click();
    await expect(button(page)).toHaveAttribute("aria-pressed", "false");
    await expect(ribbon).toBeVisible();

    // And the control that does own the bar still owns it.
    await panelToggle.click();
    await expect(app).toHaveClass(/panel-sections-off/);
    await panelToggle.click();
    await expect(app).not.toHaveClass(/panel-sections-off/);
    await expect(ribbon).toBeVisible();
  });

  // The label is the fix for #634. Naming the work it does at import time is
  // what stops it reading as the switch for the sections bar, so it is pinned
  // rather than left to the next person tidying up wording.
  test("the button names the import, not the sections bar", async ({ page }) => {
    await openStudio(page, { tauri: true });

    await expect(button(page)).toContainText(/detect/i);
    await expect(button(page)).toHaveAttribute("aria-label", /import/i);
    await expect(button(page)).toHaveAttribute("title", /next import/i);
  });
});
