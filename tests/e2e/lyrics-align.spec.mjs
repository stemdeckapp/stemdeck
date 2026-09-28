// The Lyrics tab's Align panel: synced lyrics timed to another cut of the
// song moved onto this track. Start lyrics here puts the first sung line (or
// the one last clicked) at the playhead, the nudges move every line a step,
// Auto-detect asks the server for its estimate from the vocals stem, and
// Reset goes back to the lyrics' own timing. The wipe, the line marked and a
// line click follow at once; the offset is kept on the server for lyrics it
// keeps, and in the track's store entry for lyrics kept here.
//
// The server's endpoints are stood in for here: what they keep and estimate
// is covered in tests/test_lyrics_api.py. The fixture track is 6 seconds, so
// every time below is inside it.
import { test, expect } from "@playwright/test";
import {
  JOB_ID,
  SIBLING_JOB_ID,
  fixtureTrack,
  seedCatalogState,
  stubAudioTags,
  stubExportEndpoints,
  stubUpdateCheck,
} from "./helpers.mjs";

const LRCLIB = /^https:\/\/lrclib\.net\//;
const WIKIMEDIA = /^https:\/\/((www|query)\.wikidata\.org|[a-z-]+\.wikipedia\.org)\//;
const SYNCED = "[00:00.00]\n[00:00.50] Server one\n[00:02.00] Server two\n[00:04.00] Server three";

const version = (extra = {}) => ({
  v: 1,
  source: "lrclib",
  track: "Fixture Song",
  artist: "Fixture Band",
  album: "Studio",
  duration: 6,
  synced: SYNCED,
  plain: "Server one\nServer two\nServer three",
  instrumental: false,
  lrclib_id: 201,
  timing: "exact",
  others: [],
  ...extra,
});

/**
 * Open the fixture track's Lyrics tab with `server` as its lyrics.json (null
 * for none) and `saved` as its store entry. `detect` is what POST
 * .../lyrics/align answers. Returns what was sent to the two endpoints, and
 * the lyrics.json the stand-in keeps, which GET serves back.
 */
async function setUp(page, { server = version(), saved = null, detect = { confident: false, offset_sec: null } } = {}) {
  const kept = { lyrics: server };
  const offsets = [];
  const aligns = [];
  await page.route(LRCLIB, (route) => route.abort("internetdisconnected"));
  await page.route(`**/api/jobs/${JOB_ID}/lyrics`, (route) => {
    if (!kept.lyrics) return route.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"no lyrics"}' });
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(kept.lyrics) });
  });
  await page.route(`**/api/jobs/${JOB_ID}/lyrics/offset`, (route) => {
    const body = route.request().postDataJSON();
    offsets.push(body.offset_sec);
    kept.lyrics = { ...kept.lyrics, offset_sec: body.offset_sec };
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ offset_sec: body.offset_sec }) });
  });
  await page.route(`**/api/jobs/${JOB_ID}/lyrics/align`, (route) => {
    aligns.push(route.request().postDataJSON());
    if (detect.confident && kept.lyrics && !route.request().postDataJSON().synced) {
      kept.lyrics = { ...kept.lyrics, offset_sec: detect.offset_sec };
    }
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(detect) });
  });
  await page.route(WIKIMEDIA, (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    headers: { "access-control-allow-origin": "*" },
    body: JSON.stringify({ search: [] }),
  }));
  await stubExportEndpoints(page);
  await stubUpdateCheck(page);
  await stubAudioTags(page, null);
  await seedCatalogState(page, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: [JOB_ID, SIBLING_JOB_ID], color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks: {
      [JOB_ID]: {
        ...fixtureTrack(JOB_ID, "E2E Fixture Track"),
        audioTags: { artist: "Fixture Band", title: "Fixture Song" },
      },
      [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, "E2E Fixture Track (again)"),
    },
  });
  if (saved) {
    await page.addInitScript(
      ([key, value]) => {
        // Only on the first load: a reload must find what the page saved.
        if (!sessionStorage.getItem("e2e-seeded-lyrics")) {
          window.localStorage.setItem(key, JSON.stringify(value));
          sessionStorage.setItem("e2e-seeded-lyrics", "1");
        }
      },
      [`stemdeck.lyrics.${JOB_ID}`, saved],
    );
  }
  await openLyrics(page);
  return { kept, offsets, aligns };
}

async function openLyrics(page) {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(".cat-item").first().waitFor({ timeout: 20000 });
  await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
  await expect(page.locator(".app")).not.toHaveClass(/no-track/, { timeout: 15000 });
  await page.locator(".rail-lyrics").click();
  await expect(page.locator(".lyrics-line, .lyrics-text").first()).toBeVisible();
}

