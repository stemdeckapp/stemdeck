// Tracks imported before tags were read (#699) have none in the library, so
// opening one asks the server to read them now, once, in the background. What
// comes back is kept on the track, the card names the artist, the band is found
// and saved from it, and the artist box opens on that band rather than on an
// empty search field.
//
// The tag read and every Wikimedia and LRCLIB request are answered here, so the
// tests run offline and do not depend on what the backend finds in the fixture.
import { test, expect } from "@playwright/test";
import {
  JOB_ID,
  SIBLING_JOB_ID,
  fixtureTrack,
  readCatalogState,
  seedCatalogState,
  stubAudioTags,
  stubExportEndpoints,
  stubUpdateCheck,
  LYRICS_LOOKUP,
} from "./helpers.mjs";

// Wide enough for the now-playing card, which the bar hides below 1460px.
test.use({ viewport: { width: 1600, height: 900 } });

const WIKIMEDIA = /^https:\/\/([a-z]+\.wikipedia\.org|www\.wikidata\.org|query\.wikidata\.org)\//;

// Longer than artistInfo.js's wait after a track opens.
const PAST_THE_WAIT_MS = 2000;

const TAGS = { artist: "Dream Theater", title: "Metropolis Pt. 1" };

const ANSWERS = {
  search: { search: [{ id: "Q162586" }] },
  entities: {
    entities: {
      Q162586: {
        id: "Q162586",
        labels: { en: { value: "Dream Theater" } },
        descriptions: { en: { value: "American progressive metal band" } },
        sitelinks: {
          enwiki: { site: "enwiki", title: "Dream Theater", url: "https://en.wikipedia.org/wiki/Dream_Theater" },
        },
        claims: { P434: [{}] },
      },
    },
  },
  members: { entities: {} },
  sparql: { results: { bindings: [] } },
  extract: { query: { pages: [{ extract: "Lead.\n== History ==\nIn 1985, three Berklee students formed a band." }] } },
};

async function stubWikimedia(page) {
  const asked = [];
  await page.route(WIKIMEDIA, (route) => {
    const url = new URL(route.request().url());
    asked.push(url.toString());
    const kind =
      url.host === "query.wikidata.org" ? "sparql"
      : url.host.endsWith("wikipedia.org") ? "extract"
      : url.searchParams.get("action") === "wbsearchentities" ? "search"
      : url.searchParams.get("props") === "labels" ? "members"
      : "entities";
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "access-control-allow-origin": "*" },
      body: JSON.stringify(ANSWERS[kind]),
    });
  });
  return asked;
}

