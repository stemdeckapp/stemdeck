// The karaoke wipe in Chinese, Japanese and Korean: how their lines are laid
// out in pieces for it, and that it fills them in time, from the line's own
// timing and from enhanced LRC's word stamps.
//
// Chinese and Japanese are written without spaces between words, so the wipe
// goes a character at a time, with punctuation kept beside its character, a
// Latin word in a Japanese line whole, and a small kana with the one it bends
// ("しゃ"). Korean is spaced, and goes a word at a time. How the pieces are
// cut and timed is covered in tests/js/lyrics-lookup.test.mjs; this is the
// page drawing and filling them.
//
// The lyrics are the server's (GET /api/jobs/{id}/lyrics), as
// lyrics-from-server.spec.mjs gives them, and the vocals envelope is absent, so
// the timing is the line's own and does not depend on the fixture's audio.
//
// Last, the tab's own lookup (no lyrics from the server) finding a song LRCLIB
// files only under the band's name in its own script.
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

const LINES = {
  zh: "故事的小黄花 从出生那年就飘着",
  ja: "夜に駆ける",
  mixed: "「夢ならば」Lemon しゃべらない",
  ko: "나는 너를 사랑해요",
};

const SYNCED = [
  `[00:00.50]${LINES.zh}`,
  // Enhanced LRC: each piece stamped when it is sung.
  "[00:02.00]<00:02.00>夜<00:02.40>に<00:02.80>駆ける",
  `[00:03.50]${LINES.mixed}`,
  `[00:05.00]${LINES.ko}`,
].join("\n");

const SERVER = {
  v: 1,
  source: "lrclib",
  track: "晴天",
  artist: "周杰倫",
  album: "葉惠美",
  duration: 6,
  synced: SYNCED,
  plain: Object.values(LINES).join("\n"),
  instrumental: false,
  lrclib_id: 301,
  others: [],
};

async function setUp(page, { server = SERVER, lrclibRows = null, extra = {} } = {}) {
  const lrclib = [];
  await page.route(LRCLIB, (route) => {
    const url = new URL(route.request().url());
    lrclib.push(url.searchParams.get("artist_name") || `q:${url.searchParams.get("q")}`);
    if (!lrclibRows) return route.abort("internetdisconnected");
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "access-control-allow-origin": "*" },
      body: JSON.stringify(lrclibRows(url.searchParams.get("artist_name"))),
    });
  });
  await page.route(`**/api/jobs/${JOB_ID}/lyrics`, (route) => (server
    ? route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(server) })
    : route.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"no lyrics"}' })));
  await page.route(new RegExp(`/api/jobs/${JOB_ID}/vocal-envelope$`), (route) =>
    route.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"no vocals"}' }));
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
        audioTags: { artist: "Jay Chou", title: "晴天" },
        ...extra,
      },
      [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, "E2E Fixture Track (again)"),
    },
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(".cat-item").first().waitFor({ timeout: 20000 });
  await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
  await expect(page.locator(".app")).not.toHaveClass(/no-track/, { timeout: 15000 });
  await page.locator(".rail-lyrics").click();
  if (server) await expect(page.locator(".lyrics-line")).toHaveCount(4);
  return { lrclib };
}

const line = (page, text) => page.locator(".lyrics-line", { hasText: text });
const pieces = (locator) => locator.locator(".lw").allTextContents();
const seek = (page, seconds) =>
  page.evaluate(async (t) => (await import("/js/transport.js")).setPlayheadTime(t), seconds);

