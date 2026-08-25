// Regression tests for topbar search and preview (#441, #442).
//
// /api/search and /api/search/preview are stubbed. These cover our behaviour,
// not YouTube's, and a suite that reaches YouTube fails on a flagged CI IP for
// reasons that have nothing to do with the change under test.
//
// The three things worth protecting here were all found by hand and would all
// come back silently:
//
//   The request count. Firing per keystroke instead of per word is not a
//   visible bug, it is twenty six requests where one would do.
//
//   The dropdown being visible. It rendered correctly and was clipped out of
//   existence by an ancestor's overflow:hidden. A test that counts rows in the
//   DOM passes while the user sees nothing, which is exactly what happened.
//
//   Picking not importing. Extraction is minutes of work, so a click in a list
//   the user may still be reading must not start one.

import { expect, test } from "@playwright/test";

import { seedLibrary } from "./helpers.mjs";

const RESULTS = [
  { title: "Tina Turner - The Best", duration: 250, uploader: "Tina Turner", thumbnail: null, too_long: false, url: "https://www.youtube.com/watch?v=dQw4w9WgXcQ" },
  { title: "A Very Long Compilation", duration: 4275, uploader: "Someone", thumbnail: null, too_long: true, url: "https://www.youtube.com/watch?v=aQw4w9WgXcA" },
  { title: "The Best (Live)", duration: 300, uploader: "Tina Turner", thumbnail: null, too_long: false, url: "https://www.youtube.com/watch?v=bQw4w9WgXcB" },
];

/** Stub search, and record every request so counts can be asserted. */
async function stubSearch(page, { items = RESULTS, maxDuration = 1200, fail = false } = {}) {
  const calls = [];
  await page.route("**/api/search", async (route) => {
    calls.push(JSON.parse(route.request().postData() || "{}"));
    if (fail) return route.fulfill({ status: 502, contentType: "application/json", body: '{"detail":"nope"}' });
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items, max_duration_sec: maxDuration, cached: false }),
    });
  });
  return calls;
}

/** A tiny silent wav, so <audio> has something real to load. */
async function stubPreview(page) {
  const hits = [];
  await page.route("**/api/search/preview**", async (route) => {
    hits.push(route.request().url());
    const header = Buffer.from(
      "524946462400000057415645666d7420100000000100010044ac000088580100020010006461746100000000",
      "hex",
    );
    await route.fulfill({ status: 200, contentType: "audio/wav", body: header });
  });
  return hits;
}

async function openApp(page) {
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#url").waitFor({ state: "visible" });
}

const panel = (page) => page.locator(".search-panel");
const rows = (page) => page.locator(".search-row");

