// A control pressed in the first moments after start still responds (#1010).
//
// On the desktop app every store read is a round trip to the shell, and at
// first start those round trips can take long enough to click through. The
// library's controls (Settings, About, We Recommend, the rail views, library
// search) used to be wired only after the last of those reads, so a press in
// that window did nothing at all.
//
// In a browser the store is localStorage and the whole start runs before the
// first frame, so no press can land early there. These tests take the desktop
// path (stubTauri) and hold one store read open until the test lets it go:
// the language, which is the first read of the start, and the library, which
// is the last. The last two check that a read that fails stops nothing.

import { test, expect } from "@playwright/test";
import {
  JOB_ID, SIBLING_JOB_ID, SIBLING_TITLE, TRACK_TITLE, fixtureTrack, seedCatalogState, seedLibrary, stubTauri, stubUpdateCheck,
} from "./helpers.mjs";

const LANGUAGE_KEY = "stemdeck.language";
const LIBRARY_KEY = "stemdeck.folders";

/** Start the desktop app with every read of `key` held until release(page). */
async function startWithHeldRead(page, key, { seed = seedLibrary } = {}) {
  const errors = [];
  page.on("pageerror", (err) => errors.push(String(err)));
  await seed(page);
  await stubTauri(page);
  await page.addInitScript((heldKey) => {
    const invoke = window.__TAURI__.core.invoke;
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    window.__e2eReleaseStore = () => release();
    window.__TAURI__.core.invoke = (cmd, args) =>
      cmd === "store_get" && args?.key === heldKey ? gate.then(() => invoke(cmd, args)) : invoke(cmd, args);
  }, key);
  await stubUpdateCheck(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.locator("#settingsBtn")).toBeVisible();
  return errors;
}

const release = (page) => page.evaluate(() => window.__e2eReleaseStore());

test("Settings pressed while the language is still being read opens", async ({ page }) => {
  const errors = await startWithHeldRead(page, LANGUAGE_KEY);
  await page.locator("#settingsBtn").click();
  await release(page);
  await expect(page.locator(".library-editor")).toBeVisible();
  expect(errors).toEqual([]);
});

test("Settings pressed while the library is still loading opens on the loaded library", async ({ page }) => {
  const errors = await startWithHeldRead(page, LIBRARY_KEY);
  await page.locator("#settingsBtn").click();
  await release(page);
  await expect(page.locator(".library-editor")).toBeVisible();
  await expect(page.locator(".library-editor-status")).toHaveText("All tracks in sync");
  await page.locator(".library-editor-close").click();
  await expect(page.locator(`.cat-item[data-id="${JOB_ID}"]`).first()).toBeVisible();
  expect(errors).toEqual([]);
});

test("About, a rail view and search pressed before the library loads all take effect", async ({ page }) => {
  const errors = await startWithHeldRead(page, LIBRARY_KEY);

  // About needs nothing loaded, so it opens at once.
  await page.locator("#aboutBtn").click();
  await expect(page.locator("#aboutDialog")).toBeVisible();
  await page.locator("#aboutClose").click();
  await expect(page.locator("#aboutDialog")).toBeHidden();

  // The rest is applied once the library has loaded. Nothing draws the
  // library before then: an empty Trash drawn now would offer Empty Trash
  // over the real one.
  await page.locator(".rail-trash").click();
  await expect(page.locator("#catalogList")).toBeEmpty();
  await expect(page.locator("#clearBinBtn")).toBeHidden();
  await page.locator(".rail-favorites").click();
  await page.locator("#catalogSearch").fill("again");
  await expect(page.locator("#catalogList")).toBeEmpty();
  await release(page);

  await expect(page.locator(".rail-favorites")).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator("#catalogPanel")).toHaveClass(/favorites-view/);
  // The search typed early filters the library: only the sibling's title
  // contains "again".
  await page.locator(".rail-library").click();
  await expect(page.locator(`.cat-item[data-id="${SIBLING_JOB_ID}"]`).first()).toBeVisible();
  await expect(page.locator(`.cat-item[data-id="${JOB_ID}"]`)).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("We Recommend pressed while the language is still being read opens with its cards", async ({ page }) => {
  const errors = await startWithHeldRead(page, LANGUAGE_KEY);
  await page.locator("#friendsBtn").click();
  await release(page);
  await expect(page.locator("#friendsDialog")).toBeVisible();
  await expect(page.locator("#friendsDialogGrid .lib-friend").first()).toBeVisible();
  expect(errors).toEqual([]);
});

test("We Recommend needs only the language, so it opens while the library still loads", async ({ page }) => {
  const errors = await startWithHeldRead(page, LIBRARY_KEY);
  await page.locator("#friendsBtn").click();
  await expect(page.locator("#friendsDialog")).toBeVisible();
  await expect(page.locator("#friendsDialogGrid .lib-friend").first()).toBeVisible();
  await release(page);
  expect(errors).toEqual([]);
});

test("a stored library that cannot be read is reported, and Settings still opens", async ({ page }) => {
  // Trash items stored as text rather than a list: loadState throws on it.
  const corrupt = (p) => seedCatalogState(p, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: [JOB_ID, SIBLING_JOB_ID], color: null },
      { id: "trash", name: "Trash", items: "not a list", color: null },
    ],
    tracks: {
      [JOB_ID]: fixtureTrack(JOB_ID, TRACK_TITLE),
      [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, SIBLING_TITLE),
    },
  });
  const errors = await startWithHeldRead(page, LIBRARY_KEY, { seed: corrupt });
  await page.locator("#settingsBtn").click();
  await release(page);
  await expect(page.locator(".library-editor")).toBeVisible();
  await expect(page.locator("#error")).toContainText("Your library could not be read in full");
  expect(errors).toEqual([]);
});

test("a sidebar state that cannot be read does not stop the start", async ({ page }) => {
  // Some browsers refuse storage outright (a SecurityError) for a page they
  // treat as third-party or in a locked-down profile.
  const errors = [];
  page.on("pageerror", (err) => errors.push(String(err)));
  await seedLibrary(page);
  await page.addInitScript(() => {
    const getItem = Storage.prototype.getItem;
    Storage.prototype.getItem = function (key) {
      if (key === "stemdeck.catalog.collapsed") throw new DOMException("denied", "SecurityError");
      return getItem.call(this, key);
    };
  });
  await stubUpdateCheck(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  await expect(page.locator(".library-editor")).toBeVisible();
  expect(errors).toEqual([]);
});