test.describe("karaoke in Chinese, Japanese and Korean", () => {
  test("each line is drawn whole, in the pieces the wipe fills", async ({ page }) => {
    const { lrclib } = await setUp(page);
    await expect(page.locator(".lyrics-line")).toHaveText([LINES.zh, LINES.ja, LINES.mixed, LINES.ko]);
    expect(await pieces(line(page, "故事"))).toEqual(
      ["故", "事", "的", "小", "黄", "花 ", "从", "出", "生", "那", "年", "就", "飘", "着"],
    );
    expect(await pieces(line(page, "夜に"))).toEqual(["夜", "に", "駆ける"]);
    expect(await pieces(line(page, "Lemon"))).toEqual(
      ["「夢", "な", "ら", "ば」", "Lemon ", "しゃ", "べ", "ら", "な", "い"],
    );
    expect(await pieces(line(page, "사랑해요"))).toEqual(["나는 ", "너를 ", "사랑해요"]);
    await expect(page.locator(".lyrics-match-meta")).toHaveText("周杰倫 · 葉惠美");
    expect(lrclib).toEqual([]);
  });

  test("a Chinese line fills a character at a time", async ({ page }) => {
    await setUp(page);
    const zh = line(page, "故事");
    await zh.click();
    await expect(zh).toHaveClass(/current/);
    await expect(zh.locator(".lw").first()).toHaveClass(/singing/);
    await expect(zh.locator(".lw.sung")).toHaveCount(0);

    // Part way through: the characters before sung, one being sung, the rest waiting.
    await seek(page, 1.0);
    // Waited for, not read at once: the wipe catches up on its next frame.
    await expect.poll(() => zh.locator(".lw.sung").count()).toBeGreaterThan(1);
    await expect(zh.locator(".lw.singing")).toHaveCount(1);
    const sung = await zh.locator(".lw.sung").count();
    expect(sung).toBeLessThan(13);
    await expect(zh.locator(".lw").nth(sung)).toHaveClass(/singing/);
  });

  test("enhanced LRC's word stamps time the Japanese wipe exactly", async ({ page }) => {
    await setUp(page);
    const ja = line(page, "夜に");
    await ja.click();
    await expect(ja).toHaveClass(/current/);
    const words = ja.locator(".lw");
    await expect(words.nth(0)).toHaveClass(/singing/);
    await expect(words.nth(1)).not.toHaveClass(/sung|singing/);

    // Between "に" (2.40) and "駆ける" (2.80).
    await seek(page, 2.5);
    await expect(words.nth(0)).toHaveClass(/sung/);
    await expect(words.nth(1)).toHaveClass(/singing/);
    await expect(words.nth(2)).not.toHaveClass(/sung|singing/);
  });

  test("a Korean line fills a word at a time, and the line before lets go", async ({ page }) => {
    await setUp(page);
    const ko = line(page, "사랑해요");
    await ko.click();
    await expect(ko).toHaveClass(/current/);
    await expect(ko.locator(".lw").first()).toHaveClass(/singing/);
    await expect(ko.locator(".lw").last()).not.toHaveClass(/sung|singing/);
    await expect(line(page, "Lemon").locator(".lw.sung, .lw.singing")).toHaveCount(0);
  });

  test("each line says its language, and Korean wraps between words", async ({ page }) => {
    await setUp(page);
    await expect(line(page, "故事的小黄花")).toHaveAttribute("lang", "zh");
    await expect(line(page, "駆ける")).toHaveAttribute("lang", "ja");
    await expect(line(page, "Lemon")).toHaveAttribute("lang", "ja");
    const ko = line(page, "사랑해요");
    await expect(ko).toHaveAttribute("lang", "ko");
    expect(await ko.evaluate((el) => getComputedStyle(el).wordBreak)).toBe("keep-all");
  });

  test("the tab finds a song LRCLIB files under the band's name in its own script", async ({ page }) => {
    // Tagged "Jay Chou"; LRCLIB has the song only as 周杰倫's, and the artist
    // box saved the band with both names. Another singer's 晴天 is never taken.
    const rows = (artist) => (artist === "周杰倫"
      ? [{ id: 401, trackName: "晴天", artistName: "周杰倫", albumName: "葉惠美", duration: 6, instrumental: false, syncedLyrics: SYNCED, plainLyrics: "" }]
      : [{ id: 402, trackName: "晴天", artistName: "张信哲", albumName: "", duration: 6, instrumental: false, syncedLyrics: "[00:00.50]not his", plainLyrics: "" }]);
    const { lrclib } = await setUp(page, {
      server: null,
      lrclibRows: rows,
      extra: { artist: { id: "Q238819", name: "周杰倫", englishName: "Jay Chou" } },
    });
    await expect(page.locator(".lyrics-line")).toHaveCount(4);
    await expect(page.locator(".lyrics-match-meta")).toHaveText("周杰倫 · 葉惠美");
    expect(lrclib).toEqual(["Jay Chou", "周杰倫"]);
  });
});
