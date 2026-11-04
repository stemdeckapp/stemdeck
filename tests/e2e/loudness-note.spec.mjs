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

/** Serve JOB_ID as a song measured again from ``basis``; the sibling as is. */
async function remeasuredOnServer(page, basis) {
  const path = `/api/jobs/${JOB_ID}`;
  await page.route((url) => url.pathname === path, async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    const response = await route.fetch();
    const state = await response.json();
    Object.assign(state, {
      lufs: -10.5,
      peak_db: -1.0,
      dynamic_range: 9.5,
      loudness_version: 2,
      loudness_remeasured: true,
      loudness_basis: basis,
    });
    await route.fulfill({ response, json: state });
  });
}

async function openLibrary(page, { basis = "stems" } = {}) {
  await seedCatalogState(page, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: [JOB_ID, SIBLING_JOB_ID], color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks: {
      [JOB_ID]: fixtureTrack(JOB_ID, TRACK_TITLE),
      [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, SIBLING_TITLE),
    },
  }, { once: true });
  await remeasuredOnServer(page, basis);
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
