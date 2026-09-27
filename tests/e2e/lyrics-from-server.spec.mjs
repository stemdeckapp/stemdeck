// The Lyrics tab with lyrics the server found while the track was separated
// (GET /api/jobs/{id}/lyrics, lyrics.json): shown straight away, with nothing
// asked of LRCLIB by the browser. A version the user picked for the track
// still wins over them, and one picked from the server's others is kept as
// the user's own choice.
//
// When LRCLIB had versions but none the track's length, the server keeps none
// and its 404 carries them as `others`, which the tab offers to pick from.
//
// The server's answer is given here, as the tag backfill's is elsewhere
// (stubAudioTags): its lookup, file and endpoint are covered in
// tests/test_lyrics_lookup.py and tests/test_lyrics_api.py.
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

const version = (id, extra = {}) => ({
  v: 1,
  source: "lrclib",
  track: "Fixture Song",
  artist: "Fixture Band",
  album: "Studio",
  duration: 6,
  synced: "[00:00.50] Server one\n[00:02.00] Server two\n[00:04.00] Server three",
  plain: "Server one\nServer two\nServer three",
  instrumental: false,
  lrclib_id: id,
  ...extra,
});

const SERVER = {
  ...version(201),
  others: [
    version(202, {
      album: "Live",
      duration: 300,
      synced: "",
      plain: "Live words\nMore live words",
    }),
  ],
};

async function setUp(page, { server = SERVER, saved = null } = {}) {
  const lrclib = [];
  await page.route(LRCLIB, (route) => {
    lrclib.push(route.request().url());
    return route.abort("internetdisconnected");
  });
  const served = [];
  await page.route(`**/api/jobs/${JOB_ID}/lyrics`, (route) => {
    served.push(route.request().url());
    if (!server) return route.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"no lyrics"}' });
    // Versions but no lyrics kept: the server's 404 carries them.
    const status = server.source ? 200 : 404;
    return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(server) });
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
      ([key, value]) => window.localStorage.setItem(key, JSON.stringify(value)),
      [`stemdeck.lyrics.${JOB_ID}`, saved],
    );
  }
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(".cat-item").first().waitFor({ timeout: 20000 });
  await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
  await expect(page.locator(".app")).not.toHaveClass(/no-track/, { timeout: 15000 });
  await page.locator(".rail-lyrics").click();
  return { lrclib, served };
}

const stored = (page) =>
  page.evaluate((id) => JSON.parse(localStorage.getItem(`stemdeck.lyrics.${id}`) || "null"), JOB_ID);

