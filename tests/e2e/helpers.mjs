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

const STORAGE_KEY = "stemdeck.folders";
const STORAGE_VERSION = 2;

/** Put the fixture track in the library so the sidebar renders it. */
export async function seedLibrary(page) {
  const state = {
    v: STORAGE_VERSION,
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: [JOB_ID], color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks: {
      [JOB_ID]: {
        id: JOB_ID,
        title: TRACK_TITLE,
        status: "done",
        stems: ["vocals", "drums", "bass", "other"],
        sourceUrl: "local:e2e-fixture.wav",
        createdAt: 1700000000,
        favorite: false,
      },
    },
  };
  await page.addInitScript(
    ([key, value]) => window.localStorage.setItem(key, JSON.stringify(value)),
    [STORAGE_KEY, state],
  );
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

/** Open the fixture track in the studio and wait until the transport is live. */
export async function openStudio(page, { tauri = false } = {}) {
  await seedLibrary(page);
  if (tauri) await stubTauri(page);
  await stubExportEndpoints(page);

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
