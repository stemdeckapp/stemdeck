// A delete, a library move or a reset the server refuses while a song is still
// being separated (Extract missing stems, Lead + Backing): 409 with the code
// "job_busy". The page says why, in the interface language, and a song the
// Trash could not empty stays in the Trash.
//
// The refusals are answered here: this suite's backend runs no separation, and
// the real delete, reset and move would change the fixture the whole suite
// shares. The server half is tests/test_job_busy.py.

import { test, expect } from "@playwright/test";

import {
  JOB_ID,
  SIBLING_JOB_ID,
  TRACK_TITLE,
  fixtureTrack,
  readCatalogState,
  seedCatalogState,
  seedLibrary,
  stubUpdateCheck,
} from "./helpers.mjs";

const BUSY = { status: 409, json: { detail: "server words", code: "job_busy" } };
const EN = "A song is still being separated (missing stems or Lead + Backing). Try again when it has finished.";
const DE = "Ein Song wird noch getrennt (fehlende Stems oder Lead + Backing). Versuche es erneut, wenn das abgeschlossen ist.";

const trashRow = (page, id) => page.locator(`#catalogList .cat-item.in-trash[data-id="${id}"]`);

async function seedTrash(page) {
  await seedCatalogState(page, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: [], color: null },
      { id: "trash", name: "Trash", items: [JOB_ID, SIBLING_JOB_ID], color: null },
    ],
    tracks: {
      [JOB_ID]: fixtureTrack(JOB_ID, TRACK_TITLE),
      [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, "Second Trashed"),
    },
  });
  await stubUpdateCheck(page);
  // The startup sync would trash the shared fixture jobs on the server.
  await page.route(/\/api\/jobs\/[^/]+\/(trash|restore)$/, (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "{}" }));
  const deletes = [];
  await page.route(/\/api\/jobs\/[a-f0-9]{12}$/, (route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    const id = route.request().url().split("/").pop();
    deletes.push(id);
    return id === JOB_ID ? route.fulfill(BUSY) : route.fulfill({ json: { job_id: id, status: "deleted" } });
  });
  return deletes;
}

test("emptying the Trash keeps a song still being separated, and says why", async ({ page }) => {
  const deletes = await seedTrash(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(".rail-trash").click();
  await expect(trashRow(page, JOB_ID)).toBeVisible();

  await page.locator("#clearBinBtn").click();

  await expect(page.locator("#error")).toBeVisible();
  await expect(page.locator("#error")).toContainText(EN);
  await expect(page.locator("#error")).not.toContainText("server words");
  await expect(page.locator("#error")).not.toContainText("could not be deleted");
  expect(deletes.sort()).toEqual([JOB_ID, SIBLING_JOB_ID].sort());
  await expect(trashRow(page, JOB_ID)).toBeVisible();
  await expect(trashRow(page, SIBLING_JOB_ID)).toHaveCount(0);
  // Kept as well, not only on screen: in the Trash, with no tombstone, so it
  // can be emptied once the separation is over.
  const state = await readCatalogState(page);
  expect(state.folders.find((f) => f.id === "trash").items).toEqual([JOB_ID]);
  expect(Object.keys(state.tracks)).toContain(JOB_ID);
  const tombstones = await page.evaluate(() => JSON.parse(localStorage.getItem("stemdeck.deleted_jobs") || "[]"));
  expect(tombstones).toContain(SIBLING_JOB_ID);
  expect(tombstones).not.toContain(JOB_ID);
});

test("a reset refused while a song is being separated says why, in the interface language", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("stemdeck.language", JSON.stringify("de")));
  await seedLibrary(page);
  const resets = [];
  await page.route("**/api/reset", (route) => {
    resets.push(route.request().method());
    return route.fulfill(BUSY);
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  await page.locator('.settings-tab[data-tab="library"]').click();
  await page.locator(".settings-reset-btn").click();
  await page.locator(".reset-confirm-input").fill("RESET");
  await page.locator(".reset-confirm-go").click();

  await expect(page.locator(".reset-confirm-msg")).toHaveText(DE);
  expect(resets).toEqual(["POST"]);
  // Nothing was cleared: the page is still there, and the button works again.
  await expect(page.locator(".reset-confirm-go")).toBeEnabled();
});

test("a library move refused while a song is being separated says why", async ({ page }) => {
  await page.route("**/api/settings/stems-location", (route) =>
    route.request().method() === "GET"
      ? route.fulfill({
          json: {
            path: "/Users/someone/Documents/StemDeck/jobs",
            bytes: 1024,
            editable: true,
            is_default: true,
            restart_required: false,
            busy: false,
          },
        })
      : route.fulfill(BUSY));
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  await page.locator('.settings-tab[data-tab="library"]').click();
  page.once("dialog", (d) => d.accept("D:\\Elsewhere"));
  await page.locator(".set-stems-location").click();

  const msg = page.locator(".stems-location-msg");
  await expect(msg).toHaveText(EN);
  await expect(msg).toHaveClass(/\berror\b/);
});
