// Shared setup for the browser tests.
//
// Two things here are load-bearing and were both learned the hard way:
//
// 1. The sidebar renders from the library store, not from /api/jobs. A job that
//    exists on disk but is absent from the store is invisible in the UI, and a
//    test that clicks nothing passes for the wrong reason. seedLibrary writes
//    that store before any script runs.
//
// 2. The desktop and browser download paths genuinely diverge, which is why
//    #335 was invisible in a browser. stubTauri installs a controllable
//    window.__TAURI__ so the desktop branch runs, and so the test decides when
//    an export finishes rather than racing a real one.

export const JOB_ID = "e2e0deadbeef";
export const TRACK_TITLE = "E2E Fixture Track";

// The second job seed.py writes, a re-extraction of the same source. Its
// source_url is identical to the fixture track's, which is what makes the
// catalog's dedup-by-source branch reachable (#542).
export const SIBLING_JOB_ID = "e2e0cafebabe";
export const SIBLING_TITLE = "E2E Fixture Track (again)";
export const SOURCE_URL = "local:e2e-fixture";

const STORAGE_KEY = "stemdeck.folders";
const STORAGE_VERSION = 2;

/** The library store's shape for one finished track. */
export function fixtureTrack(id, title) {
  return {
    id,
    title,
    status: "done",
    stems: ["vocals", "drums", "bass", "other"],
    sourceUrl: SOURCE_URL,
    createdAt: 1700000000,
    favorite: false,
  };
}

/**
 * Write the library store before any of the app's scripts run.
 *
 * Tests that care about which folder a track is in build the state themselves
 * and call this. seedLibrary is the default arrangement on top of it.
 */
export async function seedCatalogState(page, { folders, tracks }) {
  await page.addInitScript(
    ([key, value]) => window.localStorage.setItem(key, JSON.stringify(value)),
    [STORAGE_KEY, { v: STORAGE_VERSION, folders, tracks }],
  );
}

/** Read the library store back, to assert on what was persisted. */
export async function readCatalogState(page) {
  return page.evaluate((key) => JSON.parse(window.localStorage.getItem(key) || "null"), STORAGE_KEY);
}

/**
 * Put both fixture tracks in the library so the sidebar renders them.
 *
 * Both, not just one. syncWithServer imports any server job the store does not
 * already know, and the sibling shares the fixture track's source_url, so
 * leaving it out would send every page load in the suite through
 * addTrackToLibrary's dedup branch. That branch renames the existing entry to
 * the incoming job's id, and `.cat-item[data-id="e2e0deadbeef"]` -- which most
 * specs here click -- would stop existing.
 */
export async function seedLibrary(page) {
  await seedCatalogState(page, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: [JOB_ID, SIBLING_JOB_ID], color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks: {
      [JOB_ID]: fixtureTrack(JOB_ID, TRACK_TITLE),
      [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, SIBLING_TITLE),
    },
  });
}

/**
 * Install a fake Tauri bridge so the app takes its desktop code path.
 *
 * The export is two commands, and the test controls each independently:
 *
 *   pick_export_destination  the native save dialog
 *   download_to_path         the transfer
 *
 * Holding the dialog open is what makes #338 testable -- the label must still
 * read "Export Mix" while the user is choosing a folder, because nothing is
 * being exported yet. Holding the transfer open is what makes #335 testable.
 * Tests drive both through window.__e2e.
 */
