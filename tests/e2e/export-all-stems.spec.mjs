// Export mix, All stems: the zip holds every stem the song has, in the chosen
// format, whatever the lanes show. The lanes are mode-shaped (the rest is
// folded into Original), so building the zip from them dropped stems: after a
// Lead + Backing split the zip held one or two files.

import { test, expect } from "@playwright/test";
import { openStudio, exportUi, resetVersions } from "./helpers.mjs";

// The fixture song's stems (seed.py): it has no lead/backing split.
const STEMS = ["bass", "drums", "other", "vocals"];

const lanes = (page) =>
  page.evaluate(async () =>
    Object.keys((await import("/js/state.js")).trackIndex)
      .filter((n) => !/^t[kr]_/.test(n))
      .sort()
      .join(","),
  );

// Entry names of a zip, read from its central directory.
function zipNames(buf) {
  let eocd = buf.length - 22;
  while (eocd >= 0 && buf.readUInt32LE(eocd) !== 0x06054b50) eocd--;
  const count = buf.readUInt16LE(eocd + 10);
  let at = buf.readUInt32LE(eocd + 16);
  const names = [];
  for (let i = 0; i < count; i++) {
    const n = buf.readUInt16LE(at + 28);
    names.push(buf.toString("utf8", at + 46, at + 46 + n));
    at += 46 + n + buf.readUInt16LE(at + 30) + buf.readUInt16LE(at + 32);
  }
  return names.sort();
}

async function collapseToVocals(page) {
  await page.locator("#laneStemsBtn").click();
  for (const name of ["drums", "bass", "other"]) {
    const box = page.locator(`#laneStemsPop input[data-stem="${name}"]`);
    if (await box.isChecked()) await box.click();
  }
  await page.locator("#laneStemsPop .stems-pop-apply").click();
  await expect
    .poll(() => lanes(page), { timeout: 15000 })
    .toBe("original,vocals");
}

test.describe("All stems zip", () => {
  // Extract on the open song makes a version of it on the server (#804).
  test.afterEach(async ({ request }) => {
    await resetVersions(request);
  });

  for (const format of ["wav", "mp3"]) {
    test(`holds every stem as ${format} with the lanes down to Vocals`, async ({
      page,
    }) => {
      await page.addInitScript(() => {
        if (!sessionStorage.getItem("all-stems-spec")) {
          sessionStorage.setItem("all-stems-spec", "1");
          localStorage.setItem("stemdeck.mode", "practice");
        }
      });
      await openStudio(page);
      await collapseToVocals(page);

      const ui = exportUi(page);
      await ui.open();
      await ui.fmt(format).click();
      const download = page.waitForEvent("download");
      await ui.stems.click();
      // The request the page made, then the zip the real backend builds for it.
      const url = new URL((await download).url());
      const body = await (await page.request.get(url.href)).body();

      expect(url.searchParams.get("format")).toBe(format);
      // No stems list: the server answers with all of them.
      expect(url.searchParams.has("stems")).toBe(false);
      expect(
        zipNames(body).map((n) => n.replace(/^.*_(?=[a-z_]+\.\w+$)/, "")),
      ).toEqual(STEMS.map((s) => `${s}.${format}`));
    });
  }
});
