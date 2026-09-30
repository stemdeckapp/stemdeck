// A track can be favourited at any window width (#724).
//
// The only heart used to be the Now Playing card's, and daw.css hides that
// whole card below 1460 px. A maximised laptop window is under that, so there
// was no way to favourite at all. Library and Favorites rows have one now, and
// every heart goes through one toggle, so they cannot disagree.

import { test, expect } from "@playwright/test";
import { JOB_ID, SIBLING_JOB_ID, openStudio, readCatalogState } from "./helpers.mjs";

const row = (page, id) => page.locator(`#catalogList .cat-item[data-id="${id}"]`).first();
const rowHeart = (page, id) => row(page, id).locator(".cat-fav");
const favoriteIn = async (page, id) => Boolean((await readCatalogState(page))?.tracks?.[id]?.favorite);

test.describe("favourites at a narrow window", () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test("a library row can favourite a track the Now Playing card cannot reach", async ({ page }) => {
    await openStudio(page);
    await expect(page.locator("#fav-btn")).toBeHidden();

    await row(page, SIBLING_JOB_ID).hover();
    await expect(rowHeart(page, SIBLING_JOB_ID)).toBeVisible();
    await rowHeart(page, SIBLING_JOB_ID).click();

    expect(await favoriteIn(page, SIBLING_JOB_ID)).toBe(true);
    await expect(rowHeart(page, SIBLING_JOB_ID)).toHaveAttribute("aria-pressed", "true");
    await expect(row(page, SIBLING_JOB_ID)).toHaveClass(/\bis-fav\b/);
    // The heart is a button on the row, not a way of opening the track.
    await expect(row(page, JOB_ID)).toHaveClass(/\bactive\b/);
    await expect(row(page, SIBLING_JOB_ID)).not.toHaveClass(/\bactive\b/);
  });

  test("the Favorites view can take a track back out", async ({ page }) => {
    await openStudio(page);
    await row(page, SIBLING_JOB_ID).hover();
    await rowHeart(page, SIBLING_JOB_ID).click();

    await page.locator(".rail-favorites").click();
    await expect(row(page, SIBLING_JOB_ID)).toBeVisible();
    await row(page, SIBLING_JOB_ID).hover();
    await rowHeart(page, SIBLING_JOB_ID).click();

    await expect(row(page, SIBLING_JOB_ID)).toHaveCount(0);
    expect(await favoriteIn(page, SIBLING_JOB_ID)).toBe(false);
  });

  test("keyboard focus reveals the row heart", async ({ page }) => {
    await openStudio(page);
    await rowHeart(page, SIBLING_JOB_ID).focus();
    // Polled: the reveal fades in over a short transition.
    await expect.poll(() => row(page, SIBLING_JOB_ID).locator(".cat-actions")
      .evaluate((el) => Number(getComputedStyle(el).opacity))).toBeGreaterThan(0.9);
  });
});

test.describe("favourites at a wide window", () => {
  test.use({ viewport: { width: 1600, height: 900 } });

  test("the row heart and the Now Playing heart stay in step", async ({ page }) => {
    await openStudio(page);
    const npHeart = page.locator("#fav-btn");
    await expect(npHeart).toBeVisible();
    await expect(npHeart).toHaveAttribute("aria-pressed", "false");

    await row(page, JOB_ID).hover();
    await rowHeart(page, JOB_ID).click();
    await expect(npHeart).toHaveAttribute("aria-pressed", "true");

    await npHeart.click();
    await expect(rowHeart(page, JOB_ID)).toHaveAttribute("aria-pressed", "false");
    expect(await favoriteIn(page, JOB_ID)).toBe(false);
  });
});