export async function stubTauri(page) {
  await page.addInitScript(() => {
    const calls = [];
    let pendingResolve = null;
    let pendingReject = null;
    let pickResolve = null;

    const settle = (fn) => (v) => {
      pendingResolve = null;
      pendingReject = null;
      fn(v);
    };

    window.__e2e = {
      calls,
      // Choose a destination, as if the user hit Save in the dialog.
      choosePath: () => pickResolve && (pickResolve("token-1"), (pickResolve = null)),
      // Dismiss the dialog. No transfer follows.
      cancelPick: () => pickResolve && (pickResolve(null), (pickResolve = null)),
      pickPending: () => Boolean(pickResolve),
      // Settle the transfer that is currently in flight.
      finishSave: (value) => pendingResolve && pendingResolve(value ?? null),
      failSave: (message) => pendingReject && pendingReject(message ?? "save failed"),
      savePending: () => Boolean(pendingResolve),
      callsFor: (cmd) => calls.filter((c) => c.cmd === cmd),
    };

    window.__TAURI__ = {
      core: {
        invoke: (cmd, args) => {
          calls.push({ cmd, args });
          switch (cmd) {
            case "pick_export_destination":
              return new Promise((resolve) => { pickResolve = resolve; });
            case "download_to_path":
            case "save_audio_file":
              return new Promise((resolve, reject) => {
                pendingResolve = settle(resolve);
                pendingReject = settle(reject);
              });
            // The library store lives in the Tauri store on desktop. Back it
            // with localStorage so seedLibrary works in this mode too.
            case "store_get": {
              const raw = window.localStorage.getItem(args?.key);
              return Promise.resolve(raw === null ? null : JSON.parse(raw));
            }
            case "store_set":
              window.localStorage.setItem(args?.key, JSON.stringify(args?.value));
              return Promise.resolve(null);
            case "get_setup_status":
              return Promise.resolve({ ready: true, data_dir: "/tmp/e2e", ffmpeg: "/usr/bin/ffmpeg" });
            default:
              return Promise.resolve(null);
          }
        },
      },
      event: { listen: () => Promise.resolve(() => {}) },
    };
  });
}

/**
 * Keep export requests off the real backend.
 *
 * A browser-mode export is an <a download> pointed at a mixdown endpoint that
 * shells out to ffmpeg. These tests are about the menu's state machine, so the
 * bytes are irrelevant and the render time is not worth paying.
 */
export async function stubExportEndpoints(page) {
  await page.route("**/api/jobs/*/mix**", (route) =>
    route.fulfill({ status: 200, contentType: "audio/wav", body: Buffer.from("RIFF") }));
  await page.route("**/api/jobs/*/stems.zip**", (route) =>
    route.fulfill({ status: 200, contentType: "application/zip", body: Buffer.from("PK") }));
  await page.route("**/api/jobs/*/render**", (route) =>
    route.fulfill({ status: 200, contentType: "audio/wav", body: Buffer.from("RIFF") }));
}

/**
 * Answer the tag read an older track asks for when it is opened (#699), with
 * `answer` as its audio_tags (null: the file had none; a function: called with
 * the job id), and record the job id of each request. `hold`, when given, is
 * awaited before answering, so a test can act while the read is still out.
 * `status` other than 200 answers with that error instead.
 */
export async function stubAudioTags(page, answer = null, { hold = null, status = 200 } = {}) {
  const asked = [];
  await page.route("**/api/jobs/*/audio-tags", async (route) => {
    const id = new URL(route.request().url()).pathname.split("/")[3];
    asked.push(id);
    if (hold) await hold;
    const tags = typeof answer === "function" ? answer(id) : answer;
    try {
      await route.fulfill(status === 200
        ? { status, contentType: "application/json", body: JSON.stringify({ audio_tags: tags }) }
        : { status, contentType: "application/json", body: JSON.stringify({ detail: "stubbed" }) });
    } catch {
      // The page went away while the answer was held.
    }
  });
  return asked;
}

/**
 * Answer the update check locally instead of letting it reach GitHub.
 *
 * Two reasons. It puts an external service in the path of every run, and more
 * subtly it makes the notification centre non-deterministic: when the release
 * on GitHub is newer than the version under test, an update card appears and
 * lights the same badge failure notifications use. That is real behaviour --
 * one badge for the centre -- but a test asserting on the badge has to control
 * it. A non-ok response is the check's own "nothing to see" path.
 *
 * This is why the notification tests passed locally and failed in CI: a dev
 * build reports a version containing "dev", which checkForUpdate skips, so the
 * card never appeared on a developer machine.
 */
export async function stubUpdateCheck(page, { available = false } = {}) {
  if (available) {
    // Force the "an update exists" state: the check skips dev builds, so the
    // version has to look like a release for the card to appear at all.
    await page.route("**/api/health**", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          name: "StemDeck",
          status: "ok",
          version: "0.5.0",
          ffmpeg_configured: true,
          demucs_model: "htdemucs_6s",
          demucs_device: "cpu",
        }),
      }));
    // A single object, not an array: the app asks for /releases/latest, which
    // is GitHub's own answer to "which release is the latest one". A release
    // published as a pre-release, or as neither latest nor pre-release, is not
    // returned by that endpoint at all, so it cannot be offered (#666).
    await page.route("https://api.github.com/**", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          tag_name: "v9.9.9",
          draft: false,
          prerelease: false,
          body: "notes",
          html_url: "https://example.invalid",
          assets: [],
        }),
      }));
    return;
  }
  await page.route("https://api.github.com/**", (route) =>
    route.fulfill({
      status: 403,
      contentType: "application/json",
      body: JSON.stringify({ message: "stubbed by tests" }),
    }));
}

