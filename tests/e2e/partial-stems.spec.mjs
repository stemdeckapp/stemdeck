// A song with only some stems shows only those stems.
//
// The fixture job has four stems (tests/e2e/seed.py: vocals, drums, bass,
// other), so guitar and piano were never extracted. They used to keep a
// greyed, empty mixer row and waveform lane each, which spread four stems
// over six lanes. Now they have no row at all, and the four sit together at
// the top, still aligned row for row with their waveforms.
//
// The empty studio, with no song open, keeps its six placeholder rows.
import { test, expect } from "@playwright/test";
import { openStudio, seedLibrary, stubUpdateCheck, waitForClickTrack } from "./helpers.mjs";

const PRESENT = ["vocals", "drums", "bass", "other"];
const ABSENT = ["guitar", "piano"];

async function lanes(page) {
  return page.evaluate(() => {
    const visible = (sel) => [...document.querySelectorAll(sel)]
      .filter((e) => !e.classList.contains("hidden") && e.getBoundingClientRect().height > 0);
    const mix = visible(".mixer-column .lane-header[data-stem]")
      .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
    const wave = visible(".stem-waveform-row[data-stem]")
      .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
    const box = (e) => { const r = e.getBoundingClientRect(); return { top: Math.round(r.top), h: Math.round(r.height) }; };
    return {
      mixNames: mix.map((e) => e.dataset.stem),
      waveNames: wave.map((e) => e.dataset.stem),
      mixBoxes: mix.map(box),
      waveBoxes: wave.map(box),
      stemList: visible(".stem-list span[data-stem]").map((e) => e.dataset.stem),
      count: getComputedStyle(document.querySelector(".app")).getPropertyValue("--visible-track-count").trim(),
    };
  });
}

test.describe("a song with only some stems", () => {
  test("shows only the extracted stems, together at the top and aligned with their lanes", async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page, { revealOptions: false });
    // The waveform rows arrive with the peaks.
    await expect.poll(async () => (await lanes(page)).waveNames.length, { timeout: 10000 }).toBe(PRESENT.length);

    const g = await lanes(page);
    expect(g.mixNames).toEqual(PRESENT);
    expect(g.waveNames).toEqual(PRESENT);
    expect(g.count).toBe(String(PRESENT.length));
    for (const name of ABSENT) {
      await expect(page.locator(`.mixer-column .lane-header[data-stem="${name}"]`)).toBeHidden();
      await expect(page.locator(`.stem-waveform-row[data-stem="${name}"]`)).toHaveCount(0);
      expect(g.stemList).not.toContain(name);
    }
    // Row for row: each mixer row and its waveform lane start on the same line.
    for (let i = 0; i < PRESENT.length; i++) {
      expect(Math.abs(g.mixBoxes[i].top - g.waveBoxes[i].top)).toBeLessThanOrEqual(1);
    }

    // The engine and the mixer know only these lanes.
    const names = await page.evaluate(async () => {
      const st = await import("/js/state.js");
      return Object.entries(st.trackIndex).sort((a, b) => a[1] - b[1]).map(([n]) => n);
    });
    expect(names).toEqual(PRESENT);

    // Solo and export still work on what is there.
    await page.locator('.mixer-column .lane-header[data-stem="other"] .solo').click();
    expect(await page.evaluate(async () => (await import("/js/state.js")).mixerState.other.soloed)).toBe(true);
    await page.locator('.mixer-column .lane-header[data-stem="other"] .solo').click();
    await page.locator("#t-export-btn").click();
    await page.locator("#t-export-mix").click();
    await page.evaluate(() => window.__e2e.choosePath());
    await expect.poll(() => page.evaluate(() => window.__e2e.callsFor("download_to_path").length)).toBe(1);
    const url = await page.evaluate(() => window.__e2e.callsFor("download_to_path")[0].args.url);
    expect(new URL(url).searchParams.get("stems").split(",")).toEqual(PRESENT);
  });

  test("stays aligned at other window heights", async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page, { revealOptions: false });
    await expect.poll(async () => (await lanes(page)).waveNames.length, { timeout: 10000 }).toBe(PRESENT.length);
    for (const height of [720, 1013, 1300]) {
      await page.setViewportSize({ width: 1600, height });
      await page.waitForTimeout(450);
      const g = await lanes(page);
      const drift = g.mixBoxes.map((b, i) => Math.abs(b.top - (g.waveBoxes[i]?.top ?? b.top)));
      expect(Math.max(...drift), `height ${height}`).toBeLessThanOrEqual(1);
    }
  });

  test("the empty studio keeps its six placeholder rows", async ({ page }) => {
    await seedLibrary(page);
    await stubUpdateCheck(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await expect(page.locator(".app.no-track")).toHaveCount(1);
    await expect.poll(async () => (await lanes(page)).mixNames)
      .toEqual(["vocals", "drums", "bass", "guitar", "piano", "other"]);
  });
});
