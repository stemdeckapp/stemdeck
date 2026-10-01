// Favourites are kept on the server, so the phone and the studio agree (#734).
//
// They lived only in the studio's own store, so the phone had no heart to
// press and its Favorites chip listed every track. The server is stubbed here
// (stubFavorites) so no spec leaves a favourite behind on the shared backend;
// the endpoint itself is covered by tests/test_jobs_favorite.py.

import { test, expect } from "@playwright/test";
import { JOB_ID, SIBLING_JOB_ID, openStudio, readCatalogState, stubFavorites } from "./helpers.mjs";

const row = (page, id) => page.locator(`#catalogList .cat-item[data-id="${id}"]`).first();

test.describe("studio", () => {
  test.use({ viewport: { width: 1600, height: 900 } });

  test("a heart pressed in the studio is saved on the server", async ({ page }) => {
    const { favoriteWrites } = await openStudio(page);
    await row(page, SIBLING_JOB_ID).hover();
    await row(page, SIBLING_JOB_ID).locator(".cat-fav").click();
    await expect.poll(() => favoriteWrites).toContainEqual({ id: SIBLING_JOB_ID, favorite: true });
  });

  test("a favourite set on the phone shows in the studio", async ({ page }) => {
    await openStudio(page, { serverFavorites: { [SIBLING_JOB_ID]: true } });
    await expect(row(page, SIBLING_JOB_ID)).toHaveClass(/\bis-fav\b/);
    expect((await readCatalogState(page))?.tracks?.[SIBLING_JOB_ID]?.favorite).toBe(true);
  });
});

test.describe("phone", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  const openPhone = async (page, serverFavorites) => {
    const writes = await stubFavorites(page, serverFavorites);
    await page.goto("/?ui=mobile", { waitUntil: "domcontentloaded" });
    await page.locator('[data-action="tab"][data-tab="library"]').first().click();
    await page.locator(`.track[data-id="${JOB_ID}"]`).first().waitFor({ timeout: 20000 });
    return writes;
  };

  test("the Favorites chip lists only favourites", async ({ page }) => {
    await openPhone(page, { [JOB_ID]: true });
    await page.locator('[data-action="filter"][data-filter="Favorites"]').click();
    await expect(page.locator(`.track[data-id="${JOB_ID}"]`)).toBeVisible();
    await expect(page.locator(`.track[data-id="${SIBLING_JOB_ID}"]`)).toHaveCount(0);
  });

  test("a heart on the phone is saved and does not open the track", async ({ page }) => {
    const writes = await openPhone(page, {});
    const heart = page.locator(`.track-fav[data-id="${SIBLING_JOB_ID}"]`);
    await heart.click();
    await expect(heart).toHaveAttribute("aria-pressed", "true");
    await expect.poll(() => writes).toContainEqual({ id: SIBLING_JOB_ID, favorite: true });
    // Still on the library, not taken to the player.
    await expect(page.locator(`.track[data-id="${SIBLING_JOB_ID}"]`)).toBeVisible();
  });
});
