// The library's Tags section shows the open track's tags, not every tag in the
// library. With every tag from every track it was a wall of chips with nothing
// to say which song they came from. A chip still filters the library by its
// #tag, and its count is still how many tracks that filter finds.
import { test, expect } from "@playwright/test";
import {
  JOB_ID,
  SIBLING_JOB_ID,
  TRACK_TITLE,
  SIBLING_TITLE,
  fixtureTrack,
  seedCatalogState,
  stubExportEndpoints,
  stubUpdateCheck,
} from "./helpers.mjs";

const FIRST_TAGS = ["rock", "live", "shared"];
const SIBLING_TAGS = ["jazz", "shared"];

const chips = (page) => page.locator(".lib-tags-section .lib-tag-chip");
const chipTags = (page) => chips(page).evaluateAll((els) => els.map((el) => el.dataset.tag));

test.beforeEach(async ({ page }) => {
  // Both fixture jobs, as seedLibrary has them, each with tags of its own.
  // Seeding only one would not work: the startup sync adopts the other from
  // the server, and as they share a source the catalog's dedup (#542)
  // replaces the first with it.
  await seedCatalogState(page, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: [JOB_ID, SIBLING_JOB_ID], color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks: {
      [JOB_ID]: { ...fixtureTrack(JOB_ID, TRACK_TITLE), tags: FIRST_TAGS },
      [SIBLING_JOB_ID]: { ...fixtureTrack(SIBLING_JOB_ID, SIBLING_TITLE), tags: SIBLING_TAGS },
    },
  });
  await stubExportEndpoints(page);
  await stubUpdateCheck(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().waitFor({ timeout: 20000 });
});

test("no track open, no Tags section", async ({ page }) => {
  await expect(page.locator(".lib-tags-section")).toHaveCount(0);
  await expect(page.locator(".lib-tag-chip")).toHaveCount(0);
});

test("only the open track's tags show, and they follow the open track", async ({ page }) => {
  await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
  await expect.poll(() => chipTags(page), { timeout: 20000 }).toEqual(FIRST_TAGS);
  // A tag on both tracks counts both, since that is what its filter finds.
  await expect(chips(page).nth(2).locator(".lib-tag-count")).toHaveText("2");
  await expect(chips(page).first().locator(".lib-tag-count")).toHaveText("1");

  await page.locator(`.cat-item[data-id="${SIBLING_JOB_ID}"]`).first().click();
  await expect.poll(() => chipTags(page), { timeout: 20000 }).toEqual(SIBLING_TAGS);
});

test("a chip still filters the library by its tag", async ({ page }) => {
  await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
  await expect.poll(() => chipTags(page), { timeout: 20000 }).toEqual(FIRST_TAGS);

  await chips(page).first().click();
  await expect(page.locator("#catalogSearch")).toHaveValue("#rock");
  await expect(page.locator(`.cat-item[data-id="${JOB_ID}"]`)).not.toHaveCount(0);
  await expect(page.locator(`.cat-item[data-id="${SIBLING_JOB_ID}"]`)).toHaveCount(0);
  await expect(chips(page).first()).toHaveClass(/active/);

  // Clicking the active chip again clears the filter.
  await chips(page).first().click();
  await expect(page.locator("#catalogSearch")).toHaveValue("");
  await expect(page.locator(`.cat-item[data-id="${SIBLING_JOB_ID}"]`)).not.toHaveCount(0);
});
