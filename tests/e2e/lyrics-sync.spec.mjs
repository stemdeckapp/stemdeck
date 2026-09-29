// The Lyrics tab's Sync lines mode: timing lyrics line by line. Tap along
// with playback (T or Enter), undo a tap (Backspace), drag a line's marker in
// the lane over the waveform, nudge the line in hand with the arrows, re-time
// every line from the vocals, reset to the detected or the original timing.
// And the order the timings take effect in: the user's own, then the
// server's fitted to the vocals, then the lyrics' own at the Align offset.
//
// The server's endpoints are stood in for here (the timing engine and its
// checks are the server's own tests). The fixture track is 6 seconds, so
// every time below is inside it.
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
} from "./helpers.mjs";

const LRCLIB = /^https:\/\/lrclib\.net\//;
const WIKIMEDIA = /^https:\/\/((www|query)\.wikidata\.org|[a-z-]+\.wikipedia\.org)\//;
// An empty stamp first, as LRCLIB often has: an instrumental gap, not a line.
const SYNCED = "[00:00.00]\n[00:00.50] Server one\n[00:02.00] Server two\n[00:04.00] Server three";
const ALIGNED = "[00:00.00]\n[00:00.80]<00:00.80>Server <00:01.20>one\n[00:02.40] Server two\n[00:04.60] Server three";
const USER = "[00:00.00]\n[00:01.10] Server one\n[00:03.10] Server two\n[00:05.10] Server three";

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

/** Timed lines from LRC, as [text, seconds] with the empty stamps left out. */
function stamps(lrc) {
  const out = [];
  for (const raw of String(lrc || "").split("\n")) {
    const m = /^\[(\d+):(\d+(?:\.\d+)?)\](.*)$/.exec(raw);
    if (!m) continue;
    const text = m[3].replace(/<[^>]*>/g, "").trim();
    if (text) out.push([text, Math.round((Number(m[1]) * 60 + Number(m[2])) * 100) / 100]);
  }
  return out;
}

/**
 * Open the fixture track's Lyrics tab with `server` as its lyrics.json (null
 * for none) and `saved` as its store entry. `retime` is the list of answers
 * GET .../lyrics/retime gives in turn (the last repeats), and `retimed` what
 * lyrics.json holds once it is done. `envelope` is the vocal envelope, or
 * null for a track without one (so nothing snaps).
 */
async function setUp(page, { server = version(), saved = null, retime = [], retimed = null, envelope = null } = {}) {
  const kept = { lyrics: server };
  const puts = [];
  const deletes = [];
  const retimes = [];
  const envelopes = [];
  let polls = 0;
  await page.route(LRCLIB, (route) => route.abort("internetdisconnected"));
  await page.route(`**/api/jobs/${JOB_ID}/lyrics`, (route) => {
    if (!kept.lyrics) return route.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"no lyrics"}' });
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(kept.lyrics) });
  });
  await page.route(`**/api/jobs/${JOB_ID}/lyrics/user-synced`, (route) => {
    const method = route.request().method();
    if (method === "PUT") {
      const { synced } = route.request().postDataJSON();
      puts.push(synced);
      kept.lyrics = { ...kept.lyrics, user_synced: synced };
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ user_synced: synced }) });
    }
    deletes.push(method);
    const { user_synced: _, ...rest } = kept.lyrics || {};
    kept.lyrics = rest;
    return route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
  });
  await page.route(`**/api/jobs/${JOB_ID}/lyrics/retime`, (route) => {
    if (route.request().method() === "POST") {
      retimes.push(route.request().postDataJSON());
      polls = 0;
      return route.fulfill({ status: 202, contentType: "application/json", body: '{"state":"running"}' });
    }
    const answer = retime[Math.min(polls, retime.length - 1)] || { state: "idle" };
    polls += 1;
    if (answer.state === "done" && retimed && kept.lyrics) kept.lyrics = { ...kept.lyrics, aligned: retimed };
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(answer) });
  });
  await page.route(`**/api/jobs/${JOB_ID}/vocal-envelope`, (route) => (envelopes.push(1), envelope
    ? route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(envelope) })
    : route.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"no vocals"}' })));
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
        if (!sessionStorage.getItem("e2e-seeded-lyrics")) {
          window.localStorage.setItem(key, JSON.stringify(value));
          sessionStorage.setItem("e2e-seeded-lyrics", "1");
        }
      },
      [`stemdeck.lyrics.${JOB_ID}`, saved],
    );
  }
  await page.setViewportSize({ width: 1600, height: 900 });
  await openLyrics(page);
  return { kept, puts, deletes, retimes, envelopes };
}

