// Stem choice per mode (static/js/modeStems.js, stemsControl.js): each mode
// keeps its own lanes for a song. Written while e2e runs are paused
// (2026-10-06); not run yet. The 6 second fixture has four stems (vocals,
// drums, bass, other) and no lead/backing split, so the split itself is not
// covered here (scratchpad ms3.mjs ran it against a real song).
import { test, expect } from "@playwright/test";
import { openStudio, JOB_ID, chooseMode, resetVersions } from "./helpers.mjs";

const lanes = (page) =>
  page.evaluate(async () => Object.keys((await import("/js/state.js")).trackIndex).filter((n) => !/^t[kr]_/.test(n)).sort().join(","));
const setMode = (page, m) => page.evaluate(async (x) => (await import("/js/mode.js")).setMode(x), m);

async function chooseStems(page, want) {
  await page.locator("#laneStemsBtn").click();
  for (const name of ["vocals", "drums", "bass", "other"]) {
    const box = page.locator(`#laneStemsPop input[data-stem="${name}"]`);
    if ((await box.isChecked()) !== want.includes(name)) await box.click();
  }
  await page.locator("#laneStemsPop .stems-pop-apply").click();
}

test.describe("stem choice per mode", () => {
  // Extract on the open song makes a version of it on the server (#804),
  // which every spec shares.
  test.afterEach(async ({ request }) => { await resetVersions(request); });

  test("Practice and Advanced keep different lanes for one song", async ({ page }) => {
    await page.addInitScript(() => {
      if (!sessionStorage.getItem("mode-stems-spec")) {
        sessionStorage.setItem("mode-stems-spec", "1");
        localStorage.setItem("stemdeck.mode", "practice");
      }
    });
    await openStudio(page);
    await expect.poll(() => lanes(page)).toBe("bass,drums,other,vocals");
    await expect(page.locator("#laneStemsBtn")).toBeVisible();

    await chooseStems(page, ["vocals", "drums"]);
    await expect.poll(() => lanes(page), { timeout: 15000 }).toBe("drums,original,vocals");
    // Original is the rest, built once on the server: never the drums again.
    const href = await page.locator('.lane-header[data-stem="original"] .lane-dl').getAttribute("href");
    expect(href).toContain("/rest/bass-other.wav");

    await setMode(page, "daw");
    await expect.poll(() => lanes(page), { timeout: 15000 }).toBe("bass,drums,other,vocals");
    await setMode(page, "practice");
    await expect.poll(() => lanes(page), { timeout: 15000 }).toBe("drums,original,vocals");

    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    await expect.poll(() => lanes(page), { timeout: 15000 }).toBe("drums,original,vocals");
  });

  test("the export names the mode's lanes", async ({ page }) => {
    await openStudio(page);
    await chooseStems(page, ["vocals"]);
    await expect.poll(() => lanes(page), { timeout: 15000 }).toBe("original,vocals");
    const query = await page.evaluate(async () => (await import("/js/masterEngine.js")).currentMixSource()?.query || "");
    expect(new URLSearchParams(query).get("lanes")).toBe("vocals");
  });

  test("Karaoke shows the voice toggle and keeps the voice off", async ({ page }) => {
    await openStudio(page);
    await chooseMode(page, "karaoke");
    await expect(page.locator("#karaoke .kk-voice")).toBeVisible();
    await expect(page.locator('#karaoke .kk-voice-btn[data-form="combined"]')).toHaveAttribute("aria-checked", "true");
    // Karaoke's lanes are the voice only; the instruments are in Original.
    await expect.poll(() => lanes(page), { timeout: 15000 }).toBe("original,vocals");
    const muted = await page.evaluate(async () => (await import("/js/state.js")).mixerState.vocals.muted);
    expect(muted).toBe(true);
  });

  test("opening a song leaves the Extract chips alone", async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem("stemdeck:selected-stems", JSON.stringify(["drums"])));
    await openStudio(page);
    const pressed = await page.evaluate(() =>
      [...document.querySelectorAll(".stem-choice[data-stem]")].filter((b) => b.getAttribute("aria-pressed") === "true").map((b) => b.dataset.stem),
    );
    expect(pressed).toEqual(["drums"]);
  });

  test("Extract with other chips changes the lanes as a version of the song, no new job", async ({ page }) => {
    await openStudio(page);
    const posts = [];
    await page.route("**/api/jobs/**", (route) => {
      if (route.request().method() === "POST") posts.push(route.request().url());
      return route.fallback();
    });
    await page.locator("#stemAllBtn").click(); // all off
    await page.locator('.stem-choice[data-stem="vocals"]').click();
    await page.locator("#submit").click();
    await expect.poll(() => lanes(page), { timeout: 15000 }).toBe("original,vocals");
    expect(posts.filter((u) => /\/resplit$|\/api\/jobs$/.test(u))).toEqual([]);
  });

  test("Apply with no stem ticked says so and changes nothing (#793)", async ({ page }) => {
    await openStudio(page);
    await expect.poll(() => lanes(page)).toBe("bass,drums,other,vocals");
    await chooseStems(page, []);
    await expect(page.locator("#laneStemsPop .stems-pop-status")).toHaveText(
      "Tick at least one stem to give it its own lane. Nothing was changed.",
    );
    const stored = await page.evaluate(() => Object.keys(localStorage).filter((k) => k.startsWith("stemdeck:stems:")));
    expect(stored).toEqual([]);
    expect(await lanes(page)).toBe("bass,drums,other,vocals");
  });

  test("Extract that is refused says why, never that the lanes changed (#793)", async ({ page }) => {
    await openStudio(page);
    // A refusal the Stems control did not know before: the song is not the
    // open one any more by the time the change would be saved.
    await page.evaluate(async () => (await import("/js/modeStems.js")).setLayoutBusyCheck(() => "noSong"));
    await page.locator("#stemAllBtn").click(); // all off
    await page.locator('.stem-choice[data-stem="vocals"]').click();
    await page.locator("#submit").click();
    await expect(page.locator("#trackNote")).toHaveText("Open a song first.");
    expect(await lanes(page)).toBe("bass,drums,other,vocals");
  });
});
