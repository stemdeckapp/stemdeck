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
import { JOB_ID, openStudio } from "./helpers.mjs";

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

// The sections bar had no control of its own, so the nearest button with
// "structure" in its name got the click meant to put it away (#634). Its Hide
// button is the same switch as Collapse > Sections, not a second state.
test.describe("the sections bar's own Hide button", () => {
  const ribbon = (page) => page.locator(".daw-section-ribbon");
  const hide = (page) => page.locator("#sectionsHideBtn");
  const toggle = (page) => page.locator('.daw-panel-toggle[data-panel="sections"]');

  // One test adds a section, and the fixture is shared with every other spec.
  test.afterEach(async ({ page }) => {
    await page.request.patch(`/api/jobs/${JOB_ID}/sections`, { data: { sections: [] } });
  });

  test("hides the bar from the keyboard, and Collapse brings it back", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await expect(ribbon(page)).toBeVisible();
    await expect(hide(page)).toHaveAccessibleName("Hide the sections bar");

    await hide(page).focus();
    await page.keyboard.press("Enter");
    await expect(ribbon(page)).toBeHidden();
    expect((await state(page)).hidden).toEqual(["sections"]);
    await expect(toggle(page)).toHaveAttribute("aria-pressed", "false");
    // The button just vanished with the bar. Focus lands on the way back
    // rather than falling to the page.
    await expect(toggle(page)).toBeFocused();

    await page.keyboard.press("Enter");
    await expect(ribbon(page)).toBeVisible();
    await expect(hide(page)).toBeVisible();
    expect((await state(page)).hidden).toEqual([]);
  });

  test("its choice is the Collapse row's, reload and all", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await hide(page).click();

    await page.reload({ waitUntil: "domcontentloaded" });
    await expect.poll(async () => (await state(page)).hidden).toEqual(["sections"]);
    await expect(toggle(page)).toHaveAttribute("aria-pressed", "false");
    await expect(ribbon(page)).toBeHidden();
  });

  // The header is a fixed 300px column, and German leaves the least room:
  // Clear, Add and Hide fit with about 12px to spare. While "Gespeichert"
  // shows after an edit, the indicator has to give way rather than run over
  // the title or push Hide out of the column.
  test("fits its column in German, while it saves too", async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem("stemdeck.language", JSON.stringify("de")));
    await openStudio(page, { tauri: true });
    const fits = () => page.evaluate(() => {
      const head = document.querySelector(".daw-sections-header").getBoundingClientRect();
      const title = document.querySelector(".daw-sections-header .daw-label-title").getBoundingClientRect();
      const shown = [...document.querySelectorAll(".sections-header-actions > *")].filter((el) => el.getBoundingClientRect().width);
      const first = shown[0].getBoundingClientRect();
      const hide = document.getElementById("sectionsHideBtn").getBoundingClientRect();
      return { clearOfTitle: first.left >= title.right, insideColumn: hide.right <= head.right, height: Math.round(head.height) };
    });
    const idle = await fits();
    expect(idle.insideColumn).toBe(true);

    await page.locator("#sectionsAddBtn").click();
    await expect(page.locator("#sectionsClearBtn")).toBeVisible();
    await expect(page.locator("#sectionsSaveIndicator")).toHaveClass(/saved/);
    expect(await fits()).toEqual({ clearOfTitle: true, insideColumn: true, height: idle.height });
  });

  test("changes nothing but the sections bar", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await hide(page).click();

    const s = await state(page);
    expect(s.hidden).toEqual(["sections"]);
    expect(s.libraryCollapsed).toBe(false);
  });
});
