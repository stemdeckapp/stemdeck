// The key is one label on one card, and it fits (#736).
//
// It used to be "D maj" on the Key card, "Major" under it and "Major" again on
// a Scale card of its own, while the Key card was too narrow for anything
// longer. The longest label analysis can produce is a sharp harmonic minor, and
// German spells it longest, so that is what this measures.

import { test, expect } from "@playwright/test";
import {
  JOB_ID, SIBLING_JOB_ID, TRACK_TITLE, SIBLING_TITLE,
  fixtureTrack, seedCatalogState, stubExportEndpoints, stubUpdateCheck,
} from "./helpers.mjs";

async function openWithKey(page, { language }) {
  await page.addInitScript((code) => window.localStorage.setItem("stemdeck.language", JSON.stringify(code)), language);
  await seedCatalogState(page, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: [JOB_ID, SIBLING_JOB_ID], color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks: {
      [JOB_ID]: { ...fixtureTrack(JOB_ID, TRACK_TITLE), key: "F# min", scale: "Harmonic Minor", keyConfidence: 72 },
      [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, SIBLING_TITLE),
    },
  });
  // The server's own record wins over the store once the page syncs, and the
  // fixture job says "C maj", so the answer is rewritten on the way in.
  await page.route(`**/api/jobs**`, async (route) => {
    const response = await route.fetch();
    let body = await response.text();
    if ((response.headers()["content-type"] || "").includes("json")) {
      body = body.replaceAll('"key":"C maj"', '"key":"F# min"')
        .replaceAll('"key": "C maj"', '"key": "F# min"')
        .replaceAll('"scale":"Major"', '"scale":"Harmonic Minor"')
        .replaceAll('"scale": "Major"', '"scale": "Harmonic Minor"');
    }
    await route.fulfill({ response, body });
  });
  await stubExportEndpoints(page);
  await stubUpdateCheck(page, { available: false });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
}

// Background syncs are still in flight through the rewriter when a test ends.
test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

const EXPECTED = { en: "F# harmonic minor", de: "F# harmonisches Moll", fr: "F# mineur harmonique" };

for (const [language, label] of Object.entries(EXPECTED)) {
  for (const width of [1366, 950]) {
    test(`${language} at ${width}px: the key is one label and it fits`, async ({ page }) => {
      await page.setViewportSize({ width, height: 800 });
      await openWithKey(page, { language });
      const key = page.locator("#summary-key");
      await expect(key).toHaveText(label);
      await expect(page.locator('[data-meta="scale"]')).toHaveCount(0);
      const clipped = await key.evaluate((el) => el.scrollWidth > el.clientWidth);
      expect(clipped).toBe(false);
    });
  }
}
