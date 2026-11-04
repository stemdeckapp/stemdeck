// The LUFS and Dynamic Range cards say why their numbers moved (#923).
//
// 0.21.0 measures loudness on the whole stereo mix, and an older song is
// measured again in the background the first time it is opened. The next
// open shows different numbers. The page now says so once per song, under the
// cards, and remembers the dismissal on the song's library row.
//
// The re-measure itself is pytest's (tests/test_loudness_upgrade.py): here the
// fixture song's server state is rewritten to one that was measured again, so
// the test does not depend on ffmpeg or on how long the measurement takes.

import { test, expect } from "@playwright/test";
import {
  JOB_ID, SIBLING_JOB_ID, TRACK_TITLE, SIBLING_TITLE,
  fixtureTrack, readCatalogState, seedCatalogState, stubExportEndpoints, stubUpdateCheck,
} from "./helpers.mjs";

const note = (page) => page.locator("#loudness-note");
const dismiss = (page) => page.locator("#loudness-note-dismiss");

/** Serve JOB_ID as a song measured again from ``basis`` with loudness
 *  ``version``; the sibling as is. */
async function remeasuredOnServer(page, basis, version) {
  const path = `/api/jobs/${JOB_ID}`;
  await page.route((url) => url.pathname === path, async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    const response = await route.fetch();
    const state = await response.json();
    Object.assign(state, {
      lufs: -10.5,
      peak_db: -1.0,
      dynamic_range: 9.5,
      loudness_version: version,
      loudness_remeasured: true,
      loudness_basis: basis,
    });
    await route.fulfill({ response, json: state });
  });
}

/** ``seen``: the loudness version the note was already dismissed for. */
async function openLibrary(page, { basis = "stems", version = 2, seen } = {}) {
  const track = fixtureTrack(JOB_ID, TRACK_TITLE);
  if (seen !== undefined) track.loudnessNoteSeen = seen;
  await seedCatalogState(page, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: [JOB_ID, SIBLING_JOB_ID], color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks: {
      [JOB_ID]: track,
      [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, SIBLING_TITLE),
    },
  }, { once: true });
  await remeasuredOnServer(page, basis, version);
  await stubExportEndpoints(page);
  await stubUpdateCheck(page, { available: false });
  await page.goto("/", { waitUntil: "domcontentloaded" });
}

async function openSong(page, id) {
  await page.locator(`.cat-item[data-id="${id}"]`).first().click();
  // The cards are painted from the server's state once it has answered.
  await expect(page.locator("#summary-bpm")).toHaveText("120");
}

// Background syncs are still in flight through the rewriter when a test ends.
test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

test("a song measured again says so once, and the dismissal survives a reload", async ({ page }) => {
  await openLibrary(page);
  await openSong(page, JOB_ID);
  await expect(page.locator("#summary-lufs")).toHaveText("-10.5");
  await expect(note(page)).toBeVisible();
  await expect(note(page)).toContainText("measured again with the 0.21.0 method");
  await expect(note(page)).toContainText("from its stems added together");

  // The Guide explains the dismiss button.
  await page.locator("#helpModeBtn").click();
  await dismiss(page).hover();
  await expect(page.locator("#helpTip")).toContainText("It stays hidden for this song.");
  await expect(page.locator("#helpTip b")).toHaveText("Hide this note");
  await page.keyboard.press("Escape");

  await dismiss(page).click();
  await expect(note(page)).toBeHidden();
  const saved = await readCatalogState(page);
  expect(saved.tracks[JOB_ID].loudnessNoteSeen).toBe(2);

  await page.reload({ waitUntil: "domcontentloaded" });
  await openSong(page, JOB_ID);
  await expect(page.locator("#summary-lufs")).toHaveText("-10.5");
  await expect(note(page)).toBeHidden();
});

test("a song that was not measured again shows no note, and opening it hides another's", async ({ page }) => {
  await openLibrary(page);
  await openSong(page, SIBLING_JOB_ID);
  await expect(note(page)).toBeHidden();

  await openSong(page, JOB_ID);
  await expect(note(page)).toBeVisible();
  await openSong(page, SIBLING_JOB_ID);
  await expect(note(page)).toBeHidden();
});

test("a song measured from its kept upload names no stems", async ({ page }) => {
  await openLibrary(page, { basis: "source" });
  await openSong(page, JOB_ID);
  await expect(note(page)).toContainText("the whole song, in stereo.");
  await expect(note(page)).not.toContainText("stems");
});

test("the note follows the language", async ({ page }) => {
  await page.addInitScript(() => window.localStorage.setItem("stemdeck.language", JSON.stringify("de")));
  await openLibrary(page);
  await openSong(page, JOB_ID);
  await expect(note(page)).toContainText("neu gemessen");
  await expect(dismiss(page)).toHaveAttribute("aria-label", "Hinweis ausblenden");
});

test("a later change of method shows the note again", async ({ page }) => {
  // Hidden for version 2; the server now says the song was measured with 3.
  await openLibrary(page, { version: 3, seen: 2 });
  await openSong(page, JOB_ID);
  await expect(note(page)).toBeVisible();
});

test("the note goes with the cards when the studio is cleared", async ({ page }) => {
  await openLibrary(page);
  await openSong(page, JOB_ID);
  await expect(note(page)).toBeVisible();
  // What "Sync again" does before it imports (job.js importFromUrl): the same
  // module instance the page uses. Deleting the open song does not clear the
  // studio, so the note stays with the cards it explains there.
  await page.evaluate(async () => (await import("/js/job.js")).reset());
  // The empty card's placeholder dash (player.js resetAnalysisCards).
  await expect(page.locator("#summary-key")).toHaveText(String.fromCodePoint(0x2014));
  await expect(note(page)).toBeHidden();
});