const alignLink = (page) => page.locator(".lyrics-tools .lyrics-align-toggle");
const panel = (page) => page.locator("#lyricsAlign");
const readout = (page) => page.locator("#lyricsAlign .lyrics-align-offset");
const current = (page) => page.locator(".lyrics-line.current");
const button = (page, name) => page.getByRole("button", { name, exact: true });

async function openAlign(page) {
  await alignLink(page).click();
  await expect(panel(page)).toBeVisible();
  await expect(alignLink(page)).toHaveAttribute("aria-expanded", "true");
}

/** Move the playhead without touching the lyrics. */
const seek = (page, seconds) =>
  page.evaluate(async (s) => (await import("/js/transport.js")).setPlayheadTime(s), seconds);

const stored = (page) =>
  page.evaluate((id) => JSON.parse(localStorage.getItem(`stemdeck.lyrics.${id}`) || "null"), JOB_ID);

test.describe("aligning lyrics to the track", () => {
  test("Start lyrics here moves the first sung line to the playhead, and a line click follows", async ({ page }) => {
    const { offsets } = await setUp(page);
    await openAlign(page);
    await expect(readout(page)).toHaveText("0.0 s");
    // The empty stamp before it is an instrumental gap, not the first line.
    await expect(page.locator(".lyrics-align-target")).toHaveText("Moves the first line: “Server one”");

    await seek(page, 3);
    await expect(current(page)).toHaveText("Server two");
    await button(page, "Start lyrics here").click();
    await expect(readout(page)).toHaveText("+2.5 s");
    await expect(current(page)).toHaveText("Server one");
    await expect.poll(() => offsets).toEqual([2.5]);

    // A line click seeks to where the line is now: 2.0 + 2.5.
    await page.locator(".lyrics-line", { hasText: "Server two" }).click();
    await expect(page.locator("#t-time")).toContainText("00:04");
    await expect(current(page)).toHaveText("Server two");
  });

  test("a line clicked first is the one Start lyrics here moves", async ({ page }) => {
    const { offsets } = await setUp(page);
    await openAlign(page);
    await page.locator(".lyrics-line", { hasText: "Server three" }).click();
    await expect(page.locator(".lyrics-align-target")).toHaveText("Moves the line you clicked: “Server three”");
    await expect(page.locator(".lyrics-line.picked")).toHaveText("Server three");
    await seek(page, 5);
    await button(page, "Start lyrics here").click();
    await expect(readout(page)).toHaveText("+1.0 s");
    await expect(current(page)).toHaveText("Server three");
    await expect.poll(() => offsets).toEqual([1]);
  });

  test("the nudges change the offset shown and the line marked, and the last one is saved", async ({ page }) => {
    const { offsets } = await setUp(page);
    await openAlign(page);
    // Just after "Server two" is stamped (the tab looks 50 ms ahead).
    await seek(page, 2.1);
    await expect(current(page)).toHaveText("Server two");

    await button(page, "Later by 0.5 s").click();
    await expect(readout(page)).toHaveText("+0.5 s");
    // "Server two" now starts at 2.5, after the playhead.
    await expect(current(page)).toHaveText("Server one");
    await button(page, "Earlier by 0.1 s").click();
    await button(page, "Earlier by 0.1 s").click();
    await button(page, "Earlier by 0.1 s").click();
    await expect(readout(page)).toHaveText("+0.2 s");
    await expect(current(page)).toHaveText("Server one");
    await button(page, "Earlier by 0.5 s").click();
    await expect(readout(page)).toHaveText("-0.3 s");
    await expect(current(page)).toHaveText("Server two");
    // Saved a moment after a run of nudges, not once per nudge.
    await expect.poll(() => offsets.at(-1)).toBe(-0.3);
    expect(offsets.length).toBeLessThan(5);
  });

  test("the panel works from the keyboard, and Escape closes it back onto Align", async ({ page }) => {
    await setUp(page);
    await alignLink(page).focus();
    await page.keyboard.press("Enter");
    await expect(panel(page)).toBeVisible();
    await button(page, "Later by 0.1 s").focus();
    await page.keyboard.press("Enter");
    await expect(readout(page)).toHaveText("+0.1 s");
    await page.keyboard.press("Escape");
    await expect(panel(page)).toBeHidden();
    await expect(alignLink(page)).toBeFocused();
    await expect(alignLink(page)).toHaveAttribute("aria-expanded", "false");
  });

  test("Auto-detect applies the server's estimate when it is sure", async ({ page }) => {
    const { offsets, aligns } = await setUp(page, { detect: { confident: true, offset_sec: 1.5 } });
    await openAlign(page);
    await seek(page, 2.2);
    await button(page, "Auto-detect").click();
    await expect(page.locator(".lyrics-align-message")).toHaveText("Moved to fit the vocals: +1.5 s.");
    await expect(readout(page)).toHaveText("+1.5 s");
    await expect(current(page)).toHaveText("Server one");
    // Asked about the lyrics the server keeps, which it saved itself.
    expect(aligns).toEqual([{}]);
    await page.waitForTimeout(600);
    expect(offsets).toEqual([]);
  });

  test("Auto-detect that cannot tell says so and leaves the lyrics as they are", async ({ page }) => {
    const { offsets } = await setUp(page, { server: version({ offset_sec: 1 }) });
    await openAlign(page);
    await expect(readout(page)).toHaveText("+1.0 s");
    await button(page, "Auto-detect").click();
    await expect(page.locator(".lyrics-align-message")).toHaveText("Could not tell from the vocals. The lyrics are as they were.");
    await expect(readout(page)).toHaveText("+1.0 s");
    await page.waitForTimeout(600);
    expect(offsets).toEqual([]);
  });

  test("Reset goes back to the lyrics' own timing", async ({ page }) => {
    const { offsets } = await setUp(page, { server: version({ offset_sec: 2 }) });
    // The kept offset is applied before the panel is ever opened.
    await page.locator(".lyrics-line", { hasText: "Server one" }).click();
    await expect(page.locator("#t-time")).toContainText("00:02");
    await openAlign(page);
    await expect(readout(page)).toHaveText("+2.0 s");
    await button(page, "Reset").click();
    await expect(readout(page)).toHaveText("0.0 s");
    await expect.poll(() => offsets).toEqual([0]);
    await page.locator(".lyrics-line", { hasText: "Server three" }).click();
    await expect(page.locator("#t-time")).toContainText("00:04");
  });

  test("the offset kept on the server is there after the tab is opened again", async ({ page }) => {
    const { offsets } = await setUp(page);
    await openAlign(page);
    await button(page, "Later by 0.5 s").click();
    await expect.poll(() => offsets).toEqual([0.5]);

    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    await page.locator(".rail-lyrics").click();
    await expect(page.locator(".lyrics-line", { hasText: "Server one" })).toBeVisible();
    await page.locator(".lyrics-line", { hasText: "Server three" }).click();
    await expect(page.locator("#t-time")).toContainText("00:04");
    await seek(page, 4.2);
    await expect(current(page)).toHaveText("Server two");
    await openAlign(page);
    await expect(readout(page)).toHaveText("+0.5 s");
  });

  test("lyrics kept in the browser keep their offset with them, and Auto-detect sends them along", async ({ page }) => {
    const saved = {
      entry: { ...version(), id: 77, album: "Chosen", savedAt: 1 },
      others: [],
    };
    const { offsets, aligns } = await setUp(page, { server: null, saved, detect: { confident: true, offset_sec: 0.8 } });
    await openAlign(page);
    await button(page, "Later by 0.5 s").click();
    await expect.poll(async () => (await stored(page))?.entry?.offsetSec).toBe(0.5);

    await button(page, "Auto-detect").click();
    await expect(readout(page)).toHaveText("+0.8 s");
    expect(aligns).toEqual([{ synced: SYNCED }]);
    await expect.poll(async () => (await stored(page))?.entry?.offsetSec).toBe(0.8);
    expect(offsets).toEqual([]);

    // Opening the tab again, and reloading, find it where it was left. The
    // panel stays open while the track does.
    await page.locator(".rail-library").click();
    await page.locator(".rail-lyrics").click();
    await expect(readout(page)).toHaveText("+0.8 s");
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    await page.locator(".rail-lyrics").click();
    await openAlign(page);
    await expect(readout(page)).toHaveText("+0.8 s");
    expect((await stored(page)).entry.album).toBe("Chosen");
  });

  test("text-only lyrics show Align disabled, saying why", async ({ page }) => {
    await setUp(page, { server: version({ synced: "", plain: "Server one\nServer two" }) });
    await expect(page.locator(".lyrics-text").first()).toHaveText("Server one");
    await expect(alignLink(page)).toHaveAttribute("aria-disabled", "true");
    await expect(alignLink(page)).toHaveAttribute("title", /Only synced lyrics can be aligned/);
    await alignLink(page).click({ force: true });
    await alignLink(page).press("Enter");
    await expect(panel(page)).toHaveCount(0);
  });
});
