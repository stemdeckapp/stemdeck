// The Collapse row governs the panels beside it. Not the library.
//
// It used to carry an "All" as well, one press to put every panel away. That
// button is gone: it governed two panels by the end, so it saved a single
// press, and it wore a word the Extract row already uses for something else.
// What survives it is the rule it existed to prove, which is still the thing
// worth guarding: whatever this row collapses, it can also bring back, and the
// library is not one of the things it touches.
//
// "All" used to take the library with it, and nothing in the row could put it
// back: the buttons beside it only know about their own panels, so you could
// turn each panel on again, see the row looking complete, and still be staring
// at a collapsed library with no way to reach it from here (#588). The library
// has its own control in the rail.
import { test, expect } from "@playwright/test";
import { openStudio } from "./helpers.mjs";

const PANELS = ["analysis", "sections"];

const state = (page) =>
  page.evaluate((panels) => {
    const app = document.querySelector(".app");
    return {
      hidden: panels.filter((n) => app.classList.contains(`panel-${n}-off`)),
      libraryCollapsed: app.classList.contains("cat-collapsed"),
      buttons: [...document.querySelectorAll(".daw-panel-toggle")].map((b) => b.textContent.trim()),
    };
  }, PANELS);

const clickPanel = (page, name) =>
  page.locator(`.daw-panel-toggle[data-panel="${name}"]`).click();

test.describe("collapse row", () => {
  test("the row is the panels it governs, and nothing else", async ({ page }) => {
    await openStudio(page, { tauri: true });

    const s = await state(page);
    expect(s.buttons).toEqual(["Analysis", "Sections"]);
    // No "All": one press saved is not worth a third control called All in a
    // bar that already has one.
    expect(s.buttons).not.toContain("All");
  });

  test("every panel it puts away, it can bring back", async ({ page }) => {
    await openStudio(page, { tauri: true });
    expect((await state(page)).hidden).toEqual([]);

    for (const name of PANELS) await clickPanel(page, name);
    expect((await state(page)).hidden).toEqual(PANELS);

    // The way back is the same button. Collapsing to nothing is only safe
    // because of this.
    for (const name of PANELS) await clickPanel(page, name);
    expect((await state(page)).hidden).toEqual([]);
  });

  test("it leaves the library alone in both directions", async ({ page }) => {
    await openStudio(page, { tauri: true });
    expect((await state(page)).libraryCollapsed).toBe(false);

    for (const name of PANELS) await clickPanel(page, name);
    // The bug was that the library went with them and this row could not
    // return it (#588).
    expect((await state(page)).libraryCollapsed).toBe(false);
  });

  test("collapsing the library does not disturb the panels", async ({ page }) => {
    // The library's class lands on the same element the panels' classes do, so
    // this is the check that the two are genuinely independent rather than
    // accidentally not colliding.
    await openStudio(page, { tauri: true });
    await clickPanel(page, "analysis");

    await page.locator("#sidebarCollapseBtn").click();
    await expect.poll(async () => (await state(page)).libraryCollapsed).toBe(true);

    expect((await state(page)).hidden).toEqual(["analysis"]);
  });

  test("a collapsed panel survives a reload", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await clickPanel(page, "sections");
    expect((await state(page)).hidden).toEqual(["sections"]);

    await page.reload({ waitUntil: "domcontentloaded" });
    await expect.poll(async () => (await state(page)).hidden).toEqual(["sections"]);
  });
});
