// We Recommend: a partner card with one link is a link as a whole; a card with
// several (Adrianna Claro: Spotify and Apple Music) is a plain card with one
// labelled link per destination, since a link cannot hold other links.

import { test, expect } from "@playwright/test";

import { seedLibrary, stubUpdateCheck } from "./helpers.mjs";

async function openWeRecommend(page) {
  await seedLibrary(page);
  await stubUpdateCheck(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#friendsBtn").click();
}

test.describe("We Recommend", () => {
  test("a card with two links offers both, each opening outside the app", async ({ page }) => {
    await openWeRecommend(page);
    const card = page.locator(".lib-friend").filter({ hasText: "Adrianna Claro" });
    await expect(card).toHaveCount(1);
    await expect(card).toBeVisible();
    // Not a link itself, so the two below are the only things to click.
    expect(await card.evaluate((el) => el.tagName)).toBe("DIV");
    // Her photo loads, rather than falling back to the monogram.
    await expect(card.locator("img.lib-friend-avatar")).toHaveAttribute("src", "/img/friends/adrianna-claro.webp");
    await expect.poll(() => card.locator("img.lib-friend-avatar").evaluate((img) => img.naturalWidth)).toBeGreaterThan(0);

    const spotify = card.locator(".lib-friend-pill", { hasText: "Spotify" });
    const apple = card.locator(".lib-friend-pill", { hasText: "Apple Music" });
    await expect(spotify).toHaveAttribute("href", "https://open.spotify.com/album/10fZ48iJhIe3lWcXSMUlFG");
    await expect(apple).toHaveAttribute("href", "https://music.apple.com/us/album/happy/1592026727?i=1592026728");
    for (const link of [spotify, apple]) {
      await expect(link).toHaveAttribute("target", "_blank");
      await expect(link).toHaveAttribute("rel", "noopener noreferrer");
    }
  });

  test("a card with one link is still a link as a whole", async ({ page }) => {
    await openWeRecommend(page);
    const card = page.locator(".lib-friend").filter({ hasText: "Analog4Lyfe" });
    expect(await card.evaluate((el) => el.tagName)).toBe("A");
    await expect(card).toHaveAttribute("href", "https://www.instagram.com/analog4lyfe");
    await expect(card.locator(".lib-friend-pill")).toHaveCount(0);
  });

  test("Killah Trakz and NIHIL offer Instagram and Spotify, and Killah Trakz Apple Music too", async ({ page }) => {
    await openWeRecommend(page);
    const pills = (name) => page.locator(".lib-friend").filter({ hasText: name }).locator(".lib-friend-pill");
    await expect(pills("Killah Trakz")).toHaveText(["Instagram", "Spotify", "Apple Music"]);
    await expect(pills("NIHIL")).toHaveText(["Instagram", "Spotify"]);
    await expect(pills("NIHIL").filter({ hasText: "Spotify" })).toHaveAttribute("href", "https://open.spotify.com/artist/1OeKplJxFNM6JHrWeo2SaV");
  });
});