test.describe("lyrics from the server", () => {
  test("are shown with nothing asked of LRCLIB, and not saved as the user's choice", async ({ page }) => {
    const { lrclib, served } = await setUp(page);
    await expect(page.locator(".lyrics-line")).toHaveText(["Server one", "Server two", "Server three"]);
    await expect(page.locator(".lyrics-match-title")).toHaveText("Fixture Song");
    await expect(page.locator(".lyrics-match-meta")).toHaveText("Fixture Band · Studio");
    await expect(page.locator(".lyrics-tools .lyrics-link").first()).toHaveText("Other versions (1)");
    expect(served.length).toBeGreaterThan(0);
    expect(lrclib).toEqual([]);
    expect(await stored(page)).toBeNull();
  });

  test("a version picked from the server's others is kept as the user's choice", async ({ page }) => {
    const { lrclib } = await setUp(page);
    await page.locator(".lyrics-tools .lyrics-link").first().click();
    await page.locator(".lyrics-version", { hasText: "Live" }).click();
    await expect(page.locator(".lyrics-text").first()).toHaveText("Live words");
    const kept = await stored(page);
    expect(kept.entry.id).toBe(202);
    expect(kept.others.map((m) => m.id)).toEqual([201]);

    // And it wins over the server's from then on.
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    await page.locator(".rail-lyrics").click();
    await expect(page.locator(".lyrics-text").first()).toHaveText("Live words");
    expect(lrclib).toEqual([]);
  });

  test("the user's saved choice wins over the server's", async ({ page }) => {
    const saved = {
      entry: {
        v: 1,
        source: "lrclib",
        id: 77,
        track: "Fixture Song",
        artist: "Fixture Band",
        album: "Chosen",
        duration: 6,
        instrumental: false,
        synced: "[00:00.50] Chosen one\n[00:02.00] Chosen two",
        plain: "",
        savedAt: 1,
      },
      others: [],
    };
    const { lrclib } = await setUp(page, { saved });
    await expect(page.locator(".lyrics-line")).toHaveText(["Chosen one", "Chosen two"]);
    await expect(page.locator(".lyrics-match-meta")).toHaveText("Fixture Band · Chosen");
    expect(lrclib).toEqual([]);
  });

  test("a transcription is labelled as one", async ({ page }) => {
    const whisper = {
      ...version(null, { source: "whisper", album: "", synced: "[00:00.50]<00:00.50>Heard <00:01.00>words", plain: "Heard words" }),
      others: [],
    };
    await setUp(page, { server: whisper });
    await expect(page.locator(".lyrics-line")).toHaveText(["Heard words"]);
    await expect(page.locator(".lyrics-match-meta")).toHaveText("Fixture Band · Transcribed");
    await expect(page.locator(".lyrics-tools .lyrics-link")).toHaveText(["Remove lyrics"]);
  });

  test("Polish is shown letter for letter, a word to a span, even when it came decomposed", async ({ page }) => {
    // The second line arrives as a Mac can type it: each accent a mark of its own.
    const polish = {
      ...version(203, {
        track: "Małomiasteczkowy",
        artist: "Dawid Podsiadło",
        album: "Małomiasteczkowy",
        synced: `[00:00.50]Małomiasteczkowa głowa\n[00:02.00]${"Śpiewałem głośno pod prysznicem".normalize("NFD")}\n[00:04.00]Żółć, gęś, źdźbło`,
        plain: "",
      }),
      others: [],
    };
    await setUp(page, { server: polish });
    await expect(page.locator(".lyrics-line")).toHaveText([
      "Małomiasteczkowa głowa",
      "Śpiewałem głośno pod prysznicem",
      "Żółć, gęś, źdźbło",
    ]);
    await expect(page.locator(".lyrics-match-title")).toHaveText("Małomiasteczkowy");
    await expect(page.locator(".lyrics-match-meta")).toHaveText("Dawid Podsiadło · Małomiasteczkowy");
    const spans = await page.locator(".lyrics-line").nth(1).locator(".lw").allTextContents();
    expect(spans).toEqual(["Śpiewałem ", "głośno ", "pod ", "prysznicem"]);
    expect(spans.join("").length).toBe(31); // 33 decomposed
  });

  test("plain Polish lyrics are shown letter for letter", async ({ page }) => {
    const plain = { ...version(204, { synced: "", plain: "Źdźbło trawy\nZażółć gęślą jaźń".normalize("NFD") }), others: [] };
    await setUp(page, { server: plain });
    await expect(page.locator(".lyrics-text")).toHaveText(["Źdźbło trawy", "Zażółć gęślą jaźń"]);
  });

  test("versions the server kept none of are offered to pick from, with nothing asked of LRCLIB", async ({ page }) => {
    const { lrclib } = await setUp(page, { server: { detail: "no lyrics", others: SERVER.others } });
    await expect(page.locator("#lyricsStatus")).toContainText("Pick the right one");
    await expect(page.locator(".lyrics-version")).toHaveCount(1);
    await expect(page.locator(".lyrics-line")).toHaveCount(0);
    await page.locator(".lyrics-version").click();
    await expect(page.locator(".lyrics-text").first()).toHaveText("Live words");
    expect((await stored(page)).entry.id).toBe(202);
    expect(lrclib).toEqual([]);
  });

  test("with none on the server, the tab looks them up itself as before", async ({ page }) => {
    const { lrclib, served } = await setUp(page, { server: null });
    await expect.poll(() => lrclib.length).toBeGreaterThan(0);
    expect(served.length).toBeGreaterThan(0);
    await expect(page.locator("#lyricsStatus")).toContainText("Could not reach LRCLIB");
  });
});
