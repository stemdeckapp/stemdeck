// The featured artist at the top of We Recommend (#1092): one artist, under a
// "Featured artist" label with no date, an optional quote, an optional track to start with, and their links,
// all opening outside the app. Nothing plays in StemDeck, so a track row is a
// link, not a player. The featured artist is not repeated in the grid below.
//
// Every expectation about the artist is read from FEATURED_ARTISTS and the
// English strings, not written here, so featuring someone else is a change to
// static/js/featuredArtist.js alone. A part the current entry does not have
// (a track, a quote) is checked to be absent instead.
//
// The "Previously featured" row is checked only while the list has one entry:
// it draws only with two or more. What it draws and which card an avatar goes
// to are pure functions (previousFaces, findFriendCard), covered in
// tests/js/featured-artist.test.mjs.

import { test, expect } from "@playwright/test";

import { seedLibrary, stubUpdateCheck } from "./helpers.mjs";
import { FEATURED_ARTISTS } from "../../static/js/featuredArtist.js";
import { TRANSLATIONS } from "../../static/js/i18n.js";

const CURRENT = FEATURED_ARTISTS.at(-1);
const EN = TRANSLATIONS.en;
/** The English string for `key`, filled in as t() fills it. */
const en = (key, vars = {}) => EN[key].replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));

