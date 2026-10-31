// A track can be favourited at any window width (#724).
//
// The only heart used to be the Now Playing card's, and daw.css hid that whole
// card below 1460 px, so library rows got a heart of their own. The card stays
// in a compact form at narrow widths since #735, and the maintainer asked for
// the rows' hearts to go: the Now Playing heart is the one way to favourite, and
// a row shows a favourite only by its mark (.is-fav).

import { test, expect } from "@playwright/test";
import { JOB_ID, openStudio, readCatalogState } from "./helpers.mjs";

const row = (page, id) => page.locator(`#catalogList .cat-item[data-id="${id}"]`).first();
const favoriteIn = async (page, id) => Boolean((await readCatalogState(page))?.tracks?.[id]?.favorite);

for (const [name, viewport] of [
  ["a narrow window", { width: 1280, height: 800 }],
  ["a wide window", { width: 1600, height: 900 }],
]) {
  test.describe(`favourites at ${name}`, () => {
    test.use({ viewport });

    test("the Now Playing heart favourites the open track and its row shows it", async ({ page }) => {
      await openStudio(page);
      const npHeart = page.locator("#fav-btn");
      await expect(npHeart).toBeVisible();
      await expect(npHeart).toHaveAttribute("aria-pressed", "false");

      await npHeart.click();

      await expect(npHeart).toHaveAttribute("aria-pressed", "true");
      expect(await favoriteIn(page, JOB_ID)).toBe(true);
      await expect(row(page, JOB_ID)).toHaveClass(/\bis-fav\b/);
    });

    test("library rows carry no heart", async ({ page }) => {
      await openStudio(page);
      await row(page, JOB_ID).hover();
      await expect(page.locator("#catalogList .cat-fav")).toHaveCount(0);
    });
  });
}

test.describe("the Favorites view", () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test("the Now Playing heart takes the open track back out", async ({ page }) => {
    await openStudio(page);
    const npHeart = page.locator("#fav-btn");
    await npHeart.click();

    await page.locator(".rail-favorites").click();
    await expect(row(page, JOB_ID)).toBeVisible();
    await npHeart.click();

    await expect(row(page, JOB_ID)).toHaveCount(0);
    expect(await favoriteIn(page, JOB_ID)).toBe(false);
  });
});
