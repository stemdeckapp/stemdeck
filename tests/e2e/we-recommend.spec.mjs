// We Recommend: a partner card with one link is a link as a whole; a card with
// several (NIHIL: Instagram and Spotify) is a plain card with one labelled link
// per destination, since a link cannot hold other links. The featured artist
// at the top of the dialog is covered by featured-artist.spec.mjs.
//
// The featured artist is left out of the grid, so a test about a named
// partner steps aside when that partner is the one featured (FEATURED below):
// featuring someone is then a change to featuredArtist.js alone.

import { test, expect } from "@playwright/test";

import { seedLibrary, stubTauri, stubUpdateCheck } from "./helpers.mjs";
import { FEATURED_ARTISTS } from "../../static/js/featuredArtist.js";

const FEATURED = FEATURED_ARTISTS.at(-1)?.name;
const ABOVE = "featured above the grid; featured-artist.spec.mjs checks its links";

const PLAYLIST = "https://open.spotify.com/playlist/0yKLnaIdrhqXvJOENDevgI";

async function openWeRecommend(page, { tauri = false } = {}) {
  await seedLibrary(page);
  if (tauri) await stubTauri(page);
  await stubUpdateCheck(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#friendsBtn").click();
}

test.describe("We Recommend", () => {
  test("a card with two links offers both, each opening outside the app", async ({ page }) => {
    test.skip(FEATURED === "NIHIL", `NIHIL is ${ABOVE}`);
    await openWeRecommend(page);
    const card = page.locator(".lib-friend").filter({ hasText: "NIHIL" });
    await expect(card).toHaveCount(1);
    await expect(card).toBeVisible();
    // Not a link itself, so the two below are the only things to click.
    expect(await card.evaluate((el) => el.tagName)).toBe("DIV");
    // The photo loads, rather than falling back to the monogram.
    await expect(card.locator("img.lib-friend-avatar")).toHaveAttribute("src", "/img/friends/nihil.jpg");
    await expect.poll(() => card.locator("img.lib-friend-avatar").evaluate((img) => img.naturalWidth)).toBeGreaterThan(0);

    const instagram = card.locator(".lib-friend-pill", { hasText: "Instagram" });
    const spotify = card.locator(".lib-friend-pill", { hasText: "Spotify" });
    await expect(instagram).toHaveAttribute("href", "https://www.instagram.com/somosnihil/");
    await expect(spotify).toHaveAttribute("href", "https://open.spotify.com/artist/1OeKplJxFNM6JHrWeo2SaV");
    for (const link of [instagram, spotify]) {
      await expect(link).toHaveAttribute("target", "_blank");
      await expect(link).toHaveAttribute("rel", "noopener noreferrer");
    }
  });

  test("a card with one link is still a link as a whole", async ({ page }) => {
    await openWeRecommend(page);
    // Two candidates, so one is always in the grid whoever is featured.
    const [name, url] = [
      ["Analog4Lyfe", "https://www.instagram.com/analog4lyfe"],
      ["Kris Luthier", "https://www.instagram.com/krisluthier"],
    ].find(([n]) => n !== FEATURED);
    const card = page.locator(".lib-friend").filter({ hasText: name });
    expect(await card.evaluate((el) => el.tagName)).toBe("A");
    await expect(card).toHaveAttribute("href", url);
    await expect(card.locator(".lib-friend-pill")).toHaveCount(0);
  });

  test("Killah Trakz and NIHIL offer Instagram and Spotify, and Killah Trakz Apple Music too", async ({ page }) => {
    test.skip(FEATURED === "NIHIL" || FEATURED === "Killah Trakz", `${FEATURED} is ${ABOVE}`);
    await openWeRecommend(page);
    const pills = (name) => page.locator(".lib-friend").filter({ hasText: name }).locator(".lib-friend-pill");
    await expect(pills("Killah Trakz")).toHaveText(["Instagram", "Spotify", "Apple Music"]);
    await expect(pills("NIHIL")).toHaveText(["Instagram", "Spotify"]);
    await expect(pills("NIHIL").filter({ hasText: "Spotify" })).toHaveAttribute("href", "https://open.spotify.com/artist/1OeKplJxFNM6JHrWeo2SaV");
  });

  test("the playlist link sits under the ask, with its title, and opens outside the app", async ({ page }) => {
    await openWeRecommend(page);
    const link = page.locator("#friendsDialog .friends-playlist a");
    await expect(link).toHaveCount(1);
    await expect(link).toBeVisible();
    await expect(link).toHaveAttribute("href", PLAYLIST);
    await expect(link).toHaveAttribute("target", "_blank");
    await expect(link).toHaveAttribute("rel", "noopener noreferrer");
    await expect(link).toContainText("StemDeck Recommended Artists");
    // Under the ask and above the featured card and the grid.
    const order = await page.evaluate(() => {
      const scroll = document.querySelector("#friendsDialog .friends-scroll");
      const kids = [...scroll.children];
      const at = (sel) => kids.findIndex((el) => el.matches(sel));
      return { ask: at(".friends-ask"), playlist: at(".friends-playlist"), grid: at("#friendsDialogGrid"), featured: at(".featured-artist") };
    });
    expect(order.playlist).toBe(order.ask + 1);
    expect(order.playlist).toBeLessThan(order.grid);
    if (order.featured >= 0) expect(order.playlist).toBeLessThan(order.featured);
  });

  test("on the desktop the playlist link goes to the shell, and the app stays", async ({ page }) => {
    await openWeRecommend(page, { tauri: true });
    const result = await page.evaluate(async () => {
      const seen = [];
      const original = window.__TAURI__.core.invoke;
      window.__TAURI__.core.invoke = (cmd, args) => {
        seen.push({ cmd, url: args?.url });
        return original(cmd, args);
      };
      document.querySelector("#friendsDialog .friends-playlist a").click();
      await new Promise((r) => setTimeout(r, 200));
      window.__TAURI__.core.invoke = original;
      return { seen, path: location.pathname };
    });
    expect(result.seen).toContainEqual({ cmd: "open_url", url: PLAYLIST });
    expect(result.path).toBe("/");
  });
});
