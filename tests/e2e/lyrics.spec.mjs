// The Lyrics tab (#699): a view of the sidebar that finds the open track's
// lyrics by itself, from the file's tags or the band saved on the track, keeps
// them for the track, and shows them in time with playback, karaoke style.
// There is no search box.
//
// The tab asks the server to look lyrics up (#719), and the server's answer is
// given here (stubLyricsLookup), so the tests run offline. The fixture track
// is six seconds long, which is why the synced lines below sit inside it and
// why the first row below counts as the same recording. Which version the
// server keeps is tested against the server in tests/test_lyrics_lookup.py,
// LRC parsing and word timing in tests/js/lyrics-lookup.test.mjs; this is the
// page: what is asked and when, what is kept, what is shown.
import { test, expect } from "@playwright/test";
import {
  JOB_ID,
  SIBLING_JOB_ID,
  fixtureTrack,
  readCatalogState,
  seedCatalogState,
  seedLibrary,
  stubAudioTags,
  stubExportEndpoints,
  stubUpdateCheck,
  stubLyricsLookup,
  LYRICS_LOOKUP,
} from "./helpers.mjs";

const LRCLIB = LYRICS_LOOKUP;

const ROWS = [
  {
    id: 101,
    trackName: "Fixture Song",
    artistName: "Fixture Band",
    albumName: "Studio",
    duration: 6,
    instrumental: false,
    syncedLyrics: "[00:00.50] First line\n[00:02.00] Second line\n[00:04.00] Third line",
    plainLyrics: "First line\nSecond line\nThird line",
  },
  {
    id: 102,
    trackName: "Fixture Song (Live)",
    artistName: "Fixture Band",
    albumName: "Live",
    duration: 300,
    instrumental: false,
    syncedLyrics: null,
    plainLyrics: "Live words\nMore live words",
  },
];

const TAGS = { audioTags: { artist: "Fixture Band", title: "Fixture Song" } };

// The first row as another artist's or another song's, for a search that asks for those.
const rowAs = (artistName, trackName) => [{ ...ROWS[0], artistName, trackName }];

// The tagged artist and song unless a test says otherwise.
const stubLrclib = (page, options = {}) =>
  stubLyricsLookup(page, { rows: ROWS, artist: "Fixture Band", song: "Fixture Song", ...options });

/** What the server kept for the fixture track, as GET .../lyrics answers. */
const serverKept = (page) =>
  page.evaluate(async (id) => {
    const res = await fetch(`/api/jobs/${id}/lyrics`);
    return res.ok ? res.json() : null;
  }, JOB_ID);

/** Both fixture tracks, the first with `extra` fields (tags, a saved band). */
async function seedWith(page, extra) {
  await seedCatalogState(page, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: [JOB_ID, SIBLING_JOB_ID], color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks: {
      [JOB_ID]: { ...fixtureTrack(JOB_ID, "E2E Fixture Track"), ...extra },
      [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, "E2E Fixture Track (again)"),
    },
  });
}

// A tagged artist also sets off the artist box's own lookup (artistInfo.js).
// Answered with "no such band", so no test reaches Wikimedia and none gets a
// band saved behind its back.
const WIKIMEDIA = /^https:\/\/((www|query)\.wikidata\.org|[a-z-]+\.wikipedia\.org)\//;

async function open(page) {
  await stubExportEndpoints(page);
  await stubUpdateCheck(page);
  await page.route(WIKIMEDIA, (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    headers: { "access-control-allow-origin": "*" },
    body: JSON.stringify({ search: [] }),
  }));
  // An untagged track asks the server to read its tags when it is opened
  // (catalog.js). Answered with none, so an untagged fixture stays untagged.
  await stubAudioTags(page, null);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(".cat-item").first().waitFor({ timeout: 20000 });
}

async function openTrack(page) {
  await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
  await expect(page.locator(".app")).not.toHaveClass(/no-track/, { timeout: 15000 });
}

const showLyricsTab = (page) => page.locator(".rail-lyrics").click();

