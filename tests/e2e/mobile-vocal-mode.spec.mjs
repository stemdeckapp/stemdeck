// The phone's Extract screen answers the vocals question the same way the
// studio does.
//
// Vocals is the one stem with something to say about how it comes out, so the
// Combined / Lead + Backing pair carries three states: one lit, the other lit,
// or neither, which is what "no vocals" looks like. Either button switches the
// vocals on.
//
// This screen had the older arrangement, where the pair was a consequence of
// the Vocals chip rather than a control: it was not rendered at all until the
// chip was on, pressing a mode did not select vocals, and pressing the lit one
// did not switch them off. Two UIs disagreeing about what a control does is
// worse than either behaviour on its own, and nothing here would have caught
// it, since this screen is only covered by the transpose file.
import { test, expect } from "@playwright/test";

const modeBtn = (page, mode) => page.locator(`[data-action="vocalmode"][data-mode="${mode}"]`);
const chip = (page, id) => page.locator(`[data-action="chip"][data-id="${id}"]`);
const cta = (page) => page.locator('[data-action="split"]');

const on = (locator) => locator.evaluate((el) => el.classList.contains("on"));

async function openExtract(page) {
  await page.goto("/?ui=mobile", { waitUntil: "domcontentloaded" });
  await page.locator('[data-action="tab"][data-tab="extract"]').first().click();
  await modeBtn(page, "all").first().waitFor({ timeout: 20000 });
}

test.describe("phone: the vocal mode", () => {
  test("the pair is on screen before anything is pressed", async ({ page }) => {
    // It used to render only once the Vocals chip was on, so the way to say
    // how you wanted the vocals split was hidden until you had asked for them.
    await openExtract(page);

    await expect(modeBtn(page, "all")).toBeVisible();
    await expect(modeBtn(page, "split")).toBeVisible();
    expect(await on(modeBtn(page, "all"))).toBe(true);
    expect(await on(modeBtn(page, "split"))).toBe(false);
  });

  test("pressing the mode already in force switches the vocals off", async ({ page }) => {
    await openExtract(page);
    expect(await on(chip(page, "vocals"))).toBe(true);

    await modeBtn(page, "all").click();

    expect(await on(chip(page, "vocals"))).toBe(false);
    // Neither lit is the third state, not a missing one.
    expect(await on(modeBtn(page, "all"))).toBe(false);
    expect(await on(modeBtn(page, "split"))).toBe(false);
    await expect(modeBtn(page, "all")).toBeVisible();
    // And it takes nothing else with it.
    expect(await on(chip(page, "drums"))).toBe(true);
  });

  test("pressing a mode from cold switches the vocals on", async ({ page }) => {
    await openExtract(page);
    await modeBtn(page, "all").click();
    expect(await on(chip(page, "vocals"))).toBe(false);

    await modeBtn(page, "split").click();

    expect(await on(chip(page, "vocals"))).toBe(true);
    expect(await on(modeBtn(page, "split"))).toBe(true);
    expect(await on(modeBtn(page, "all"))).toBe(false);
  });

  test("switching between the two keeps the vocals on", async ({ page }) => {
    await openExtract(page);

    await modeBtn(page, "split").click();
    expect(await on(chip(page, "vocals"))).toBe(true);
    expect(await on(modeBtn(page, "split"))).toBe(true);

    await modeBtn(page, "all").click();
    expect(await on(chip(page, "vocals"))).toBe(true);
    expect(await on(modeBtn(page, "all"))).toBe(true);
  });

  test("the Vocals chip switches them on as Combined", async ({ page }) => {
    await openExtract(page);
    await modeBtn(page, "split").click();
    await chip(page, "vocals").click();
    expect(await on(chip(page, "vocals"))).toBe(false);

    await chip(page, "vocals").click();

    // A press on the chip does not name a mode, so it means Combined.
    expect(await on(chip(page, "vocals"))).toBe(true);
    expect(await on(modeBtn(page, "all"))).toBe(true);
  });

  test("an empty screen cannot be submitted", async ({ page }) => {
    // The server reads an empty stems list as every stem, so this would have
    // extracted six stems the screen was showing as off.
    await openExtract(page);
    await expect(cta(page)).toBeEnabled();

    for (const id of ["drums", "bass", "guitar", "piano", "other"]) {
      if (await on(chip(page, id))) await chip(page, id).click();
    }
    await modeBtn(page, "all").click();

    await expect(cta(page)).toBeDisabled();
  });
});
