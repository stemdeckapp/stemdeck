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
export const SOURCE_URL = "local:e2e-fixture.wav";

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
    await page.route("https://api.github.com/**", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        // An ARRAY: the app polls /releases (the list), not /releases/latest.
        // The unpromoted pre-release in front of the stable one is deliberate:
        // it must be skipped, because a release is only offered once it has
        // been promoted to the latest release.
        body: JSON.stringify([
          { tag_name: "v9.9.10", draft: false, prerelease: true, body: "unpromoted", html_url: "https://example.invalid", assets: [] },
          { tag_name: "v9.9.9", draft: false, prerelease: false, body: "notes", html_url: "https://example.invalid", assets: [] },
        ]),
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
export async function openStudio(page, { tauri = false, updateAvailable = false } = {}) {
  await seedLibrary(page);
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
}

/**
 * Wait until the click track is live.
 *
 * openStudio returns once the transport reports a duration, but the metronome
 * is built later still -- after the audio engine is up and the beat grid has
 * been fetched. Acting before then hits a null metronome, where the rate and
 * accent controls silently no-op: the click looks present and does nothing.
 */
export async function waitForClickTrack(page) {
  await page.waitForFunction(
    () => document.querySelector("#t-metro") && !document.querySelector("#t-metro").disabled,
    null,
    { timeout: 20000 },
  );
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
