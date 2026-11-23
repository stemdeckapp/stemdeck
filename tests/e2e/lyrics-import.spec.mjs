// The Lyrics tab's Import .lrc and Replace lyrics (#1083): a .lrc or .txt the
// user picks is read in the browser (UTF-8, else GB18030), posted as text to
// POST .../lyrics/import, and shown as the file's own lyrics.
//
// The server's endpoints are stood in for here, so a test never leaves
// lyrics.json behind for the specs after it: what the server keeps of the
// text (tags, NetEase credit lines, the lock) is covered in
// tests/test_lyrics_api.py, the decoding and the limits in
// tests/js/lyrics-file.test.mjs. This is the page: what it posts, what it
// shows, when it asks first, and what each refusal says.
import { test, expect } from "@playwright/test";
import {
  JOB_ID,
  SIBLING_JOB_ID,
  fixtureTrack,
  seedCatalogState,
  stubAudioTags,
  stubExportEndpoints,
  stubUpdateCheck,
  LYRICS_LOOKUP,
} from "./helpers.mjs";

const WIKIMEDIA = /^https:\/\/((www|query)\.wikidata\.org|[a-z-]+\.wikipedia\.org)\//;

const fileEntry = (extra = {}) => ({
  v: 1,
  source: "file",
  track: "E2E Fixture Track",
  artist: "",
  album: "",
  duration: 6,
  synced: "",
  plain: "",
  instrumental: false,
  timing: "exact",
  others: [],
  lrclib_id: null,
  ...extra,
});

const LRCLIB_KEPT = {
  ...fileEntry(),
  source: "lrclib",
  track: "Fixture Song",
  artist: "Fixture Band",
  synced: "[00:00.50] Old one\n[00:02.00] Old two",
  plain: "Old one\nOld two",
  lrclib_id: 201,
};

/**
 * Open the fixture track's Lyrics tab with `server` as its lyrics.json (null
 * for none). `answer(text)` is what POST .../lyrics/import answers for the
 * posted text: { status, body }. Returns the texts posted and what LRCLIB
 * was asked.
 */