test.describe("topbar search", () => {
  test("a typed phrase costs one request, not one per keystroke", async ({ page }) => {
    const calls = await stubSearch(page);
    await openApp(page);

    await page.locator("#url").type("simply the best ", { delay: 25 });
    await expect(rows(page).first()).toBeVisible();

    // The whole point of word-boundary triggering. Sixteen characters.
    expect(calls.length).toBeLessThanOrEqual(2);
    expect(calls.at(-1).query).toBe("simply the best");
  });

  test("the dropdown is actually visible, not merely rendered", async ({ page }) => {
    await stubSearch(page);
    await openApp(page);
    await page.locator("#url").type("best ", { delay: 25 });

    // toBeVisible, not a DOM count: the panel once rendered correctly inside an
    // ancestor with overflow:hidden and was clipped away entirely.
    await expect(panel(page)).toBeVisible();
    await expect(rows(page).first()).toBeVisible();

    const box = await panel(page).boundingBox();
    const composer = await page.locator(".daw-composer").boundingBox();
    expect(box.width).toBeGreaterThan(100);
    expect(Math.abs(box.x - composer.x)).toBeLessThan(2);
    expect(box.y).toBeGreaterThan(0);
  });

  test("a pasted link is left to the import flow", async ({ page }) => {
    const calls = await stubSearch(page);
    await openApp(page);

    await page.locator("#url").fill("https://www.youtube.com/watch?v=dQw4w9WgXcQ");
    await page.waitForTimeout(900);

    expect(calls).toHaveLength(0);
    await expect(panel(page)).toBeHidden();
  });

  test("a query shorter than the minimum asks nothing", async ({ page }) => {
    const calls = await stubSearch(page);
    await openApp(page);
    await page.locator("#url").type("a", { delay: 25 });
    await page.waitForTimeout(900);
    expect(calls).toHaveLength(0);
  });

  test("switching tab re-asks the same question of a different service", async ({ page }) => {
    const calls = await stubSearch(page);
    await openApp(page);
    await page.locator("#url").type("best ", { delay: 25 });
    await expect(rows(page).first()).toBeVisible();

    await page.locator(".search-tab").nth(2).click();
    // Poll rather than read once: the previous tab's rows stay on screen until
    // the new request starts, so an immediate read sees the old call.
    await expect.poll(() => calls.at(-1)?.source, { timeout: 10_000 }).toBe("soundcloud");
    await expect(rows(page).first()).toBeVisible();

    // The panel is a sibling of the composer, so a mousedown on a tab once
    // counted as an outside click and closed the dropdown before the tab's own
    // handler ran.
    await expect(panel(page)).toBeVisible();
    await expect(page.locator(".search-tab.active")).toHaveText("SoundCloud songs");
    // Focus must stay in the box the user is typing into.
    expect(await page.locator("#url").evaluate((el) => document.activeElement === el)).toBe(true);
  });

  test("picking a result fills the box and starts nothing", async ({ page }) => {
    await stubSearch(page);
    const jobPosts = [];
    await page.route("**/api/jobs**", async (route) => {
      if (route.request().method() === "POST") jobPosts.push(route.request().url());
      await route.fulfill({ status: 503, contentType: "application/json", body: '{"detail":"blocked"}' });
    });
    await openApp(page);

    await page.locator("#url").type("best ", { delay: 25 });
    await expect(rows(page).first()).toBeVisible();
    await page.locator(".search-row:not(.too-long)").first().click();

    await expect(page.locator("#url")).toHaveValue(/youtube\.com\/watch\?v=/);
    await expect(panel(page)).toBeHidden();
    // Extraction is minutes of work. It waits for Split stems.
    expect(jobPosts).toHaveLength(0);
  });

  test("a result over the limit is refused, and says whose limit", async ({ page }) => {
    await stubSearch(page, { maxDuration: 1200 });
    const jobPosts = [];
    await page.route("**/api/jobs**", async (route) => {
      if (route.request().method() === "POST") jobPosts.push(1);
      await route.fulfill({ status: 503, contentType: "application/json", body: "{}" });
    });
    await openApp(page);
    await page.locator("#url").type("best ", { delay: 25 });
    await expect(rows(page).first()).toBeVisible();

    const tooLong = page.locator(".search-row.too-long").first();
    await expect(tooLong).toBeVisible();
    await expect(tooLong.locator(".search-warn")).toHaveText("Over 20 min");
    await expect(tooLong.locator(".search-warn")).toHaveAttribute("title", /Settings/);

    await tooLong.click({ force: true });
    await page.waitForTimeout(400);
    expect(jobPosts).toHaveLength(0);
    await expect(page.locator("#url")).not.toHaveValue(/youtube\.com/);
  });

  test("the limit shown follows the setting, not a hardcoded 20", async ({ page }) => {
    // #443: three places held the ceiling and the client's copy went stale.
    await stubSearch(page, { maxDuration: 3600 });
    await openApp(page);
    await page.locator("#url").type("best ", { delay: 25 });
    await expect(rows(page).first()).toBeVisible();
    await expect(page.locator(".search-row.too-long .search-warn").first()).toHaveText("Over 60 min");
  });

  test("a failed search says so and can be retried", async ({ page }) => {
    await stubSearch(page, { fail: true });
    await openApp(page);
    await page.locator("#url").type("best ", { delay: 25 });
    await expect(page.locator(".search-status")).toBeVisible();
    await expect(page.locator(".search-status")).toHaveText(/failed/i);
  });

  test("Escape closes it", async ({ page }) => {
    await stubSearch(page);
    await openApp(page);
    await page.locator("#url").type("best ", { delay: 25 });
    await expect(panel(page)).toBeVisible();
    await page.locator("#url").press("Escape");
    await expect(panel(page)).toBeHidden();
  });

  test("arrow keys move a selection and name it for assistive tech", async ({ page }) => {
    await stubSearch(page);
    await openApp(page);
    await page.locator("#url").type("best ", { delay: 25 });
    await expect(rows(page).first()).toBeVisible();

    await page.locator("#url").press("ArrowDown");
    await expect(page.locator(".search-row.active")).toHaveCount(1);
    await expect(page.locator("#url")).toHaveAttribute("aria-activedescendant", /search-row-/);
  });
});