test("a language switch while the note shows rewrites it", async ({ page }) => {
  await openLibrary(page);
  await openSong(page, JOB_ID);
  await expect(note(page)).toContainText("measured again");
  await page.evaluate(async () => (await import("/js/i18n.js")).setLanguage("fr"));
  await expect(note(page)).toContainText("mesurés à nouveau");
  await expect(note(page)).toContainText("somme de ses stems");
  await expect(dismiss(page)).toHaveAttribute("aria-label", "Masquer cette note");
});

// #480: the lanes are what runs short of height on a 1366x768 laptop. The
// note is a row in the track header, above the lanes, so it takes height from
// the wave panel while it shows. What this holds it to, with the longest note
// strings (es, fr) and German:
// - at most two lines, which is 37 px there;
// - it costs the wave panel its own height and nothing more;
// - collapsing Analysis, the #480 way to give the lanes room, still hides
//   it with the cards;
// - the mixer and waveform columns agree with it shown, and again after it is
//   dismissed, which changes the panel after the lanes were built.
// Measured on 2026-11-04 at 1366x768: the panel is 347 px with no note and
// 310 px with one, against 328 px for the fixture's four stems and the
// add-track row. Six stems (472 px) did not fit there before the note either.
const NOTE_MAX_LINES = 2;

async function waitForLanes(page) {
  await page.waitForFunction(
    () => !/\/\s*00:00\s*$/.test(document.querySelector("#t-time")?.textContent || "00:00 / 00:00"),
    null,
    { timeout: 20000 },
  );
  // The lane refit runs from a ResizeObserver; let it settle.
  await page.waitForTimeout(450);
}

function layout(page) {
  return page.evaluate(() => {
    const n = document.getElementById("loudness-note");
    const cs = getComputedStyle(n);
    const box = n.getBoundingClientRect();
    const text = document.getElementById("loudness-note-text").getBoundingClientRect();
    const top = (el) => Math.round(el.getBoundingClientRect().top);
    const mix = [...document.querySelectorAll(".mixer-column .lane-header:not(.hidden)")].map(top);
    const wave = [...document.querySelectorAll(".stem-waveform-row:not(.hidden)")].map(top);
    return {
      noteShown: !n.hidden,
      noteLines: Math.round(text.height / parseFloat(cs.lineHeight)),
      noteH: Math.round(box.height),
      panelH: document.querySelector(".daw-wave-panel").clientHeight,
      rows: [mix.length, wave.length],
      worstRow: Math.max(0, ...mix.map((t, i) => Math.abs((wave[i] ?? t) - t))),
      column: `${Math.round(document.querySelector(".waves-column").getBoundingClientRect().height)}px`,
      stackVar: getComputedStyle(document.querySelector(".app")).getPropertyValue("--wave-widget-track-stack-h").trim(),
    };
  });
}

async function toggleAnalysis(page) {
  await page.locator('.daw-panel-toggle[data-panel="analysis"]').click();
  await page.waitForTimeout(450);
}

for (const language of ["es", "fr", "de"]) {
  test(`${language} at 1366x768: the note takes two lines at most and the lanes stay lined up`, async ({ page }) => {
    await page.setViewportSize({ width: 1366, height: 768 });
    await page.addInitScript((code) => window.localStorage.setItem("stemdeck.language", JSON.stringify(code)), language);
    await openLibrary(page);
    await openSong(page, JOB_ID);
    await waitForLanes(page);

    const shown = await layout(page);
    expect(shown.noteShown).toBe(true);
    expect(shown.noteLines, JSON.stringify(shown)).toBeLessThanOrEqual(NOTE_MAX_LINES);
    expect(shown.rows[0]).toBe(shown.rows[1]);
    expect(shown.worstRow, JSON.stringify(shown)).toBeLessThanOrEqual(1);
    expect(shown.column).toBe(shown.stackVar);

    // Analysis collapsed with the note pending: the note goes with the cards.
    await toggleAnalysis(page);
    const collapsedWithNote = (await layout(page)).panelH;
    await toggleAnalysis(page);

    // Dismissed after the lanes were built: the panel gets back exactly the
    // note's height, and the lanes follow in both columns.
    await dismiss(page).click();
    await page.waitForTimeout(450);
    const gone = await layout(page);
    expect(gone.noteShown).toBe(false);
    expect(Math.abs(gone.panelH - shown.panelH - shown.noteH), JSON.stringify({ shown, gone })).toBeLessThanOrEqual(1);
    expect(gone.worstRow, JSON.stringify(gone)).toBeLessThanOrEqual(1);
    expect(gone.column).toBe(gone.stackVar);

    await toggleAnalysis(page);
    expect((await layout(page)).panelH).toBe(collapsedWithNote);
  });
}

// The narrowest desktop window the cards are laid out for (daw.css). The
// phone UI (static/mobile/) has cards of its own and no note.
test("at a 950 px window the whole note and its button fit", async ({ page }) => {
  await page.setViewportSize({ width: 950, height: 700 });
  await openLibrary(page);
  await openSong(page, JOB_ID);
  await expect(note(page)).toBeVisible();
  const fits = await page.evaluate(() => {
    const n = document.getElementById("loudness-note");
    const b = document.getElementById("loudness-note-dismiss").getBoundingClientRect();
    const r = n.getBoundingClientRect();
    return n.scrollWidth <= n.clientWidth && b.right <= r.right && b.width > 0;
  });
  expect(fits).toBe(true);
});