async function setUp(page, { server = null, answer = null, hold = null } = {}) {
  const kept = { lyrics: server };
  const posted = [];
  const lookups = [];
  await page.route(LYRICS_LOOKUP, (route) => {
    lookups.push(route.request().url());
    return route.fulfill({ status: 404, json: { detail: "nothing to look up", others: [], nothing_known: true } });
  });
  await page.route(`**/api/jobs/${JOB_ID}/lyrics`, (route) => {
    if (!kept.lyrics) return route.fulfill({ status: 404, json: { detail: "no lyrics" } });
    return route.fulfill({ status: 200, json: kept.lyrics });
  });
  await page.route(`**/api/jobs/${JOB_ID}/lyrics/import`, async (route) => {
    const { text } = route.request().postDataJSON();
    posted.push(text);
    // A slow server: the test says when it answers.
    if (hold) await hold;
    const timed = /^\s*\[\d{1,3}:\d{1,2}/m.test(text);
    const reply = answer?.(text) ?? {
      status: 200,
      body: fileEntry(timed ? { synced: text.trim() } : { plain: text.trim() }),
    };
    if (reply.status === 200) kept.lyrics = reply.body;
    return route.fulfill({ status: reply.status, json: reply.body });
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
  // No tags: the tab has nothing to look up, says so, and offers the import.
  await seedCatalogState(page, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: [JOB_ID, SIBLING_JOB_ID], color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks: {
      [JOB_ID]: fixtureTrack(JOB_ID, "E2E Fixture Track"),
      [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, "E2E Fixture Track (again)"),
    },
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(".cat-item").first().waitFor({ timeout: 20000 });
  await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
  await expect(page.locator(".app")).not.toHaveClass(/no-track/, { timeout: 15000 });
  await page.locator(".rail-lyrics").click();
  await expect(importLink(page)).toBeVisible();
  return { posted, lookups, kept };
}

const importLink = (page) => page.locator("#lyricsPanel .lyrics-import");
const status = (page) => page.locator("#lyricsStatus");

/** Press Import (or Replace) and pick `file`: { name, buffer }. */
async function pick(page, file) {
  const chooser = page.waitForEvent("filechooser");
  await importLink(page).click();
  await (await chooser).setFiles({ mimeType: "text/plain", ...file });
}

const stored = (page) =>
  page.evaluate((id) => JSON.parse(localStorage.getItem(`stemdeck.lyrics.${id}`) || "null"), JOB_ID);

test.describe("importing lyrics from a file", () => {
  test("a timed .lrc is posted as its text and shown as the file's own lyrics", async ({ page }) => {
    const { posted, lookups } = await setUp(page);
    await expect(importLink(page)).toHaveText("Import .lrc…");
    const text = "[00:00.50]First line\n[00:02.00]Second line\n[00:04.00]Third line";
    await pick(page, { name: "song.lrc", buffer: Buffer.from(text, "utf8") });
    await expect(page.locator(".lyrics-line")).toHaveText(["First line", "Second line", "Third line"]);
    await expect(page.locator(".lyrics-match-meta")).toHaveText("From the file");
    expect(posted).toEqual([text]);
    expect(lookups).toEqual([]);
    // Kept on the server, not as this browser's own choice.
    expect(await stored(page)).toBeNull();
    // The song has lyrics now: the same button replaces them.
    await expect(importLink(page)).toHaveText("Replace lyrics…");
  });

  test("a .lrc saved as GB18030 on a Chinese Windows is posted as Chinese", async ({ page }) => {
    const { posted } = await setUp(page);
    // [00:00.50]你好世界 in GB18030: not valid UTF-8.
    const gb = Buffer.concat([Buffer.from("[00:00.50]", "ascii"), Buffer.from([0xc4, 0xe3, 0xba, 0xc3, 0xca, 0xc0, 0xbd, 0xe7])]);
    await pick(page, { name: "song.lrc", buffer: gb });
    await expect(page.locator(".lyrics-line")).toHaveText(["你好世界"]);
    expect(posted).toEqual(["[00:00.50]你好世界"]);
  });

  test("a .txt with no timings is shown as text, with Align and Sync lines unavailable", async ({ page }) => {
    await setUp(page);
    await pick(page, { name: "song.txt", buffer: Buffer.from("Just words\nSecond line", "utf8") });
    await expect(page.locator(".lyrics-text")).toHaveText(["Just words", "Second line"]);
    await expect(status(page)).toContainText("Imported as text only");
    // As for plain lyrics from anywhere: nothing to align or sync.
    await expect(page.locator(".lyrics-tools .lyrics-align-toggle")).toHaveAttribute("aria-disabled", "true");
    await expect(page.locator(".lyrics-tools .lyrics-sync-toggle")).toHaveAttribute("aria-disabled", "true");
  });

  test("replacing lyrics with an Align offset says what goes, and picks a file on the second press", async ({ page }) => {
    const { posted } = await setUp(page, { server: { ...LRCLIB_KEPT, offset_sec: 1.5 } });
    await expect(importLink(page)).toHaveText("Replace lyrics…");
    let chosen = 0;
    page.on("filechooser", () => chosen++);
    await importLink(page).click();
    await expect(importLink(page)).toHaveText("Replace? Also removed: your Align offset. Press again");
    await expect(importLink(page)).toHaveClass(/armed/);
    expect(chosen).toBe(0);
    await pick(page, { name: "new.lrc", buffer: Buffer.from("[00:01.00]New words", "utf8") });
    await expect(page.locator(".lyrics-line")).toHaveText(["New words"]);
    expect(posted).toEqual(["[00:01.00]New words"]);
  });

  test("lyrics nobody worked on are replaced on the first press", async ({ page }) => {
    const { posted } = await setUp(page, { server: LRCLIB_KEPT });
    await pick(page, { name: "new.lrc", buffer: Buffer.from("[00:01.00]New words", "utf8") });
    await expect(page.locator(".lyrics-line")).toHaveText(["New words"]);
    expect(posted).toHaveLength(1);
  });

  test("each refusal says why, in its own words", async ({ page }) => {
    const replies = [
      { status: 409, body: { detail: "job is not finished" }, says: "still being processed" },
      { status: 422, body: { detail: "no lyrics in the text" }, says: "has no lyrics StemDeck can show" },
      { status: 404, body: { detail: "job not found" }, says: "no longer in the library" },
      { status: 500, body: { detail: "could not save lyrics" }, says: "Could not save the lyrics" },
    ];
    let next = 0;
    await setUp(page, { answer: () => replies[next] });
    for (; next < replies.length; next++) {
      await pick(page, { name: "song.lrc", buffer: Buffer.from("[00:01.00]words", "utf8") });
      await expect(status(page)).toContainText(replies[next].says);
    }
  });

  test("a file the page can tell is wrong is never posted", async ({ page }) => {
    const { posted } = await setUp(page);
    await pick(page, { name: "empty.lrc", buffer: Buffer.alloc(0) });
    await expect(status(page)).toHaveText("That file has no lyrics in it.");
    await pick(page, { name: "cover.lrc", buffer: Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0xff, 0x80]) });
    await expect(status(page)).toContainText("That file is not text.");
    // 100,001 characters, three bytes each: within the size a file may have,
    // past the characters the server keeps.
    await pick(page, { name: "long.lrc", buffer: Buffer.from("字".repeat(100_001), "utf8") });
    await expect(status(page)).toHaveText("That file is too long for lyrics: 100,000 characters at most.");
    expect(posted).toEqual([]);
  });

  test("the Guide explains the button, and that replacing worked-on lyrics asks twice", async ({ page }) => {
    await setUp(page);
    await page.locator("#helpModeBtn").click();
    await expect(page.locator("body")).toHaveClass(/help-mode/);
    await importLink(page).hover();
    await expect(page.locator("#helpTip")).toContainText("Uses the lyrics in a .lrc or .txt file you choose");
    await expect(page.locator("#helpTip")).toContainText("press once, then press again to confirm: these go with the old lyrics");
  });

  test("the armed Replace is said in the live status line too, and put back when it disarms", async ({ page }) => {
    await setUp(page, { server: { ...LRCLIB_KEPT, user_synced: "[00:00.40] Old one\n[00:02.10] Old two" } });
    await expect(status(page)).toHaveAttribute("aria-live", "polite");
    await importLink(page).click();
    const sentence = "Replace? Also removed: your own line timing. Press again";
    await expect(importLink(page)).toHaveText(sentence);
    await expect(status(page)).toHaveText(sentence);
    // Disarmed by itself after 10 s: the button and the status as they were.
    await expect(importLink(page)).toHaveText("Replace lyrics…", { timeout: 12_000 });
    await expect(importLink(page)).not.toHaveClass(/armed/);
    await expect(status(page)).toHaveText("");
  });

  test("the armed Replace disarms when focus leaves it", async ({ page }) => {
    await setUp(page, { server: { ...LRCLIB_KEPT, offset_sec: 1.5 } });
    await importLink(page).focus();
    await page.keyboard.press("Enter");
    await expect(importLink(page)).toHaveClass(/armed/);
    await page.keyboard.press("Tab");
    await expect(importLink(page)).not.toHaveClass(/armed/);
    await expect(importLink(page)).toHaveText("Replace lyrics…");
  });

  test("Align and Sync lines are closed and disabled while an import is on its way", async ({ page }) => {
    let release;
    const hold = new Promise((resolve) => { release = resolve; });
    await setUp(page, { server: LRCLIB_KEPT, hold });
    const alignToggle = page.locator(".lyrics-tools .lyrics-align-toggle");
    const syncToggle = page.locator(".lyrics-tools .lyrics-sync-toggle");
    await alignToggle.click();
    await expect(page.locator("#lyricsAlign")).toBeVisible();
    await pick(page, { name: "new.lrc", buffer: Buffer.from("[00:01.00]New words", "utf8") });
    await expect(status(page)).toHaveText("Importing lyrics…");
    await expect(page.locator("#lyricsAlign")).toHaveCount(0);
    await expect(alignToggle).toBeDisabled();
    await expect(syncToggle).toBeDisabled();
    // A second press says why nothing happens.
    await importLink(page).click();
    await expect(status(page)).toHaveText("An import is already running. Wait for it to finish.");
    release();
    await expect(page.locator(".lyrics-line")).toHaveText(["New words"]);
    await expect(alignToggle).toBeEnabled();
    await expect(syncToggle).toBeEnabled();
    // Align was open: it opens again on the new lyrics.
    await expect(page.locator("#lyricsAlign")).toBeVisible();
    // Focus is on the new button, not lost to the page.
    await expect(importLink(page)).toBeFocused();
  });

  test("an import still on its way when another track is opened lands on its own track", async ({ page }) => {
    let release;
    const hold = new Promise((resolve) => { release = resolve; });
    const { posted, kept } = await setUp(page, { hold });
    await pick(page, { name: "song.lrc", buffer: Buffer.from("[00:01.00]For the first track", "utf8") });
    await expect.poll(() => posted.length).toBe(1);
    await page.locator(`.cat-item[data-id="${SIBLING_JOB_ID}"]`).first().click();
    await expect(page.locator(".lyrics-match-title")).toHaveCount(0);
    release();
    await expect.poll(() => kept.lyrics?.synced).toBe("[00:01.00]For the first track");
    // Nothing of it is drawn over the other track, nor said about it.
    await expect(page.locator(".lyrics-line")).toHaveCount(0);
    await expect(status(page)).not.toContainText("Importing");
    // Back on the first track, it is there.
    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    await expect(page.locator(".lyrics-line")).toHaveText(["For the first track"]);
  });

  test("a partly timed file says how many untimed lines it lost", async ({ page }) => {
    await setUp(page, {
      answer: () => ({ status: 200, body: { ...fileEntry({ synced: "[00:01.00]Title" }), dropped_lines: 2 } }),
    });
    await pick(page, { name: "part.lrc", buffer: Buffer.from("[00:01.00]Title\nverse one\nverse two", "utf8") });
    await expect(page.locator(".lyrics-line")).toHaveText(["Title"]);
    await expect(status(page)).toHaveText("2 lines without a timing were left out.");
  });
});
