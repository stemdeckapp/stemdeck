// The featured artist at the top of We Recommend (#1092): one artist, with a
// month, a quote, one track to start with and their links, all opening outside
// the app. Nothing plays in StemDeck, so the track row is a link, not a player.
// With one entry there is no "Previously featured" row, and the featured artist
// is not repeated in the grid below.
//
// The "Previously featured" row itself cannot be reached here: it draws only
// with two entries or more, and FEATURED_ARTISTS has one. What it draws and
// which card an avatar goes to are pure functions (previousFaces,
// findFriendCard), covered in tests/js/featured-artist.test.mjs.

import { test, expect } from "@playwright/test";

import { seedLibrary, stubUpdateCheck } from "./helpers.mjs";

async function openWithGuide(page) {
  await seedLibrary(page);
  await stubUpdateCheck(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  // The Guide first: We Recommend still opens its dialog in the Guide, and
  // the open dialog covers the rail.
  await page.locator("#helpModeBtn").click();
  await expect(page.locator("body")).toHaveClass(/help-mode/);
  await page.locator("#friendsBtn").click();
  await expect(page.locator(".featured-artist")).toBeVisible();
}

const SPOTIFY = "https://open.spotify.com/album/10fZ48iJhIe3lWcXSMUlFG";
const APPLE = "https://music.apple.com/us/album/happy/1592026727?i=1592026728";

async function openWeRecommend(page) {
  await seedLibrary(page);
  await stubUpdateCheck(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#friendsBtn").click();
  await expect(page.locator("#friendsDialog")).toBeVisible();
}

test.describe("We Recommend: featured artist", () => {
  test("the card shows Adrianna Claro with the month, her photo, role and quote", async ({ page }) => {
    await openWeRecommend(page);
    const card = page.getByRole("region", { name: "Adrianna Claro" });
    await expect(card).toBeVisible();
    // Above the grid, inside the dialog.
    const order = await page.evaluate(() => {
      const c = document.querySelector("#friendsDialog .featured-artist");
      const g = document.getElementById("friendsDialogGrid");
      return Boolean(c && g && c.compareDocumentPosition(g) & Node.DOCUMENT_POSITION_FOLLOWING);
    });
    expect(order).toBe(true);

    await expect(card.locator(".featured-label")).toHaveText("Featured artist · November 2026");
    await expect(card.getByRole("heading", { name: "Adrianna Claro" })).toBeVisible();
    await expect(card.locator(".featured-role")).toHaveText("Soul and Contemporary R&B Singer, Songwriter and Composer");
    await expect(card.locator(".featured-quote")).toBeVisible();
    await expect(card.locator("img.featured-photo")).toHaveAttribute("src", "/img/friends/adrianna-claro.webp");
    await expect.poll(() => card.locator("img.featured-photo").evaluate((img) => img.naturalWidth)).toBeGreaterThan(0);
  });

  test("the track row is one link that opens outside the app, with no play button", async ({ page }) => {
    await openWeRecommend(page);
    const track = page.locator(".featured-artist a.featured-track");
    await expect(track).toHaveCount(1);
    await expect(track).toHaveAttribute("href", APPLE);
    await expect(track).toHaveAttribute("target", "_blank");
    await expect(track).toHaveAttribute("rel", "noopener noreferrer");
    await expect(track).toHaveAccessibleName("Listen to Happy by Adrianna Claro, opens in your web browser");
    await expect(track.locator(".featured-track-title")).toHaveText("Happy");
    await expect(track.locator(".featured-track-meta")).toHaveText(/^Single · \d{4}$/);
    await expect(track.locator(".featured-track-listen")).toHaveText("Listen");
    // Nothing inside the row is a control of its own.
    await expect(track.locator("button, a, audio")).toHaveCount(0);
  });

  test("the link pills open outside the app, titled like the grid's", async ({ page }) => {
    await openWeRecommend(page);
    const pills = page.locator(".featured-artist .featured-pill");
    await expect(pills).toHaveText(["Spotify", "Apple Music"]);
    const spotify = pills.filter({ hasText: "Spotify" });
    const apple = pills.filter({ hasText: "Apple Music" });
    await expect(spotify).toHaveAttribute("href", SPOTIFY);
    await expect(apple).toHaveAttribute("href", APPLE);
    await expect(spotify).toHaveAttribute("title", "Adrianna Claro: Spotify");
    for (const link of [spotify, apple]) {
      await expect(link).toHaveAttribute("target", "_blank");
      await expect(link).toHaveAttribute("rel", "noopener noreferrer");
    }
    // A size up from the grid's pills, which share their class.
    const size = (loc) => loc.evaluate((el) => parseFloat(getComputedStyle(el).fontSize));
    const gridPill = page.locator("#friendsDialogGrid .lib-friend-pill").first();
    expect(await size(spotify)).toBeGreaterThan(await size(gridPill));
  });

  test("opening focuses the close button, Esc closes, and focus goes back to the rail", async ({ page }) => {
    await openWeRecommend(page);
    await expect(page.locator("#friendsClose")).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(page.locator("#friendsDialog")).toBeHidden();
    await expect(page.locator("#friendsBtn")).toBeFocused();
  });

  test("the close button stays in view when the dialog is scrolled", async ({ page }) => {
    await openWeRecommend(page);
    await page.setViewportSize({ width: 1280, height: 600 });
    const scroller = page.locator("#friendsDialog .friends-scroll");
    await expect.poll(() => scroller.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true);
    await scroller.evaluate((el) => { el.scrollTop = el.scrollHeight; });
    const close = page.locator("#friendsClose");
    await expect(close).toBeInViewport();
    const box = await close.boundingBox();
    const card = await page.locator("#friendsDialog .friends-card").boundingBox();
    expect(box.y).toBeGreaterThanOrEqual(card.y);
    expect(box.y).toBeLessThan(card.y + 40);
    await close.click();
    await expect(page.locator("#friendsDialog")).toBeHidden();
  });

  test("with one entry there is no Previously featured row", async ({ page }) => {
    await openWeRecommend(page);
    await expect(page.locator(".featured-artist")).toBeVisible();
    await expect(page.locator(".featured-prev")).toHaveCount(0);
    await expect(page.locator(".featured-prev-btn")).toHaveCount(0);
  });

  test("the featured artist is not repeated in the grid", async ({ page }) => {
    await openWeRecommend(page);
    const grid = page.locator("#friendsDialogGrid");
    await expect(grid.locator(".lib-friend").first()).toBeVisible();
    await expect(grid.locator(".lib-friend").filter({ hasText: "Adrianna Claro" })).toHaveCount(0);
    // The rest of her category is still there.
    await expect(grid.locator(".lib-friend").filter({ hasText: "NIHIL" })).toHaveCount(1);
  });

  test("a narrow window stacks the photo above the text, with no sideways scroll", async ({ page }) => {
    // Opened at the normal size, then narrowed: the dialog is what is under
    // test, not whether the rail fits a 600 px window.
    await openWeRecommend(page);
    await page.setViewportSize({ width: 600, height: 800 });
    await expect(page.locator(".featured-artist .featured-photo")).toBeVisible();
    const photo = await page.locator(".featured-artist .featured-photo").boundingBox();
    const name = await page.locator(".featured-artist .featured-name").boundingBox();
    expect(photo && name && name.y >= photo.y + photo.height - 1).toBe(true);
    const overflow = await page.locator(".friends-scroll").evaluate((el) => el.scrollWidth - el.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
  });

  test("the Guide explains the track link and the pills", async ({ page }) => {
    await openWithGuide(page);
    const tip = page.locator("#helpTip");
    await page.locator(".featured-artist .featured-track").hover();
    await expect(tip).toContainText("Opens this track by Adrianna Claro in your web browser");
    await page.locator(".featured-artist .featured-pill").first().hover();
    await expect(tip).toContainText("Opens Adrianna Claro on this service in your web browser");
  });

  test("the Guide explains the grid's cards, their links and the close button", async ({ page }) => {
    await openWithGuide(page);
    const tip = page.locator("#helpTip");
    const grid = page.locator("#friendsDialogGrid");
    await grid.locator("a.lib-friend").filter({ hasText: "Analog4Lyfe" }).hover();
    await expect(tip).toContainText("Opens the page of Analog4Lyfe in your web browser");
    const nihil = grid.locator(".lib-friend-multi").filter({ hasText: "NIHIL" });
    await nihil.locator(".lib-friend-name").hover();
    await expect(tip).toContainText("NIHIL can be found in more than one place");
    await nihil.locator(".lib-friend-pill").first().hover();
    await expect(tip).toContainText("Opens NIHIL on this service in your web browser");
    await page.locator("#friendsClose").hover();
    await expect(tip).toContainText("Closes this window.");
  });

  test("with the Guide on, Esc leaves the Guide and a second Esc closes the dialog", async ({ page }) => {
    await openWithGuide(page);
    // As the Donate dialog: the Guide takes the first Esc.
    await page.keyboard.press("Escape");
    await expect(page.locator("body")).not.toHaveClass(/help-mode/);
    await expect(page.locator("#friendsDialog")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.locator("#friendsDialog")).toBeHidden();
    await expect(page.locator("#friendsBtn")).toBeFocused();
  });
});