/** A tagged track, opened, with the tab showing its lyrics found. */
async function lyricsShown(page) {
  const asked = await stubLrclib(page);
  await seedWith(page, TAGS);
  await open(page);
  await openTrack(page);
  await showLyricsTab(page);
  await expect(page.locator(".lyrics-line")).toHaveCount(3);
  return asked;
}

const stored = (page) =>
  page.evaluate((id) => JSON.parse(localStorage.getItem(`stemdeck.lyrics.${id}`) || "null"), JOB_ID);

test.describe("lyrics tab", () => {
  test("the rail button swaps the library list for the lyrics panel, which has no search box", async ({ page }) => {
    await stubLrclib(page);
    await seedLibrary(page);
    await open(page);
    await showLyricsTab(page);
    await expect(page.locator("#lyricsPanel")).toBeVisible();
    await expect(page.locator("#catalogList")).toBeHidden();
    await expect(page.locator(".rail-lyrics")).toHaveAttribute("aria-pressed", "true");
    await expect(page.locator("#lyricsPanel input, #lyricsPanel form")).toHaveCount(0);

    await page.locator(".rail-library").click();
    await expect(page.locator("#lyricsPanel")).toBeHidden();
    await expect(page.locator("#catalogList")).toBeVisible();
  });

  test("with no track open it says to open one", async ({ page }) => {
    const asked = await stubLrclib(page);
    await seedLibrary(page);
    await open(page);
    await showLyricsTab(page);
    await expect(page.locator("#lyricsStatus")).toHaveText("Open a track to find its lyrics.");
    expect(asked).toEqual([]);
  });

  test("a track with no tags and no saved band says how to give it one, and asks nothing", async ({ page }) => {
    const asked = await stubLrclib(page);
    await seedLibrary(page);
    await open(page);
    await openTrack(page);
    await showLyricsTab(page);
    await expect(page.locator("#lyricsStatus")).toContainText("Save its band with the");
    expect(asked).toEqual([]);
  });

  test("the file's tags are looked up, and a version the track's length is kept", async ({ page }) => {
    const asked = await lyricsShown(page);
    await expect(page.locator(".lyrics-line")).toHaveText(["First line", "Second line", "Third line"]);
    await expect(page.locator(".lyrics-match-title")).toHaveText("Fixture Song");

    // One request, to the server, which looks the tags up itself (#719).
    expect(asked).toEqual([{ id: JOB_ID, body: {} }]);

    // Kept by the server, for this track, the way an import keeps them.
    expect((await serverKept(page))?.lrclib_id).toBe(101);
    expect(await stored(page)).toBeNull();

    // The other version is folded away until asked for, then can be taken.
    const toggle = page.locator(".lyrics-tools .lyrics-link").first();
    await expect(toggle).toHaveText("Other versions (1)");
    await expect(page.locator(".lyrics-version-list")).toBeHidden();
    await toggle.click();
    await page.locator(".lyrics-version", { hasText: "Fixture Song (Live)" }).click();
    await expect(page.locator(".lyrics-text").first()).toHaveText("Live words");
    await expect(page.locator(".lyrics-line")).toHaveCount(0);
  });

  test("a saved band is looked up with the song's name taken from the title", async ({ page }) => {
    const asked = await stubLrclib(page, { rows: rowAs("Dream Theater", "E2E Fixture Track"), artist: "Dream Theater", song: "E2E Fixture Track" });
    await seedWith(page, { artist: { id: "Q162586", name: "Dream Theater", englishName: "Dream Theater" } });
    await open(page);
    await openTrack(page);
    await showLyricsTab(page);
    await expect(page.locator(".lyrics-line")).toHaveCount(3);
    // The band lives in the studio's store, so it goes along with the request.
    expect(asked[0].body.band).toEqual({ id: "Q162586", name: "Dream Theater", englishName: "Dream Theater" });

    // Kept, so coming back to the tab asks nothing more.
    await page.locator(".rail-library").click();
    await showLyricsTab(page);
    await expect(page.locator(".lyrics-line")).toHaveCount(3);
    expect(asked).toHaveLength(1);
  });

  test("the library keeps only that a file has lyrics; the tab fetches them once", async ({ page }) => {
    const asked = await stubLrclib(page);
    await seedWith(page, { audioTags: { artist: "Fixture Band", title: "Fixture Song", hasLyrics: true } });
    // The server's state for the job carries the lyrics, as it does for an
    // upload whose file had a lyrics tag.
    await page.route(new RegExp(`/api/jobs/${JOB_ID}$`), async (route) => {
      const response = await route.fetch();
      const state = await response.json();
      state.audio_tags = {
        artist: "Fixture Band",
        title: "Fixture Song",
        lyrics: ["[00:00.50]Served one", "[00:02.00]Served two"].join("\n"),
      };
      await route.fulfill({ response, json: state });
    });
    await open(page);
    await openTrack(page);
    await showLyricsTab(page);
    await expect(page.locator(".lyrics-line")).toHaveText(["Served one", "Served two"]);
    expect(asked).toEqual([]);

    // The library store has the flag, never the text.
    const library = await readCatalogState(page);
    expect(library.tracks[JOB_ID].audioTags).toEqual({ artist: "Fixture Band", title: "Fixture Song", hasLyrics: true });
    expect(JSON.stringify(library)).not.toContain("Served one");
  });

  test("lyrics the file came with are shown, with nothing asked", async ({ page }) => {
    const asked = await stubLrclib(page);
    await seedWith(page, {
      audioTags: {
        artist: "Fixture Band",
        title: "Fixture Song",
        lyrics: ["[00:00.50]Embedded one", "[00:02.00]Embedded two"].join("\n"),
      },
    });
    await open(page);
    await openTrack(page);
    await showLyricsTab(page);
    await expect(page.locator(".lyrics-line")).toHaveText(["Embedded one", "Embedded two"]);
    await expect(page.locator(".lyrics-match-meta")).toContainText("From the file");
    expect(asked).toEqual([]);
  });

  test("with no version the track's length, they are offered and none is kept", async ({ page }) => {
    const asked = await stubLrclib(page, { rows: [ROWS[1]] });
    await seedWith(page, TAGS);
    await open(page);
    await openTrack(page);
    await showLyricsTab(page);
    await expect(page.locator("#lyricsStatus")).toContainText("Pick the right one");
    await expect(page.locator(".lyrics-version")).toHaveCount(1);
    await expect(page.locator(".lyrics-line")).toHaveCount(0);
    expect(await stored(page)).toBeNull();

    // The same offer again on coming back, without asking again.
    await page.locator(".rail-library").click();
    await showLyricsTab(page);
    await expect(page.locator(".lyrics-version")).toHaveCount(1);
    expect(asked).toHaveLength(1);

    await page.locator(".lyrics-version").click();
    await expect(page.locator(".lyrics-text").first()).toHaveText("Live words");
  });

  test("clicking a synced line moves the playhead there and marks it", async ({ page }) => {
    await lyricsShown(page);
    await page.locator(".lyrics-line", { hasText: "Third line" }).click();
    await expect(page.locator("#t-time")).toContainText("00:04");
    await expect(page.locator(".lyrics-line.current")).toHaveText("Third line");
  });

  test("a clicked line lets go of focus, so Space plays rather than pressing it again", async ({ page }) => {
    await lyricsShown(page);
    const isPlaying = () => page.evaluate(async () => (await import("/js/state.js")).audioEngine.isPlaying());
    const second = page.locator(".lyrics-line", { hasText: "Second line" });
    await second.click();
    await expect(page.locator("#t-time")).toContainText("00:02");
    await expect(second).not.toBeFocused();

    await page.keyboard.press("Space");
    await expect.poll(isPlaying).toBe(true);
    await page.keyboard.press("Space");
    await expect.poll(isPlaying).toBe(false);

    // From the keyboard, focus stays on the line, where its ring shows.
    const third = page.locator(".lyrics-line", { hasText: "Third line" });
    await third.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator("#t-time")).toContainText("00:04");
    await expect(third).toBeFocused();
    expect(await third.evaluate((el) => el.matches(":focus-visible"))).toBe(true);
  });

  test("the line being sung fills word by word, and the next is raised", async ({ page }) => {
    await lyricsShown(page);

    // Paused just after "Second line" starts: its first word is being sung.
    await page.locator(".lyrics-line", { hasText: "Second line" }).click();
    const current = page.locator(".lyrics-line.current");
    await expect(current).toHaveText("Second line");
    await expect(current.locator(".lw")).toHaveCount(2);
    await expect(current.locator(".lw").first()).toHaveClass(/singing/);
    await expect(current.locator(".lw").nth(1)).not.toHaveClass(/sung|singing/);
    await expect(page.locator(".lyrics-line.next")).toHaveText("Third line");

    // Moving on leaves no fill behind on the line before.
    await page.locator(".lyrics-line", { hasText: "Third line" }).click();
    await expect(page.locator(".lyrics-line", { hasText: "Second line" }).locator(".lw.sung, .lw.singing"))
      .toHaveCount(0);
  });

  test("the wipe waits for the singer: no fill while the vocals stem is silent", async ({ page }) => {
    // The fixture's vocals are a steady tone, so the real envelope would have
    // the singing start on the stamp. This one has the voice come in 0.6s
    // after "Second line" is stamped, as a singer who is late on it would.
    const hop = 0.04;
    const db = Array.from({ length: 150 }, (_, i) => (i * hop >= 2.6 && i * hop < 3.6 ? -15 : -90));
    const envelopeUrl = new RegExp(`/api/jobs/${JOB_ID}/vocal-envelope$`);
    await page.route(envelopeUrl, (route) => route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ hop, db }),
    }));
    const asked = page.waitForRequest(envelopeUrl);
    await lyricsShown(page);
    await asked;

    await page.locator(".lyrics-line", { hasText: "Second line" }).click();
    const current = page.locator(".lyrics-line.current");
    await expect(current).toHaveText("Second line");
    // Paused on the stamp, before the voice: the line is current, nothing filled.
    await expect(current.locator(".lw.sung, .lw.singing")).toHaveCount(0);
  });

  test("kept lyrics come back after a reload, with nothing asked", async ({ page }) => {
    const asked = await lyricsShown(page);
    // The library is re-seeded on every load; the lyrics have a store entry of
    // their own, so they survive it.
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(".cat-item").first().waitFor({ timeout: 20000 });
    asked.length = 0;
    await openTrack(page);
    await showLyricsTab(page);
    await expect(page.locator(".lyrics-line")).toHaveCount(3);
    expect(asked).toEqual([]);
  });

  test("removed lyrics stay removed until looked up again", async ({ page }) => {
    const asked = await lyricsShown(page);
    await page.locator(".lyrics-tools .lyrics-link", { hasText: "Remove lyrics" }).click();
    await expect(page.locator(".lyrics-line")).toHaveCount(0);
    await expect(page.locator("#lyricsStatus")).toHaveText("Lyrics removed for this track.");
    await expect.poll(() => stored(page)).toEqual({ dismissed: true });

    // The lookup runs by itself, and must not bring them straight back.
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(".cat-item").first().waitFor({ timeout: 20000 });
    await openTrack(page);
    await showLyricsTab(page);
    await expect(page.locator("#lyricsStatus")).toHaveText("Lyrics removed for this track.");
    expect(asked).toHaveLength(1);

    // Looking up again finds what the server kept, with nothing more asked.
    await page.locator(".lyrics-link", { hasText: "Look up again" }).click();
    await expect(page.locator(".lyrics-line")).toHaveCount(3);
    expect(asked).toHaveLength(1);
  });

  test("nothing found says so", async ({ page }) => {
    await stubLrclib(page, { rows: [] });
    await seedWith(page, TAGS);
    await open(page);
    await openTrack(page);
    await showLyricsTab(page);
    await expect(page.locator("#lyricsStatus")).toHaveText("No lyrics found for “Fixture Song”.");
  });

  test("no connection says so, and the next opening tries again", async ({ page }) => {
    const asked = await stubLrclib(page, { offline: true });
    await seedWith(page, TAGS);
    await open(page);
    await openTrack(page);
    await showLyricsTab(page);
    await expect(page.locator("#lyricsStatus")).toHaveText(
      "Could not reach LRCLIB. Check your connection and try again.",
    );
    // An error status stays in the tab, not floated over the top bar the way
    // the error banner's rules once took every element marked "error".
    await expect(page.locator("#lyricsStatus")).toHaveClass(/error/);
    expect(await page.locator("#lyricsStatus").evaluate((el) => getComputedStyle(el).position)).toBe("static");
    const tab = await page.locator("#lyricsStatus").evaluate((el) => {
      const own = el.getBoundingClientRect();
      const side = el.closest("aside").getBoundingClientRect();
      return own.left >= side.left && own.right <= side.right;
    });
    expect(tab).toBe(true);

    await page.unroute(LRCLIB);
    await stubLrclib(page);
    await page.locator(".rail-library").click();
    await showLyricsTab(page);
    await expect(page.locator(".lyrics-line")).toHaveCount(3);
    expect(asked).toHaveLength(1);
  });

  test("no connection offers a way to try again in place", async ({ page }) => {
    await stubLrclib(page, { offline: true });
    await seedWith(page, TAGS);
    await open(page);
    await openTrack(page);
    await showLyricsTab(page);
    const again = page.locator(".lyrics-link", { hasText: "Look up again" });
    await expect(again).toBeVisible();

    await page.unroute(LRCLIB);
    const asked = await stubLrclib(page);
    await again.click();
    await expect(page.locator(".lyrics-line")).toHaveCount(3);
    expect(asked).toHaveLength(1);
  });

  test("a tagged artist with no tagged title is looked up with the song from the title", async ({ page }) => {
    // The server takes the song from the title (build_query); the page only
    // has to ask, which it does even with no tagged title.
    const asked = await stubLrclib(page, { rows: rowAs("Fixture Band", "E2E Fixture Track"), song: "E2E Fixture Track" });
    await seedWith(page, { audioTags: { artist: "Fixture Band" } });
    await open(page);
    await openTrack(page);
    await showLyricsTab(page);
    await expect(page.locator(".lyrics-line")).toHaveCount(3);
    expect(asked).toHaveLength(1);
  });

  test("another artist's song of the same name is not this track's lyrics", async ({ page }) => {
    await stubLrclib(page, { rows: [...rowAs("Someone Else", "Fixture Song"), ...rowAs("Fixture Band", "Another Song")] });
    await seedWith(page, TAGS);
    await open(page);
    await openTrack(page);
    await showLyricsTab(page);
    await expect(page.locator(".lyrics-status")).toHaveText("No lyrics found for “Fixture Song”.");
    await expect(page.locator(".lyrics-line")).toHaveCount(0);
    await expect(page.locator(".lyrics-version")).toHaveCount(0);
  });
});

test.describe("lyrics kept before a lookup was held to the track", () => {
  test("another song's lyrics kept by the tab are dropped and the track looked up again", async ({ page }) => {
    // Kept by an older tab from a fuzzy search: NIHIL "Barro" once got
    // Renato Vianna's "Joao de Barro".
    await page.addInitScript((id) => {
      localStorage.setItem(`stemdeck.lyrics.${id}`, JSON.stringify({
        entry: { v: 1, source: "lrclib", id: 9, track: "Another Song", artist: "Someone Else", album: "", duration: 6, instrumental: false, synced: "[00:00.50]Not this song", plain: "" },
        others: [],
      }));
    }, JOB_ID);
    const asked = await stubLrclib(page);
    await seedWith(page, TAGS);
    await open(page);
    await openTrack(page);
    await showLyricsTab(page);
    await expect(page.locator(".lyrics-line")).toHaveCount(3);
    await expect(page.locator(".lyrics-body")).not.toContainText("Not this song");
    expect(asked.length).toBeGreaterThan(0);
  });
});
