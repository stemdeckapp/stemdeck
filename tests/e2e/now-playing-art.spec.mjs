// The now-playing square shows the open track's own picture, or its format
// icon when it has none. Switching tracks from the library goes straight to
// wireUpAudio without the teardown that clears the square, and wireUpAudio
// only ever set the thumbnail, never cleared it. So opening an upload after a
// YouTube track left the YouTube cover over the upload's WAV icon (#699).
import { test, expect } from "@playwright/test";
import {
  JOB_ID,
  SIBLING_JOB_ID,
  fixtureTrack,
  seedCatalogState,
  stubExportEndpoints,
  stubUpdateCheck,
} from "./helpers.mjs";

// A 1x1 PNG. data: is in the page's img-src, so it loads with no network.
const PIXEL =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==";

// Both fixture jobs, seeded together: seeding one lets the startup sync adopt
// the other, and the catalog's dedup (#542) would fold them into one row.
function library() {
  const tracks = {
    [JOB_ID]: { ...fixtureTrack(JOB_ID, "With Cover"), thumb: PIXEL },
    [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, "No Cover"),
  };
  return {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: Object.keys(tracks), color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks,
  };
}

const thumbState = (page) =>
  page.locator("#np-thumb").evaluate((img) => ({
    src: img.getAttribute("src") || "",
    loaded: img.classList.contains("loaded"),
  }));

// The title shown is the server job's, whatever the seed called it.
const TITLES = {
  [JOB_ID]: /^E2E Fixture Track$/,
  [SIBLING_JOB_ID]: /^E2E Fixture Track \(again\)$/,
};

async function openTrack(page, id) {
  await page.locator(`.cat-item[data-id="${id}"]`).first().click();
  await expect(page.locator("#title")).toHaveText(TITLES[id], { timeout: 15000 });
}

test.describe("now-playing art", () => {
  test("a track with no thumbnail does not keep the previous track's", async ({ page }) => {
    await seedCatalogState(page, library());
    await stubExportEndpoints(page);
    await stubUpdateCheck(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.locator(".cat-item").first().waitFor({ timeout: 20000 });

    await openTrack(page, JOB_ID);
    await expect.poll(() => thumbState(page)).toEqual({ src: PIXEL, loaded: true });

    await openTrack(page, SIBLING_JOB_ID);
    await expect.poll(() => thumbState(page)).toEqual({ src: "", loaded: false });

    // And back again: clearing it must not stop the next one from showing.
    await openTrack(page, JOB_ID);
    await expect.poll(() => thumbState(page)).toEqual({ src: PIXEL, loaded: true });
  });
});
