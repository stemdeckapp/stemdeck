// A trashed track stays trashed, even when another job shares its source URL
// (#542).
//
// Worth driving in a real browser rather than unit-testing addTrackToLibrary,
// because the damage is invisible in the call that does it. Evicting a trashed
// entry leaves a job on the server with no track, no trash entry and no
// tombstone. Nothing looks wrong that session. It is the *next* launch's
// syncWithServer, finding an orphan, that produces the reported symptom: the
// song is back in the library and the Trash is empty.
//
// The condition is two jobs sharing one source_url, which tests/e2e/seed.py now
// builds. That is not an exotic state: processing the same link twice is enough.
//
// The trap in writing this is the barrier. Assertions about what did *not*
// happen have to be made after the startup sync, or they pass on a page that
// never got the chance to break anything.

import { test, expect } from "@playwright/test";

import {
  JOB_ID,
  SIBLING_JOB_ID,
  TRACK_TITLE,
  fixtureTrack,
  readCatalogState,
  seedCatalogState,
  stubUpdateCheck,
} from "./helpers.mjs";

/**
 * The state a user reaches by trashing one of two extractions of the same URL.
 *
 * The trashed job is in the catalog and in the Trash. The sibling job exists
 * only on the server, so the startup sync will import it, and that import is
 * what used to take the trashed track out with it.
 */
async function seedTrashedWithUnknownSibling(page) {
  await seedCatalogState(page, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: [], color: null },
      { id: "trash", name: "Trash", items: [JOB_ID], color: null },
    ],
    tracks: { [JOB_ID]: fixtureTrack(JOB_ID, TRACK_TITLE) },
  });
  await stubUpdateCheck(page);
}

/**
 * Wait until the startup sync has imported the sibling.
 *
 * The sibling exists only on the server, so its row appearing is proof that
 * syncWithServer ran and called addTrackToLibrary. That call is the one that
 * used to evict the trashed track, so by the time the row is on screen the
 * damage, if any, has already been done. No assertion after this races it.
 */
async function waitForSiblingImport(page) {
  await expect(page.locator(`.cat-item[data-id="${SIBLING_JOB_ID}"]`).first()).toBeVisible();
}

/** Switch to the Trash view, where the list is only the trashed tracks. */
async function openTrash(page) {
  await page.locator(".rail-trash").click();
  await expect(page.locator("#catalogPanel")).toHaveClass(/trash-view/);
}

test.describe("a trashed track and a sibling job with the same source", () => {
  test("the sibling is imported without evicting the trashed track", async ({ page }) => {
    await seedTrashedWithUnknownSibling(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await waitForSiblingImport(page);

    await openTrash(page);
    await expect(page.locator(`#catalogList .cat-item[data-id="${JOB_ID}"]`)).toHaveCount(1);

    // Persisted, not merely on screen. The next launch reads this back, and
    // the trash entry is the only thing that stops syncWithServer re-adopting
    // the job: there is no tombstone for a soft delete.
    const state = await readCatalogState(page);
    expect(state.folders.find((f) => f.id === "trash").items).toContain(JOB_ID);
    expect(Object.keys(state.tracks)).toContain(JOB_ID);
  });

  test("the trashed track is still in the Trash after a restart", async ({ page }) => {
    await seedTrashedWithUnknownSibling(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await waitForSiblingImport(page);

    // The restart is the whole bug. Everything above can look correct and the
    // song still comes back here.
    const synced = page.waitForResponse(
      (r) => new URL(r.url()).pathname === "/api/jobs" && r.request().method() === "GET",
    );
    await page.reload({ waitUntil: "domcontentloaded" });
    await synced;
    // A settle, because what follows is an assertion that nothing appeared.
    // waitForResponse returns when the bytes land; the re-adoption and the
    // re-render happen after that, and toHaveCount(0) would happily pass in
    // the gap.
    await page.waitForTimeout(500);

    await expect(page.locator(`.cat-item[data-id="${JOB_ID}"]`)).toHaveCount(0);
    await openTrash(page);
    await expect(page.locator(`#catalogList .cat-item[data-id="${JOB_ID}"]`)).toHaveCount(1);
  });
});
