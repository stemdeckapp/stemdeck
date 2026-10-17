// Library search by tag, beyond the one case library-tags.spec.mjs pins: a
// tag matches without regard to case and by part of the word, a bare "#" or an
// empty search shows everything, a plain word finds tags too, and a tag on no
// track leaves nothing listed.

import { test, expect } from "@playwright/test";
import {
  JOB_ID,
  SIBLING_JOB_ID,
  TRACK_TITLE,
  SIBLING_TITLE,
  fixtureTrack,
  seedCatalogState,
  stubExportEndpoints,
  stubFavorites,
  stubUpdateCheck,
} from "./helpers.mjs";

test.beforeEach(async ({ page }) => {
  // Both fixture jobs, as seedLibrary has them (see library-tags.spec.mjs for
  // why seeding one is not enough), each with tags of its own.
  await seedCatalogState(page, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: [JOB_ID, SIBLING_JOB_ID], color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks: {
      [JOB_ID]: { ...fixtureTrack(JOB_ID, TRACK_TITLE), tags: ["Post-Rock", "live"] },
      [SIBLING_JOB_ID]: { ...fixtureTrack(SIBLING_JOB_ID, SIBLING_TITLE), tags: ["jazz", "live"] },
    },
  });
  await stubFavorites(page);
  await stubExportEndpoints(page);
  await stubUpdateCheck(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().waitFor({ timeout: 20000 });
});

const listed = (page, id) => page.locator(`#catalogList .cat-item[data-id="${id}"]`);

async function search(page, text) {
  await page.locator("#catalogSearch").fill(text);
}

test("a tag matches whatever its case", async ({ page }) => {
  await search(page, "#POST-ROCK");
  await expect(listed(page, JOB_ID)).not.toHaveCount(0);
  await expect(listed(page, SIBLING_JOB_ID)).toHaveCount(0);
});

test("part of a tag is enough", async ({ page }) => {
  await search(page, "#roc");
  await expect(listed(page, JOB_ID)).not.toHaveCount(0);
  await expect(listed(page, SIBLING_JOB_ID)).toHaveCount(0);
});

test("a tag both tracks carry lists both", async ({ page }) => {
  await search(page, "#live");
  await expect(listed(page, JOB_ID)).not.toHaveCount(0);
  await expect(listed(page, SIBLING_JOB_ID)).not.toHaveCount(0);
});

test("a tag no track carries lists nothing", async ({ page }) => {
  await search(page, "#polka");
  await expect(listed(page, JOB_ID)).toHaveCount(0);
  await expect(listed(page, SIBLING_JOB_ID)).toHaveCount(0);
});

test("a # on its own, or clearing the search, shows every track again", async ({ page }) => {
  await search(page, "#jazz");
  await expect(listed(page, JOB_ID)).toHaveCount(0);
  await search(page, "#");
  await expect(listed(page, JOB_ID)).not.toHaveCount(0);
  await expect(listed(page, SIBLING_JOB_ID)).not.toHaveCount(0);
  await search(page, "#jazz");
  await expect(listed(page, JOB_ID)).toHaveCount(0);
  await search(page, "");
  await expect(listed(page, JOB_ID)).not.toHaveCount(0);
});

test("a plain word finds a tag too, but a #tag does not match titles", async ({ page }) => {
  await search(page, "jazz");
  await expect(listed(page, SIBLING_JOB_ID)).not.toHaveCount(0);
  await expect(listed(page, JOB_ID)).toHaveCount(0);

  // "again" is only in the sibling's title, not in any tag.
  await search(page, "#again");
  await expect(listed(page, SIBLING_JOB_ID)).toHaveCount(0);
  await search(page, "again");
  await expect(listed(page, SIBLING_JOB_ID)).not.toHaveCount(0);
});