async function openWeRecommend(page) {
  await seedLibrary(page);
  await stubUpdateCheck(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#friendsBtn").click();
  await expect(page.locator("#friendsDialog")).toBeVisible();
}

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

test.describe("We Recommend: featured artist", () => {
  test("the card shows the current artist with the label, photo, role and quote", async ({ page }) => {
    await openWeRecommend(page);
    const card = page.getByRole("region", { name: CURRENT.name });
    await expect(card).toBeVisible();
    // Above the grid, inside the dialog.
    const order = await page.evaluate(() => {
      const c = document.querySelector("#friendsDialog .featured-artist");
      const g = document.getElementById("friendsDialogGrid");
      return Boolean(c && g && c.compareDocumentPosition(g) & Node.DOCUMENT_POSITION_FOLLOWING);
    });
    expect(order).toBe(true);

    // "Featured artist" and nothing more: no month, no date.
    await expect(card.locator(".featured-label")).toHaveText(en("friends.featured.label"));
    await expect(card.getByRole("heading", { name: CURRENT.name })).toBeVisible();
    // The role comes from the partner's own entry: shown, and never a raw key.
    const role = card.locator(".featured-role");
    await expect(role).toBeVisible();
    await expect(role).not.toHaveText(/^friends\./);

    const quote = card.locator(".featured-quote");
    if (CURRENT.quoteKey && EN[CURRENT.quoteKey]) {
      await expect(quote).toHaveText(EN[CURRENT.quoteKey]);
    } else {
      await expect(quote).toHaveCount(0);
    }

    // A photo, when there is one, loads rather than failing silently.
    const photo = card.locator("img.featured-photo");
    if (await photo.count()) {
      await expect.poll(() => photo.evaluate((img) => img.naturalWidth)).toBeGreaterThan(0);
    }
  });

  test("a track, when there is one, is one link that opens outside the app, with no play button", async ({ page }) => {
    await openWeRecommend(page);
    const track = page.locator(".featured-artist a.featured-track");
    const start = page.locator(".featured-artist .featured-start");
    if (!CURRENT.track) {
      // No "Start here" over nothing.
      await expect(track).toHaveCount(0);
      await expect(start).toHaveCount(0);
      return;
    }
    await expect(start).toHaveText(en("friends.featured.startHere"));
    await expect(track).toHaveCount(1);
    await expect(track).toHaveAttribute("href", CURRENT.track.url);
    await expect(track).toHaveAttribute("target", "_blank");
    await expect(track).toHaveAttribute("rel", "noopener noreferrer");
    await expect(track).toHaveAccessibleName(en("friends.featured.trackAria", { title: CURRENT.track.title, artist: CURRENT.name }));
    await expect(track.locator(".featured-track-title")).toHaveText(CURRENT.track.title);
    await expect(track.locator(".featured-track-listen")).toHaveText(en("friends.featured.listen"));
    // Nothing inside the row is a control of its own.
    await expect(track.locator("button, a, audio")).toHaveCount(0);
  });

  test("the link pills open outside the app, titled like the grid's", async ({ page }) => {
    await openWeRecommend(page);
    const pills = page.locator(".featured-artist .featured-pill");
    await expect(pills).toHaveText(CURRENT.links.map((l) => l.label));
    for (const [i, link] of CURRENT.links.entries()) {
      const pill = pills.nth(i);
      await expect(pill).toHaveAttribute("href", link.url);
      await expect(pill).toHaveAttribute("title", `${CURRENT.name}: ${link.label}`);
      await expect(pill).toHaveAttribute("target", "_blank");
      await expect(pill).toHaveAttribute("rel", "noopener noreferrer");
    }
    // A size up from the grid's pills, which share their class.
    const size = (loc) => loc.evaluate((el) => parseFloat(getComputedStyle(el).fontSize));
    const gridPill = page.locator("#friendsDialogGrid .lib-friend-pill").first();
    expect(await size(pills.first())).toBeGreaterThan(await size(gridPill));
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

  test("the Previously featured row matches the list", async ({ page }) => {
    await openWeRecommend(page);
    await expect(page.locator(".featured-artist")).toBeVisible();
    const earlier = new Set(FEATURED_ARTISTS.slice(0, -1).map((e) => e.name).filter((n) => n !== CURRENT.name));
    if (earlier.size === 0) {
      await expect(page.locator(".featured-prev")).toHaveCount(0);
      await expect(page.locator(".featured-prev-btn")).toHaveCount(0);
    } else {
      await expect(page.getByRole("group", { name: en("friends.featured.previous") })).toBeVisible();
      await expect(page.locator(".featured-prev-btn")).toHaveCount(earlier.size);
    }
  });

  test("the featured artist is not repeated in the grid", async ({ page }) => {
    await openWeRecommend(page);
    const grid = page.locator("#friendsDialogGrid");
    await expect(grid.locator(".lib-friend").first()).toBeVisible();
    const names = await grid.locator(".lib-friend").evaluateAll((els) => els.map((el) => el.dataset.friend));
    expect(names).not.toContain(CURRENT.name);
    expect(names.length).toBeGreaterThan(0);
  });

  test("the card has no empty rows: each part present is filled", async ({ page }) => {
    await openWeRecommend(page);
    const card = page.locator(".featured-artist");
    // Every row the card drew has something in it, whatever the entry leaves out.
    const empty = await card.evaluate((el) =>
      [...el.querySelectorAll(".featured-quote, .featured-start, .featured-track, .featured-foot, .featured-links")]
        .filter((n) => !n.textContent.trim())
        .map((n) => n.className),
    );
    expect(empty).toEqual([]);
  });

  test("a narrow window stacks the photo above the text, with no sideways scroll", async ({ page }) => {
    // Opened at the normal size, then narrowed: the dialog is what is under
    // test, not whether the rail fits a 600 px window.
    await openWeRecommend(page);
    await page.setViewportSize({ width: 600, height: 800 });
    const photoEl = page.locator(".featured-artist .featured-photo");
    if (await photoEl.count()) {
      await expect(photoEl).toBeVisible();
      const photo = await photoEl.boundingBox();
      const name = await page.locator(".featured-artist .featured-name").boundingBox();
      expect(photo && name && name.y >= photo.y + photo.height - 1).toBe(true);
    }
    const overflow = await page.locator(".friends-scroll").evaluate((el) => el.scrollWidth - el.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
  });

  test("the Guide explains the card's track link and pills", async ({ page }) => {
    await openWithGuide(page);
    const tip = page.locator("#helpTip");
    if (CURRENT.track) {
      await page.locator(".featured-artist .featured-track").hover();
      await expect(tip).toContainText(en("help.featuredTrack", { name: CURRENT.name }));
    }
    await page.locator(".featured-artist .featured-pill").first().hover();
    await expect(tip).toContainText(en("help.featuredLink", { name: CURRENT.name }));
  });

  test("the Guide explains the grid's cards, their links and the close button", async ({ page }) => {
    await openWithGuide(page);
    const tip = page.locator("#helpTip");
    const grid = page.locator("#friendsDialogGrid");
    // Whichever partners are in the grid: the featured artist is not.
    const single = grid.locator("a.lib-friend").first();
    const singleName = await single.getAttribute("data-friend");
    await single.hover();
    await expect(tip).toContainText(en("help.friendCard", { name: singleName }));
    const multi = grid.locator(".lib-friend-multi").first();
    const multiName = await multi.getAttribute("data-friend");
    await multi.locator(".lib-friend-name").hover();
    await expect(tip).toContainText(en("help.friendCardMulti", { name: multiName }));
    await multi.locator(".lib-friend-pill").first().hover();
    await expect(tip).toContainText(en("help.friendPill", { name: multiName }));
    await page.locator("#friendsClose").hover();
    await expect(tip).toContainText(en("help.friendsClose"));
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