/** Open the fixture track in the studio and wait until the transport is live. */
/**
 * Keep favourites off the shared e2e backend (#734).
 *
 * A heart now writes to the server, and every spec shares one server, so a
 * favourite set by one test would come back through GET /api/jobs into the
 * next one's library. Writes are answered here instead, and the server's
 * value stays null, which the desktop reads as "never said". A test about the
 * sync itself passes `serverFavorites` to stand in for what the server knows.
 */
export async function stubFavorites(page, serverFavorites = {}) {
  const writes = [];
  await page.route("**/api/jobs/*/favorite", async (route) => {
    const id = new URL(route.request().url()).pathname.split("/")[3];
    const { favorite } = JSON.parse(route.request().postData() || "{}");
    writes.push({ id, favorite });
    serverFavorites[id] = favorite;
    await route.fulfill({ json: { job_id: id, favorite } });
  });
  await page.route(/\/api\/jobs(\?.*)?$/, async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const res = await route.fetch();
    const jobs = await res.json();
    for (const j of jobs) j.favorite = serverFavorites[j.job_id] ?? null;
    await route.fulfill({ response: res, json: jobs });
  });
  return writes;
}

export async function openStudio(page, { tauri = false, updateAvailable = false, serverFavorites = {} } = {}) {
  await seedLibrary(page);
  const favoriteWrites = await stubFavorites(page, serverFavorites);
  if (tauri) await stubTauri(page);
  await stubExportEndpoints(page);
  await stubUpdateCheck(page, { available: updateAvailable });

  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
  // The transport total only leaves 00:00 once an engine has reported a
  // duration, so it doubles as "the studio is actually ready".
  await page.waitForFunction(
    () => !/\/\s*00:00\s*$/.test(document.querySelector("#t-time")?.textContent || "00:00 / 00:00"),
    null,
    { timeout: 20000 },
  );
  return { favoriteWrites };
}

/**
 * Wait until the click track is live.
 *
 * openStudio returns once the transport reports a duration, but the metronome
 * is built later still -- after the audio engine is up and the beat grid has
 * been fetched. Acting before then hits a null metronome, where the rate and
 * accent controls silently no-op: the click looks present and does nothing.
 */
export async function waitForClickTrack(page, { revealOptions = true } = {}) {
  await page.waitForFunction(
    () => document.querySelector("#t-metro") && !document.querySelector("#t-metro").disabled,
    null,
    { timeout: 20000 },
  );
  if (revealOptions) await openClickOptions(page);
}

/**
 * Put the click-track options where a test can click them.
 *
 * Below a width footerFit measures for itself, the options move into a popover
 * behind a disclosure beside the on/off pill, instead of sitting inline. A spec that goes straight for
 * #t-metro-bar then finds a control that exists, is enabled, and cannot be
 * clicked -- and times out saying nothing about why. Opening it here keeps
 * those specs about the click track rather than about the footer's width, and
 * it is a no-op at any width that still has room for the options.
 *
 * Pass revealOptions: false to waitForClickTrack when the popover itself is
 * what is under test.
 */
export async function openClickOptions(page) {
  const more = page.locator("#t-metro-more");
  if (!(await more.isVisible())) return;
  if ((await more.getAttribute("aria-expanded")) === "true") return;
  await more.click();
  await page.locator("#t-metro-panel.open").waitFor({ timeout: 5000 });
}

export const exportUi = (page) => ({
  button: page.locator("#t-export-btn"),
  panel: page.locator("#t-export-panel"),
  label: page.locator("#t-export-label"),
  mix: page.locator("#t-export-mix"),
  stems: page.locator("#t-export-stems"),
  region: page.locator("#t-export-region"),
  fmt: (name) => page.locator(`#t-fmt-${name}`),
  error: page.locator("#error:not(.hidden)"),
  open: async () => {
    await page.locator("#t-export-btn").click();
    await page.locator("#t-export-panel:not(.hidden)").waitFor({ timeout: 5000 });
  },
});