async function openLyrics(page) {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(".cat-item").first().waitFor({ timeout: 20000 });
  await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
  await expect(page.locator(".app")).not.toHaveClass(/no-track/, { timeout: 15000 });
  await page.locator(".rail-lyrics").click();
  await expect(page.locator(".lyrics-line, .lyrics-text").first()).toBeVisible();
}

const syncLink = (page) => page.locator(".lyrics-tools .lyrics-sync-toggle");
const panel = (page) => page.locator("#lyricsSync");
const lane = (page) => page.locator("#lyricsLane");
const marker = (page, text) => page.locator(".lyr-mark", { hasText: text });
const line = (page, text) => page.locator(".lyrics-line", { hasText: text });
const current = (page) => page.locator(".lyrics-line.current");
const message = (page) => page.locator("#lyricsSync .lyrics-align-message");
const button = (page, name) => page.getByRole("button", { name, exact: true });

async function openSync(page) {
  await syncLink(page).click();
  await expect(panel(page)).toBeVisible();
  await expect(lane(page)).toBeVisible();
  await expect(syncLink(page)).toHaveAttribute("aria-expanded", "true");
}

const seek = (page, seconds) =>
  page.evaluate(async (s) => (await import("/js/transport.js")).setPlayheadTime(s), seconds);
const playhead = (page) =>
  page.evaluate(async () => (await import("/js/transport.js")).transport()?.getCurrentTime?.() ?? 0);
const stored = (page) =>
  page.evaluate((id) => JSON.parse(localStorage.getItem(`stemdeck.lyrics.${id}`) || "null"), JOB_ID);

/** Press `key` as a tap, returning the playhead just before and just after. */
async function tapAt(page, key) {
  const before = await playhead(page);
  await page.keyboard.press(key);
  const after = await playhead(page);
  return [before, after];
}

