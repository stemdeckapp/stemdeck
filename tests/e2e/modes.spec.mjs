// Studio modes (static/js/mode.js). Practice, the default, is the simple
// practice studio: no Record, no adding tracks, no EQ and no master, and a
// song's EQ and master are bypassed in it (live and in exports) rather than
// deleted. DAW is the full studio. The choice is kept per browser.

import { test, expect } from "@playwright/test";
import { openStudio } from "./helpers.mjs";

test.describe("studio modes", () => {
  test("Practice hides the DAW controls, DAW shows them, and the choice is kept", async ({ page }) => {
    await page.addInitScript(() => {
      if (!sessionStorage.getItem("modes-spec")) {
        sessionStorage.setItem("modes-spec", "1");
        localStorage.setItem("stemdeck.mode", "practice");
      }
    });
    await openStudio(page);
    await expect(page.locator("html")).toHaveAttribute("data-mode", "practice");
    for (const sel of ["#t-rec", "#t-master-btn", ".add-track-row", ".mx-rail > .mx-eq-slot"]) {
      await expect(page.locator(sel).first()).toBeHidden();
    }

    await page.locator("#modeBtn").click();
    await expect(page.locator(".mode-item")).toHaveCount(4);
    await expect(page.locator('.mode-item[data-mode="streaming"]')).toHaveCount(0);
    // Advanced, Karaoke and Performance are tagged; Practice is not (#810).
    await expect(page.locator(".mode-item .mode-item-tag")).toHaveText(["Experimental", "Experimental", "Experimental"]);
    await expect(page.locator('.mode-item[data-mode="practice"] .mode-item-tag')).toHaveCount(0);
    await page.locator('.mode-item[data-mode="daw"]').click();
    await expect(page.locator("html")).toHaveAttribute("data-mode", "daw");
    await expect(page.locator("#t-rec")).toBeVisible();
    await expect(page.locator("#t-master-btn")).toBeVisible();

    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.locator("html")).toHaveAttribute("data-mode", "daw");
  });

  test("an EQ set in DAW is bypassed in Practice and back in DAW", async ({ page }) => {
    await openStudio(page);
    const fx = () => page.evaluate(async () => (await import("/js/eqEngine.js")).exportFxParam(["vocals"]));
    await page.evaluate(async () => {
      (await import("/js/eqEngine.js")).setLaneEq("vocals", { params: { out: 0, bands: [{ type: "bell", f: 1000, g: 4, q: 1 }] } });
    });
    expect(await fx()).not.toBe("");
    await page.evaluate(async () => (await import("/js/mode.js")).setMode("practice"));
    expect(await fx()).toBe("");
    await page.evaluate(async () => (await import("/js/mode.js")).setMode("daw"));
    expect(await fx()).not.toBe("");
  });
});
