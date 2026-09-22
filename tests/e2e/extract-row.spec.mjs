// The Extract row's three controls all describe one fact: which stems will be
// extracted. They are the six chips, the All button, and the Lead + Backing
// toggle that only means anything while Vocals is among them.
//
// They used to be painted by whoever changed the selection, and All was synced
// from a closure reachable only from a click. So the two paths that set the
// selection without one, the restore on load and opening a track from the
// library, left it claiming every stem was selected while two chips were lit
// (#658). These assert the row agrees with itself on the path a click never
// touches.
import { test, expect } from "@playwright/test";
import { seedLibrary, stubExportEndpoints, stubUpdateCheck } from "./helpers.mjs";

const SEL_KEY = "stemdeck:selected-stems";

const row = (page) =>
  page.evaluate(() => ({
    chips: Object.fromEntries(
      [...document.querySelectorAll(".stem-choice[data-stem]")].map((b) => [
        b.dataset.stem,
        b.getAttribute("aria-pressed") === "true",
      ]),
    ),
    all: document.getElementById("stemAllBtn")?.getAttribute("aria-pressed") === "true",
    vocalToggleShown: !document.getElementById("vocalModeToggle")?.classList.contains("hidden"),
  }));

// Puts a selection in the store the way an earlier session would have left it,
// before any of the page's own modules run.
async function openWithStoredSelection(page, stems) {
  await seedLibrary(page);
  await stubExportEndpoints(page);
  await stubUpdateCheck(page);
  await page.addInitScript(
    ([key, value]) => localStorage.setItem(key, JSON.stringify(value)),
    [SEL_KEY, stems],
  );
  await page.goto("/", { waitUntil: "domcontentloaded" });
  // The selection is restored asynchronously, so the row is only settled once
  // the chips show the stored value rather than the all-stems default.
  await expect
    .poll(async () => (await row(page)).chips.other)
    .toBe(stems.includes("other"));
}

test.describe("extract row", () => {
  test("All is dark when the restored selection is only some stems", async ({ page }) => {
    await openWithStoredSelection(page, ["vocals", "drums"]);

    const state = await row(page);
    expect(state.chips).toEqual({
      vocals: true, drums: true, bass: false, guitar: false, piano: false, other: false,
    });
    // The whole point: two of six, so All is not the state of this row.
    expect(state.all).toBe(false);
  });

  test("All is lit only when every stem is selected", async ({ page }) => {
    await openWithStoredSelection(page, ["vocals", "drums", "bass", "guitar", "piano", "other"]);
    expect((await row(page)).all).toBe(true);
  });

  test("Lead + Backing is hidden when the restored selection has no vocals", async ({ page }) => {
    // Same staleness, other control: it used to stay on screen offering a
    // choice about vocals that were not being extracted.
    await openWithStoredSelection(page, ["drums", "bass"]);

    const state = await row(page);
    expect(state.chips.vocals).toBe(false);
    expect(state.vocalToggleShown).toBe(false);
    expect(state.all).toBe(false);
  });

  test("pressing All still fills and empties the row", async ({ page }) => {
    await openWithStoredSelection(page, ["vocals", "drums"]);

    await page.locator("#stemAllBtn").click();
    let state = await row(page);
    expect(state.all).toBe(true);
    expect(Object.values(state.chips).every(Boolean)).toBe(true);

    await page.locator("#stemAllBtn").click();
    state = await row(page);
    expect(state.all).toBe(false);
    expect(Object.values(state.chips).some(Boolean)).toBe(false);
  });
});
