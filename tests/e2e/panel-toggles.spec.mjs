// The Collapse row governs three panels. Not the library.
//
// "All" used to take the library with it, and nothing in the row could put it
// back: the three buttons beside it only know about their own panels. So the
// reported sequence was press All, then turn Analysis, Sections and Timeline
// back on one at a time, and watch All light up as though everything had
// returned while the library stayed collapsed with no way to reach it from
// there (#588). The library has its own control in the rail.
import { test, expect } from "@playwright/test";
import { openStudio } from "./helpers.mjs";

const PANELS = ["analysis", "sections", "timeline"];

const state = (page) =>
  page.evaluate((panels) => {
    const app = document.querySelector(".app");
    const all = document.querySelector(".daw-panel-toggle[data-panel-all]");
    return {
      hidden: panels.filter((n) => app.classList.contains(`panel-${n}-off`)),
      libraryCollapsed: app.classList.contains("cat-collapsed"),
      allPressed: all.getAttribute("aria-pressed") === "true",
    };
  }, PANELS);

const clickAll = (page) => page.locator(".daw-panel-toggle[data-panel-all]").click();
const clickPanel = (page, name) => page.locator(`.daw-panel-toggle[data-panel="${name}"]`).click();

test.describe("collapse row", () => {
  test("All leaves the library alone in both directions", async ({ page }) => {
    await openStudio(page, { tauri: true });
    expect((await state(page)).libraryCollapsed).toBe(false);

    await clickAll(page);
    let s = await state(page);
    expect(s.hidden, "every panel it governs is away").toEqual(PANELS);
    expect(s.libraryCollapsed, "the library is not one of them").toBe(false);

    await clickAll(page);
    s = await state(page);
    expect(s.hidden).toEqual([]);
    expect(s.libraryCollapsed).toBe(false);
  });

  test("the reporter's sequence leaves nothing stranded", async ({ page }) => {
    await openStudio(page, { tauri: true });

    // Press All, then bring each panel back from its own button.
    await clickAll(page);
    for (const name of PANELS) await clickPanel(page, name);

    const s = await state(page);
    // The bug was that All read as fully on here while the library was still
    // collapsed and unreachable from this row.
    expect(s.allPressed, "All reflects the row").toBe(true);
    expect(s.hidden).toEqual([]);
    expect(s.libraryCollapsed, "nothing was left collapsed behind it").toBe(false);
  });

  test("All still greys out only when every panel it governs is away", async ({ page }) => {
    await openStudio(page, { tauri: true });

    // One panel hidden must not read as "you pressed All".
    await clickPanel(page, "analysis");
    expect((await state(page)).allPressed).toBe(true);

    await clickPanel(page, "sections");
    expect((await state(page)).allPressed).toBe(true);

    await clickPanel(page, "timeline");
    expect((await state(page)).allPressed, "all three away, so the row is away").toBe(false);
  });

  test("collapsing the library does not change what All reports", async ({ page }) => {
    // The library's class lands on the same element the observer watches, so
    // this is the check that it is genuinely ignored rather than accidentally
    // absent from the count.
    await openStudio(page, { tauri: true });
    await page.locator("#sidebarCollapseBtn").click();
    await expect
      .poll(async () => (await state(page)).libraryCollapsed, { timeout: 5000 })
      .toBe(true);

    expect((await state(page)).allPressed, "the library is not part of the row").toBe(true);

    // And All must not resurrect it on the way past.
    await clickAll(page);
    expect((await state(page)).libraryCollapsed).toBe(true);
    await clickAll(page);
    expect((await state(page)).libraryCollapsed).toBe(true);
  });
});