test.describe("syncing lyrics line by line", () => {
  test("tapping along while the song plays sets each line where it was tapped, and that is saved", async ({ page }) => {
    const { puts } = await setUp(page);
    await openSync(page);
    await expect(page.locator(".lyrics-sync-next-text")).toHaveText("Server one");
    await expect(page.locator(".lyrics-sync-kind").first()).toHaveText("Their own timing");

    await seek(page, 0);
    await page.keyboard.press("Space");
    await expect.poll(() => playhead(page), { timeout: 8000 }).toBeGreaterThan(0.9);
    const one = await tapAt(page, "t");
    await expect(page.locator(".lyrics-sync-next-text")).toHaveText("Server two");
    await expect.poll(() => playhead(page), { timeout: 8000 }).toBeGreaterThan(2.6);
    const two = await tapAt(page, "Enter");
    await expect(page.locator(".lyrics-sync-next-text")).toHaveText("Server three");
    await expect.poll(() => playhead(page), { timeout: 8000 }).toBeGreaterThan(3.8);
    const three = await tapAt(page, "t");
    await page.keyboard.press("Space");
    await expect(page.locator(".lyrics-sync-next-label")).toHaveText("All lines tapped");
    await expect(page.locator(".lyrics-sync-tap")).toBeDisabled();

    // Saved a moment after the last tap: a run of taps is not one request each.
    await expect.poll(() => stamps(puts.at(-1))[2]?.[1]).toBeLessThanOrEqual(three[1] + 0.02);
    const saved = stamps(puts.at(-1));
    expect(saved.map(([text]) => text)).toEqual(["Server one", "Server two", "Server three"]);
    for (const [i, [lo, hi]] of [one, two, three].entries()) {
      expect(saved[i][1]).toBeGreaterThanOrEqual(Math.floor(lo * 100) / 100 - 0.01);
      expect(saved[i][1]).toBeLessThanOrEqual(hi + 0.02);
    }
    // The empty stamp before the first line is kept, so the lines still
    // match the lyrics' own one to one.
    expect(puts.at(-1).split("\n")[0]).toBe("[00:00.00]");
    await expect(page.locator(".lyrics-sync-kind").first()).toHaveText("Timed by you");
  });

  test("Backspace undoes the last tap, not the track: it stays out of the Trash", async ({ page }) => {
    const { puts } = await setUp(page);
    await openSync(page);
    await seek(page, 1);
    await page.keyboard.press("t");
    await seek(page, 2.6);
    await page.keyboard.press("t");
    await expect(page.locator(".lyrics-sync-next-text")).toHaveText("Server three");
    await expect(line(page, "Server two")).toHaveAttribute("data-time", "0:02.60");

    await page.keyboard.press("Backspace");
    await expect(page.locator(".lyrics-sync-next-text")).toHaveText("Server two");
    // Back where the first tap's ripple had carried it.
    await expect(line(page, "Server two")).toHaveAttribute("data-time", "0:02.50");
    await expect(line(page, "Server two")).toHaveClass(/selected/);
    await expect(marker(page, "Server two")).toHaveClass(/selected/);
    await expect.poll(() => stamps(puts.at(-1))).toEqual([["Server one", 1], ["Server two", 2.5], ["Server three", 4.5]]);

    // Ctrl+Z undoes too; with nothing left, the tap button's line is the first.
    await page.keyboard.press("Control+z");
    await expect(page.locator(".lyrics-sync-next-text")).toHaveText("Server one");
    await expect(line(page, "Server one")).toHaveAttribute("data-time", "0:00.50");
    await page.keyboard.press("Backspace");
    const state = await readCatalogState(page);
    expect(state.folders.find((f) => f.id === "trash").items).not.toContain(JOB_ID);
    await expect(page.locator(".app")).not.toHaveClass(/no-track/);
  });

  test("dragging a line's marker in the lane moves the line, and the list and a line click follow", async ({ page }) => {
    const { puts } = await setUp(page);
    await openSync(page);
    await page.getByLabel("Move later lines too").uncheck();
    const track = await page.locator("#daw-lyrics-track").boundingBox();
    const box = await marker(page, "Server two").boundingBox();
    const perSecond = track.width / 6;
    // Grab it near its start and move it a second later.
    const y = box.y + box.height / 2;
    await page.mouse.move(box.x + 4, y);
    await page.mouse.down();
    await page.mouse.move(box.x + 4 + perSecond * 0.5, y, { steps: 4 });
    await page.mouse.move(box.x + 4 + perSecond, y, { steps: 4 });
    await page.mouse.up();

    await expect.poll(() => stamps(puts.at(-1))[1]?.[1] ?? 0).toBeGreaterThan(2.9);
    const moved = stamps(puts.at(-1))[1][1];
    expect(moved).toBeLessThan(3.1);
    // Only that line moved: dragging holds a line between its neighbours.
    expect(stamps(puts.at(-1))[0][1]).toBe(0.5);
    expect(stamps(puts.at(-1))[2][1]).toBe(4);
    await expect(line(page, "Server two")).toHaveAttribute("data-time", /^0:0(2\.9|3\.0)/);

    // Dragged past its neighbour, it stops short of it.
    const again = await marker(page, "Server two").boundingBox();
    await page.mouse.move(again.x + 4, y);
    await page.mouse.down();
    await page.mouse.move(again.x + 4 + perSecond * 2.5, y, { steps: 6 });
    await page.mouse.up();
    await expect.poll(() => stamps(puts.at(-1))[1][1]).toBe(3.9);

    await button(page, "Done").first().click();
    await expect(panel(page)).toHaveCount(0);
    await expect(lane(page)).toBeHidden();
    await line(page, "Server two").click();
    await expect.poll(() => playhead(page)).toBeCloseTo(3.9, 1);
    await expect(current(page)).toHaveText("Server two");
  });

  test("the arrows nudge the line in hand, up and down pick another, and the lead-in plays into it", async ({ page }) => {
    const { puts } = await setUp(page);
    await openSync(page);
    await page.getByLabel("Move later lines too").uncheck();
    await line(page, "Server two").click();
    await expect(line(page, "Server two")).toHaveClass(/selected/);
    await expect(page.locator("#t-time")).toContainText("00:02");
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("ArrowRight");
    await expect(line(page, "Server two")).toHaveAttribute("data-time", "0:02.10");
    await page.keyboard.press("Shift+ArrowLeft");
    await expect(line(page, "Server two")).toHaveAttribute("data-time", "0:01.85");
    await expect.poll(() => stamps(puts.at(-1))[1]?.[1]).toBe(1.85);

    await page.keyboard.press("ArrowUp");
    await expect(marker(page, "Server one")).toHaveClass(/selected/);
    await expect(page.locator(".lyrics-sync-next-text")).toHaveText("Server one");
    // The line's own controls sit under the line in hand.
    await expect(page.locator(".lyrics-line.selected + .lyrics-line-tools")).toBeVisible();
    await page.locator(".lyrics-line-tools").getByRole("button", { name: "Later by 0.25 s" }).click();
    await expect(line(page, "Server one")).toHaveAttribute("data-time", "0:00.75");

    // Set to the playhead, exactly.
    await seek(page, 1.3);
    await button(page, "Start at playhead").click();
    await expect(line(page, "Server one")).toHaveAttribute("data-time", "0:01.30");

    await page.getByLabel("Lead-in").check();
    await line(page, "Server three").click();
    await expect(page.locator("#t-time")).toContainText("00:02");
    await page.keyboard.press("Escape");
    await expect(panel(page)).toHaveCount(0);
    await expect(syncLink(page)).toBeFocused();
  });

  test("a tap snaps to where the singing starts, unless Snap to voice is off", async ({ page }) => {
    // 40ms frames: singing from 2.4 s to 3.0 s.
    const db = new Array(150).fill(-80);
    for (let i = 60; i < 75; i++) db[i] = -12;
    const { puts, envelopes } = await setUp(page, { envelope: { hop: 0.04, db } });
    await expect.poll(() => envelopes.length).toBeGreaterThan(0);
    await openSync(page);
    await line(page, "Server two").click();
    await seek(page, 2.55);
    await page.keyboard.press("t");
    await expect(line(page, "Server two")).toHaveAttribute("data-time", "0:02.40");
    await page.getByLabel("Snap to voice").uncheck();
    await page.keyboard.press("Backspace");
    await expect(line(page, "Server two")).toHaveAttribute("data-time", "0:02.00");
    await page.keyboard.press("t");
    await expect(line(page, "Server two")).toHaveAttribute("data-time", "0:02.55");
    await expect.poll(() => stamps(puts.at(-1))[1]?.[1]).toBe(2.55);
  });

  test("Re-time from vocals shows its progress, then the new timing, and drops the user's", async ({ page }) => {
    const { puts, deletes, retimes } = await setUp(page, {
      server: version({ user_synced: USER }),
      retime: [{ state: "running", progress: 0.4 }, { state: "running", progress: 0.4 }, { state: "done", matched: 0.75 }],
      retimed: { synced: ALIGNED, method: "dtw", matched: 0.75, lines_matched: 3, lines: 4, at: 1 },
    });
    await openSync(page);
    await expect(page.locator(".lyrics-sync-kind").first()).toHaveText("Timed by you");
    await expect(line(page, "Server two")).toHaveAttribute("data-time", "0:03.10");
    await button(page, "Re-time from vocals").click();
    await expect(message(page)).toHaveText("Listening to the vocals… 40%");
    await expect(message(page)).toHaveText("Timed from the vocals: 3 of 4 lines matched.", { timeout: 8000 });
    expect(retimes).toEqual([{}]);
    await expect(line(page, "Server two")).toHaveAttribute("data-time", "0:02.40");
    await expect(page.locator(".lyrics-sync-kind").first()).toHaveText("Timed from the vocals");
    await expect.poll(() => deletes.length).toBe(1);
    expect(puts).toEqual([]);

    // Undo brings the user's own back, and keeps it again.
    await page.keyboard.press("Backspace");
    await expect(line(page, "Server two")).toHaveAttribute("data-time", "0:03.10");
    await expect.poll(() => stamps(puts.at(-1))).toEqual(stamps(USER));
  });

  test("Re-time that is unsure says so and changes nothing", async ({ page }) => {
    const { puts, deletes } = await setUp(page, { retime: [{ state: "running" }, { state: "unsure" }] });
    await openSync(page);
    await button(page, "Re-time from vocals").click();
    await expect(message(page)).toHaveText("The vocals did not match the lines well enough. The timing is as it was.", { timeout: 8000 });
    await expect(line(page, "Server two")).toHaveAttribute("data-time", "0:02.00");
    expect(puts).toEqual([]);
    expect(deletes).toEqual([]);
  });

  test("Re-time that fails says so", async ({ page }) => {
    await setUp(page, { retime: [{ state: "failed" }] });
    await openSync(page);
    await button(page, "Re-time from vocals").click();
    await expect(message(page)).toHaveText("Re-timing from the vocals is not available for this track right now.", { timeout: 8000 });
  });

  test("Reset to detected drops the user's timing, back to the vocals' timing", async ({ page }) => {
    const { deletes, puts } = await setUp(page, { server: version({ user_synced: USER, aligned: { synced: ALIGNED, lines_matched: 3, lines: 4 } }) });
    await openSync(page);
    await expect(line(page, "Server two")).toHaveAttribute("data-time", "0:03.10");
    await button(page, "Reset to detected").click();
    await expect(line(page, "Server two")).toHaveAttribute("data-time", "0:02.40");
    await expect(message(page)).toHaveText("Back to the timing from the vocals.");
    await expect.poll(() => deletes.length).toBe(1);
    expect(puts).toEqual([]);
    await button(page, "Done").first().click();
    await line(page, "Server three").click();
    await expect(page.locator("#t-time")).toContainText("00:04");
  });

  test("Reset to original puts every line back at the lyrics' own timing, over any other", async ({ page }) => {
    const { puts } = await setUp(page, { server: version({ offset_sec: 1, aligned: { synced: ALIGNED } }) });
    await openSync(page);
    await expect(line(page, "Server two")).toHaveAttribute("data-time", "0:02.40");
    await button(page, "Reset to original").click();
    await expect(line(page, "Server two")).toHaveAttribute("data-time", "0:02.00");
    await expect.poll(() => stamps(puts.at(-1))).toEqual(stamps(SYNCED));
    await expect(page.locator(".lyrics-sync-kind").first()).toHaveText("Timed by you");
  });

  test("the user's timing wins over the vocals', which wins over the offset, and Align says so", async ({ page }) => {
    const { kept } = await setUp(page, { server: version({ offset_sec: 1, aligned: { synced: ALIGNED }, user_synced: USER }) });
    await line(page, "Server two").click();
    await expect(page.locator("#t-time")).toContainText("00:03");
    await seek(page, 3.2);
    await expect(current(page)).toHaveText("Server two");
    await page.locator(".lyrics-align-toggle").click();
    await expect(page.locator(".lyrics-align-note")).toHaveText(/timed one by one, by you/);
    await expect(button(page, "Start lyrics here")).toBeDisabled();
    await expect(button(page, "Later by 0.5 s")).toBeDisabled();

    // Without the user's: the vocals'.
    kept.lyrics = version({ offset_sec: 1, aligned: { synced: ALIGNED } });
    await openLyrics(page);
    await page.locator(".lyrics-align-toggle").click();
    await seek(page, 2.5);
    await expect(current(page)).toHaveText("Server two");
    await seek(page, 2.3);
    await expect(current(page)).toHaveText("Server one");
    await expect(page.locator(".lyrics-align-note")).toHaveText(/timed one by one from the vocals/);

    // Without either: the lyrics' own, at the offset, which Align moves.
    kept.lyrics = version({ offset_sec: 1 });
    await openLyrics(page);
    await page.locator(".lyrics-align-toggle").click();
    await seek(page, 2.9);
    await expect(current(page)).toHaveText("Server one");
    await seek(page, 3.1);
    await expect(current(page)).toHaveText("Server two");
    await expect(page.locator(".lyrics-align-note")).toHaveText("");
    await expect(button(page, "Start lyrics here")).toBeEnabled();
  });

  test("lyrics kept in the browser keep their line timing in their store entry", async ({ page }) => {
    const saved = { entry: { ...version(), id: 77, album: "Chosen", savedAt: 1 }, others: [] };
    const { puts } = await setUp(page, { server: null, saved });
    await openSync(page);
    await line(page, "Server three").click();
    await seek(page, 4.7);
    await page.keyboard.press("t");
    await expect.poll(async () => stamps((await stored(page))?.entry?.userSynced)).toEqual([["Server one", 0.5], ["Server two", 2], ["Server three", 4.7]]);
    expect(puts).toEqual([]);
    expect((await stored(page)).entry.album).toBe("Chosen");

    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    await page.locator(".rail-lyrics").click();
    await line(page, "Server three").click();
    await expect(page.locator("#t-time")).toContainText("00:04");
    await seek(page, 4.5);
    await expect(current(page)).toHaveText("Server two");
  });

  test("the lane lines up with the waveform when zoomed and scrolled", async ({ page }) => {
    await setUp(page);
    await openSync(page);
    await page.evaluate(async () => (await import("/js/transport.js")).setWaveZoomLevel(4));
    await page.evaluate(() => {
      const wave = document.getElementById("wave-scroll");
      wave.scrollLeft = wave.scrollWidth / 4;
      wave.dispatchEvent(new Event("scroll"));
    });
    const at = await page.evaluate(() => {
      const wave = document.getElementById("wave-scroll");
      const canvas = document.getElementById("wave-canvas").getBoundingClientRect();
      // Where 2 s is on the waveform, on screen.
      return canvas.left + (2 / 6) * canvas.width;
    });
    await expect.poll(async () => Math.round((await marker(page, "Server two").boundingBox()).x)).toBeGreaterThan(Math.round(at) - 3);
    const box = await marker(page, "Server two").boundingBox();
    expect(Math.abs(box.x - at)).toBeLessThan(3);
  });

  test("text-only lyrics show Sync lines disabled, saying why", async ({ page }) => {
    await setUp(page, { server: version({ synced: "", plain: "Server one\nServer two" }) });
    await expect(page.locator(".lyrics-text").first()).toHaveText("Server one");
    await expect(syncLink(page)).toHaveAttribute("aria-disabled", "true");
    await expect(syncLink(page)).toHaveAttribute("title", /Only lyrics with timing can be synced/);
    await syncLink(page).click({ force: true });
    await expect(panel(page)).toHaveCount(0);
    await expect(lane(page)).toBeHidden();
  });

  test("leaving the Lyrics tab leaves Sync lines, and hides the lane", async ({ page }) => {
    await setUp(page);
    await openSync(page);
    await page.locator(".rail-library").click();
    await expect(lane(page)).toBeHidden();
  });

  test("Escape goes to a menu open over Sync lines, and only then to Sync lines", async ({ page }) => {
    await setUp(page);
    await openSync(page);
    const exportBtn = page.locator("#t-export-btn");
    await exportBtn.click();
    await expect(exportBtn).toHaveAttribute("aria-expanded", "true");

    // The menu's own keys: the arrow moves into it rather than to another line.
    await page.keyboard.press("ArrowDown");
    await expect(page.locator("#t-export-panel :focus")).toHaveCount(1);
    await page.keyboard.press("Escape");
    // The menu closed; Sync lines stayed open.
    await expect(exportBtn).toHaveAttribute("aria-expanded", "false");
    await expect(panel(page)).toBeVisible();

    // With nothing else open, Escape is Sync lines' again.
    await page.keyboard.press("Escape");
    await expect(panel(page)).toBeHidden();
  });
});