test.describe("preview", () => {
  test("every result offers a labelled preview, not a bare glyph", async ({ page }) => {
    await stubSearch(page);
    await openApp(page);
    await page.locator("#url").type("best ", { delay: 25 });
    await expect(rows(page).first()).toBeVisible();

    const btn = page.locator(".search-preview-btn").first();
    // Visible without hovering: a control you have to discover is one most
    // people never find.
    await expect(btn).toBeVisible();
    await expect(btn).toHaveText("Preview");
  });

  test("pressing preview opens a player and does not select the track", async ({ page }) => {
    await stubSearch(page);
    const hits = await stubPreview(page);
    const jobPosts = [];
    await page.route("**/api/jobs**", async (route) => {
      if (route.request().method() === "POST") jobPosts.push(1);
      await route.fulfill({ status: 503, contentType: "application/json", body: "{}" });
    });
    await openApp(page);

    await page.locator("#url").type("best ", { delay: 25 });
    await expect(rows(page).first()).toBeVisible();
    await page.locator(".search-row:not(.too-long)").first().locator(".search-preview-btn").click();

    await expect(page.locator(".search-player")).toBeVisible();
    await expect(page.locator(".search-seek")).toBeVisible();
    // preload="none" means the fetch starts on play(), not on setting src, so
    // this has to be waited for rather than read the instant the UI appears.
    await expect.poll(() => hits.length, { timeout: 10_000 }).toBeGreaterThan(0);
    // Auditioning is not choosing.
    expect(jobPosts).toHaveLength(0);
    await expect(page.locator("#url")).not.toHaveValue(/youtube\.com/);
  });

  test("only one preview is open at a time", async ({ page }) => {
    await stubSearch(page);
    await stubPreview(page);
    await openApp(page);
    await page.locator("#url").type("best ", { delay: 25 });
    await expect(rows(page).first()).toBeVisible();

    await page.locator(".search-preview-btn").nth(0).click();
    await expect(page.locator(".search-player")).toHaveCount(1);
    await page.locator(".search-preview-btn").nth(2).click();
    await expect(page.locator(".search-player")).toHaveCount(1);
  });

  test("closing the dropdown stops the audio", async ({ page }) => {
    await stubSearch(page);
    await stubPreview(page);
    await openApp(page);
    await page.locator("#url").type("best ", { delay: 25 });
    await expect(rows(page).first()).toBeVisible();
    await page.locator(".search-preview-btn").first().click();
    await expect(page.locator(".search-player")).toBeVisible();

    await page.locator("#url").press("Escape");
    // Nothing is more jarring than a dropdown that vanishes and keeps playing.
    await expect(page.locator(".search-player")).toHaveCount(0);
  });

  test("a playlist result has nothing to audition", async ({ page }) => {
    await stubSearch(page, {
      items: [
        { title: "An Album", duration: null, uploader: "Someone", thumbnail: null, too_long: false, url: "https://www.youtube.com/playlist?list=PLabcdefgh" },
      ],
    });
    await openApp(page);
    await page.locator("#url").type("best ", { delay: 25 });
    await expect(rows(page).first()).toBeVisible();

    await page.locator(".search-tab").nth(1).click();
    await expect(rows(page).first()).toBeVisible();
    await expect(page.locator(".search-preview-btn")).toHaveCount(0);
  });
});