/** The server's lyrics lookup, which the Lyrics tab asks rather than LRCLIB (#719). */
export const LYRICS_LOOKUP = /\/api\/jobs\/[a-f0-9]{12}\/lyrics\/lookup$/;

/**
 * Stand in for the server's lyrics lookup, so the lyrics specs run offline and
 * never reach LRCLIB through the shared backend.
 *
 * `rows` are LRCLIB rows. Like the server, it keeps only versions of `song` by
 * `artist` (brackets and a " - ..." tail aside), keeps the first within 3 s of
 * the track's `duration` and offers the others with it, or offers them all
 * when none is that length. What it kept or offered is then what GET
 * .../lyrics answers, as the server's lyrics.json would. The rule itself is
 * tested against the real server in tests/test_lyrics_lookup.py; this only
 * has to answer the way it does. Resolves to the requests the page made:
 * [{ id, body }].
 */
export async function stubLyricsLookup(page, { rows = [], artist = "", song = "", duration = 6, offline = false, nothingKnown = false } = {}) {
  const asked = [];
  const kept = {};
  const offered = {};
  const jobOf = (route) => new URL(route.request().url()).pathname.split("/")[3];
  const version = (r) => ({
    source: "lrclib",
    lrclib_id: r.id,
    track: r.trackName,
    artist: r.artistName,
    album: r.albumName || "",
    duration: r.duration,
    instrumental: Boolean(r.instrumental),
    synced: r.syncedLyrics || "",
    plain: r.plainLyrics || "",
  });
  const songKey = (s) => String(s || "").replace(/[([{][^)\]}]*[)\]}]/g, "").replace(/\s+-\s+.*$/, "").trim().toLowerCase();
  await page.route(/\/api\/jobs\/[a-f0-9]{12}\/lyrics$/, (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    const id = jobOf(route);
    if (kept[id]) return route.fulfill({ json: kept[id], headers: { "cache-control": "no-cache" } });
    if (offered[id]?.length) return route.fulfill({ status: 404, json: { detail: "no lyrics", others: offered[id] } });
    return route.fallback();
  });
  await page.route(LYRICS_LOOKUP, (route) => {
    const id = jobOf(route);
    asked.push({ id, body: JSON.parse(route.request().postData() || "{}") });
    if (offline) return route.fulfill({ status: 502, json: { detail: "lyrics service unreachable" } });
    if (nothingKnown) return route.fulfill({ status: 404, json: { detail: "nothing to look up", others: [], nothing_known: true } });
    const mine = rows.filter((r) => r.artistName === artist && songKey(r.trackName) === songKey(song)).map(version);
    const best = mine.find((v) => Math.abs(v.duration - duration) <= 3);
    if (best) {
      kept[id] = { v: 1, ...best, timing: "exact", others: mine.filter((v) => v !== best) };
      return route.fulfill({ json: kept[id] });
    }
    offered[id] = mine;
    return route.fulfill({ status: 404, json: { detail: "no lyrics", others: mine } });
  });
  return asked;
}

/**
 * Stand in for the import queue, for a test that has to watch an import while
 * it runs. The e2e backend never separates anything, so a job that stays
 * "running" long enough to look at cannot be produced for real.
 *
 * `run()` reports the job as running and `settle()` as gone, each followed by a
 * queue read so the page sees it at once. The queue stream is held off, or its
 * own empty frames would override the stub.
 */
export async function stubImportQueue(page, jobId, title = "Importing") {
  let running = false;
  await page.route("**/api/queue", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        running: running
          ? { job_id: jobId, status: "separating", stage: "Separating 40%", progress: 0.4, title }
          : null,
        queued: [],
        paused: false,
        max_pending_uploads: 5,
        max_pending_urls: 50,
        capacity_left_uploads: 5,
        capacity_left_urls: 50,
      }),
    }));
  await page.route("**/api/queue/events", (route) => route.abort());
  const read = () => page.evaluate(async () => (await import("/js/queue.js")).refreshQueue());
  return {
    run: async () => { running = true; await read(); },
    settle: async () => { running = false; await read(); },
  };
}
