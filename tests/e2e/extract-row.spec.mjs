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
    submitDisabled: !!document.getElementById("submit")?.disabled,
    vocalToggleShown: !!document.getElementById("vocalModeToggle")?.offsetParent,
    vocalModePressed: [...document.querySelectorAll(".vocal-mode-btn")]
      .filter((b) => b.getAttribute("aria-pressed") === "true")
      .map((b) => b.dataset.mode),
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

  test("with no vocals selected, neither mode is lit", async ({ page }) => {
    // The pair carries three states, not two, and this is the third. Both dark
    // is what "vocals are not being extracted" looks like.
    await openWithStoredSelection(page, ["drums", "bass"]);

    const state = await row(page);
    expect(state.chips.vocals).toBe(false);
    expect(state.vocalToggleShown).toBe(true);
    expect(state.vocalModePressed).toEqual([]);
    expect(state.all).toBe(false);
  });

  test("pressing a mode switches the vocals on", async ({ page }) => {
    await openWithStoredSelection(page, ["drums", "bass"]);
    expect((await row(page)).chips.vocals).toBe(false);

    await page.locator('.vocal-mode-btn[data-mode="split"]').click();

    const state = await row(page);
    expect(state.chips.vocals).toBe(true);
    expect(state.vocalModePressed).toEqual(["split"]);
    // And it takes nothing else with it.
    expect(state.chips.drums).toBe(true);
    expect(state.chips.bass).toBe(true);
    expect(state.chips.piano).toBe(false);
  });

  test("pressing the mode that is already on switches the vocals off", async ({ page }) => {
    await openWithStoredSelection(page, ["vocals", "drums"]);
    expect((await row(page)).vocalModePressed).toEqual(["all"]);

    await page.locator('.vocal-mode-btn[data-mode="all"]').click();

    const state = await row(page);
    expect(state.chips.vocals).toBe(false);
    expect(state.vocalModePressed).toEqual([]);
    expect(state.chips.drums).toBe(true);
  });

  test("switching between the two modes keeps the vocals on", async ({ page }) => {
    await openWithStoredSelection(page, ["vocals", "drums"]);

    await page.locator('.vocal-mode-btn[data-mode="split"]').click();
    let state = await row(page);
    expect(state.chips.vocals).toBe(true);
    expect(state.vocalModePressed).toEqual(["split"]);

    await page.locator('.vocal-mode-btn[data-mode="all"]').click();
    state = await row(page);
    expect(state.chips.vocals).toBe(true);
    expect(state.vocalModePressed).toEqual(["all"]);
  });

  test("the mode is remembered while the vocals are off", async ({ page }) => {
    await openWithStoredSelection(page, ["vocals", "drums"]);
    await page.locator('.vocal-mode-btn[data-mode="split"]').click();

    // Off, then on again, through the same button.
    await page.locator('.vocal-mode-btn[data-mode="split"]').click();
    expect((await row(page)).chips.vocals).toBe(false);

    await page.locator('.vocal-mode-btn[data-mode="split"]').click();
    const state = await row(page);
    expect(state.chips.vocals).toBe(true);
    expect(state.vocalModePressed).toEqual(["split"]);
  });

  test("the Vocals chip switches them on as Combined", async ({ page }) => {
    // The short way in. A press here says nothing about how the vocals should
    // come out, so it takes the mode that means "leave them alone".
    await openWithStoredSelection(page, ["drums", "bass"]);
    expect((await row(page)).vocalModePressed).toEqual([]);

    await page.locator('.stem-choice[data-stem="vocals"]').click();

    const state = await row(page);
    expect(state.chips.vocals).toBe(true);
    expect(state.vocalModePressed).toEqual(["all"]);
  });

  test("the Vocals chip switches them off again", async ({ page }) => {
    await openWithStoredSelection(page, ["vocals", "drums"]);

    await page.locator('.stem-choice[data-stem="vocals"]').click();

    const state = await row(page);
    expect(state.chips.vocals).toBe(false);
    expect(state.vocalModePressed).toEqual([]);
    expect(state.chips.drums).toBe(true);
  });

  test("Combined is what the chip means, even after Lead + Backing", async ({ page }) => {
    // The mode buttons remember their choice across an off and on of their own.
    // The chip does not restore it, because pressing the chip is not asking for
    // it: it is the press that does not name a mode.
    await openWithStoredSelection(page, ["vocals", "drums"]);
    await page.locator('.vocal-mode-btn[data-mode="split"]').click();
    await page.locator('.vocal-mode-btn[data-mode="split"]').click();
    expect((await row(page)).chips.vocals).toBe(false);

    await page.locator('.stem-choice[data-stem="vocals"]').click();
    expect((await row(page)).vocalModePressed).toEqual(["all"]);
  });

  test("the last remaining stem can still be switched off", async ({ page }) => {
    // Narrowing to one stem and pressing it again used to turn all six back
    // on: the set emptied, and an "if nothing is selected, select everything"
    // guard caught it. The press asked for less and got the most there is.
    await openWithStoredSelection(page, ["vocals"]);

    await page.locator('.vocal-mode-btn[data-mode="all"]').click();

    const state = await row(page);
    expect(state.chips.vocals).toBe(false);
    expect(Object.values(state.chips).filter(Boolean)).toHaveLength(0);
    expect(state.all).toBe(false);
  });

  test("an empty row cannot be submitted", async ({ page }) => {
    // The server reads an empty stems list as every stem, so a submit from
    // here would extract six of them while the row showed none. The row is
    // allowed to be empty; it just cannot be acted on.
    await openWithStoredSelection(page, ["vocals"]);
    expect((await row(page)).submitDisabled).toBe(false);

    await page.locator('.vocal-mode-btn[data-mode="all"]').click();
    expect((await row(page)).submitDisabled).toBe(true);

    await page.locator('.stem-choice[data-stem="drums"]').click();
    expect((await row(page)).submitDisabled).toBe(false);
  });

  test("a chip pressed during a submit does not reopen the button", async ({ page }) => {
    // setSubmitProcessing holds Split shut while a submit is in flight, which
    // for a large upload is the whole upload. The row repaints the same button
    // on every chip press, and used to reopen it, allowing a second submit.
    await openWithStoredSelection(page, ["vocals", "drums"]);

    await page.evaluate(() => {
      const b = document.getElementById("submit");
      b.disabled = true;
      b.classList.add("loading");
    });
    await page.locator('.stem-choice[data-stem="bass"]').click();

    expect((await row(page)).submitDisabled).toBe(true);
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
