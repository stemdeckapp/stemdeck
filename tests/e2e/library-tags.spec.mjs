// The library has no Tags section under the collections (the maintainer asked
// for it gone). Tags still filter the library when typed into search as #tag.
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
      [JOB_ID]: { ...fixtureTrack(JOB_ID, TRACK_TITLE), tags: ["rock", "shared"] },
      [SIBLING_JOB_ID]: { ...fixtureTrack(SIBLING_JOB_ID, SIBLING_TITLE), tags: ["jazz", "shared"] },
    },
  });
  await stubExportEndpoints(page);
  await stubUpdateCheck(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().waitFor({ timeout: 20000 });
});

test("no Tags section, with or without a track open", async ({ page }) => {
  await expect(page.locator(".lib-tags-section")).toHaveCount(0);
  await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
  await expect(page.locator(`.cat-item[data-id="${JOB_ID}"].active`).first()).toBeVisible();
  await expect(page.locator(".lib-tags-section")).toHaveCount(0);
  await expect(page.locator(".lib-tag-chip")).toHaveCount(0);
});

test("a #tag typed into search still filters the library", async ({ page }) => {
  await page.locator("#catalogSearch").fill("#rock");
  await expect(page.locator(`.cat-item[data-id="${JOB_ID}"]`)).not.toHaveCount(0);
  await expect(page.locator(`.cat-item[data-id="${SIBLING_JOB_ID}"]`)).toHaveCount(0);
});