/** Both fixture tracks, untagged, as a library from before tags were read. */
async function openStudio(page, state = null) {
  const tracks = {
    [JOB_ID]: fixtureTrack(JOB_ID, "E2E Fixture Track"),
    [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, "E2E Fixture Track (again)"),
  };
  await seedCatalogState(page, state ?? {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: Object.keys(tracks), color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks,
  });
  await stubExportEndpoints(page);
  await stubUpdateCheck(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(".cat-item").first().waitFor({ timeout: 20000 });
}

/** The library as the app saved it, written back before a reload. */
async function reloadKeepingLibrary(page) {
  await seedCatalogState(page, await readCatalogState(page));
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.locator(".cat-item").first().waitFor({ timeout: 20000 });
}

async function openTrack(page, id = JOB_ID) {
  await page.locator(`.cat-item[data-id="${id}"]`).first().click();
  await expect(page.locator(".app")).not.toHaveClass(/no-track/, { timeout: 15000 });
}

const stored = async (page, id = JOB_ID) => (await readCatalogState(page))?.tracks?.[id] ?? {};
const timesAsked = (asked, id = JOB_ID) => asked.filter((x) => x === id).length;

test.describe("tags for tracks imported before they were read", () => {
  test("an untagged track gets its tags on open, once, and its band saved from them", async ({ page }) => {
    const wiki = await stubWikimedia(page);
    const asked = await stubAudioTags(page, (id) => (id === JOB_ID ? TAGS : null));
    await openStudio(page);
    await openTrack(page);

    // The card, from the tags that just arrived.
    await expect(page.locator("#np-artist")).toHaveText("Dream Theater", { timeout: 15000 });
    await expect(page.locator("#title")).toHaveText("Metropolis Pt. 1");

    // Kept on the track, and the band found from them saved without a click.
    await expect.poll(async () => (await stored(page)).audioTags, { timeout: 15000 }).toEqual(TAGS);
    expect((await stored(page)).audioTagsChecked).toBe(true);
    await expect.poll(async () => (await stored(page)).artist, { timeout: 15000 })
      .toEqual({ id: "Q162586", name: "Dream Theater", englishName: "Dream Theater" });
    expect(wiki[0]).toContain("search=Dream+Theater");

    // Opened again, it is not asked again: not in this session...
    await openTrack(page, SIBLING_JOB_ID);
    await openTrack(page);
    await expect(page.locator("#np-artist")).toHaveText("Dream Theater");
    // ...and not in the next.
    await reloadKeepingLibrary(page);
    await openTrack(page);
    await page.waitForTimeout(PAST_THE_WAIT_MS);
    expect(timesAsked(asked)).toBe(1);

    // The box opens on the band, with no search row in the way.
    await page.locator("#np-details-btn").click();
    await expect(page.locator(".artist-name")).toHaveText("Dream Theater");
    await expect(page.locator(".artist-save")).toHaveText("Saved for this track");
    await expect(page.locator("#artistSearch")).toBeHidden();
  });

  test("a band the server found with the tags is saved, with no search of the page's own", async ({ page }) => {
    const wiki = await stubWikimedia(page);
    const band = { id: "Q162586", name: "Dream Theater", englishName: "Dream Theater" };
    // The server reads the tags and finds the band from them in the same
    // request (#699).
    await page.route("**/api/jobs/*/audio-tags", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ audio_tags: TAGS, artist: band }),
      }));
    await openStudio(page);
    await openTrack(page);

    await expect(page.locator("#np-artist")).toHaveText("Dream Theater", { timeout: 15000 });
    await expect.poll(async () => (await stored(page)).artist, { timeout: 15000 }).toEqual(band);
    await page.waitForTimeout(PAST_THE_WAIT_MS);
    expect(wiki).toEqual([]);
  });

  test("the box opened while the tags are read waits for them, then shows the band", async ({ page }) => {
    await stubWikimedia(page);
    let release;
    const hold = new Promise((resolve) => { release = resolve; });
    const asked = await stubAudioTags(page, TAGS, { hold });
    await openStudio(page);
    await openTrack(page);
    await expect.poll(() => timesAsked(asked), { timeout: 15000 }).toBe(1);

    await page.locator("#np-details-btn").click();
    await expect(page.locator(".artist-status")).toHaveText("Finding this track's artist…");
    await expect(page.locator("#artistSearch")).toBeHidden();
    await expect(page.locator(".artist-other-btn")).toHaveText("Search by name instead");

    release();
    await expect(page.locator(".artist-name")).toHaveText("Dream Theater", { timeout: 15000 });
    await expect(page.locator(".artist-save")).toHaveText("Saved for this track");
    await expect(page.locator("#artistSearch")).toBeHidden();

    // Not this band: the field, filled and focused.
    await page.locator(".artist-other-btn").click();
    await expect(page.locator("#artistSearch")).toBeVisible();
    await expect(page.locator("#artistQuery")).toHaveValue("Dream Theater");
    await expect(page.locator("#artistQuery")).toBeFocused();
  });

  test("a file with no tags is asked about once ever, and the box asks for a name", async ({ page }) => {
    const wiki = await stubWikimedia(page);
    const asked = await stubAudioTags(page, null);
    await openStudio(page);
    await openTrack(page);
    await expect.poll(async () => (await stored(page)).audioTagsChecked, { timeout: 15000 }).toBe(true);
    expect((await stored(page)).audioTags ?? null).toBeNull();
    await expect(page.locator("#np-artist")).toBeHidden();

    await page.locator("#np-details-btn").click();
    await expect(page.locator("#artistSearch")).toBeVisible();
    await expect(page.locator("#artistQuery")).toHaveValue("");
    await expect(page.locator("#artistQuery")).toBeFocused();
    await expect(page.locator(".artist-other-btn")).toHaveCount(0);
    await page.keyboard.press("Escape");

    await reloadKeepingLibrary(page);
    await openTrack(page);
    await page.waitForTimeout(PAST_THE_WAIT_MS);
    expect(timesAsked(asked)).toBe(1);
    expect(wiki).toEqual([]);
  });

  test("a failed read is not remembered, and the next session asks again", async ({ page }) => {
    await stubWikimedia(page);
    const asked = await stubAudioTags(page, null, { status: 500 });
    await openStudio(page);
    await openTrack(page);
    await expect.poll(() => timesAsked(asked), { timeout: 15000 }).toBe(1);
    await page.waitForTimeout(500);
    expect((await stored(page)).audioTagsChecked).toBeUndefined();

    // Once per session...
    await openTrack(page, SIBLING_JOB_ID);
    await openTrack(page);
    await page.waitForTimeout(500);
    expect(timesAsked(asked)).toBe(1);
    // ...and again in the next.
    await reloadKeepingLibrary(page);
    await openTrack(page);
    await expect.poll(() => timesAsked(asked), { timeout: 15000 }).toBe(2);
  });

  test("a read already under way on the server (409) is asked again on the next open", async ({ page }) => {
    await stubWikimedia(page);
    const asked = await stubAudioTags(page, null, { status: 409 });
    await openStudio(page);
    await openTrack(page);
    await expect.poll(() => timesAsked(asked), { timeout: 15000 }).toBe(1);
    await openTrack(page, SIBLING_JOB_ID);
    await openTrack(page);
    await expect.poll(() => timesAsked(asked), { timeout: 15000 }).toBe(2);
    expect((await stored(page)).audioTagsChecked).toBeUndefined();
  });

  test("tags landing after another track was opened stay on their own track", async ({ page }) => {
    const wiki = await stubWikimedia(page);
    let release;
    const hold = new Promise((resolve) => { release = resolve; });
    const asked = await stubAudioTags(page, (id) => (id === JOB_ID ? TAGS : null), { hold });
    await openStudio(page);
    await openTrack(page);
    await expect.poll(() => timesAsked(asked), { timeout: 15000 }).toBe(1);

    await openTrack(page, SIBLING_JOB_ID);
    await expect(page.locator("#title")).toHaveText("E2E Fixture Track (again)", { timeout: 15000 });
    release();

    await expect.poll(async () => (await stored(page)).audioTags, { timeout: 15000 }).toEqual(TAGS);
    await page.waitForTimeout(PAST_THE_WAIT_MS);
    // The open track's card is untouched, and no band was looked up for a
    // track that is no longer open.
    await expect(page.locator("#title")).toHaveText("E2E Fixture Track (again)");
    await expect(page.locator("#np-artist")).toBeHidden();
    expect(wiki).toEqual([]);
    expect((await stored(page)).artist ?? null).toBeNull();
    expect((await stored(page, SIBLING_JOB_ID)).artist ?? null).toBeNull();
  });

  test("the Lyrics tab, open while the tags arrive, looks them up", async ({ page }) => {
    await stubWikimedia(page);
    // The tab asks the server to look them up (#719), which has the tags the
    // read just stored; it is answered here with nothing found.
    const lrclib = [];
    await page.route(LYRICS_LOOKUP, (route) => {
      lrclib.push(route.request().url());
      return route.fulfill({ status: 404, json: { detail: "no lyrics", others: [] } });
    });
    let release;
    const hold = new Promise((resolve) => { release = resolve; });
    await stubAudioTags(page, TAGS, { hold });
    await openStudio(page);
    await openTrack(page);
    await page.locator(".rail-lyrics").click();
    await expect(page.locator("#lyricsStatus")).toContainText("no artist or song tags");
    expect(lrclib).toEqual([]);

    release();
    await expect.poll(() => lrclib.length, { timeout: 15000 }).toBeGreaterThan(0);
    await expect(page.locator("#lyricsStatus")).toContainText("No lyrics found");
  });
});
