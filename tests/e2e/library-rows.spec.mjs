// A library row says the same thing about a track however it got there, and
// each track appears once.
//
// The line under a title used to read a `channel` field that six code paths
// filled with a status label, as text in whatever language was active, and
// the path that adopts a server job the library did not know about filled with
// nothing. So two copies of one song read "Extracted · 6 stems" and " · 6
// stems" side by side (#656), and the adopted one's count came from the stems
// it had asked for rather than the ones it has, which can be none at all.
//
// And a Recent section above the folders listed the three newest tracks, each
// of which is also in its folder, so one import appeared twice and two imports
// of the same song four times.
import { test, expect } from "@playwright/test";
import {
  JOB_ID,
  SIBLING_JOB_ID,
  fixtureTrack,
  seedCatalogState,
  seedLibrary,
  stubExportEndpoints,
  stubUpdateCheck,
} from "./helpers.mjs";

const rowText = (page, id) =>
  page
    .locator(`.cat-item[data-id="${id}"] .cat-sub`)
    .first()
    .evaluate((el) => el.textContent.replace(/\s+/g, " ").trim());

async function open(page) {
  await stubExportEndpoints(page);
  await stubUpdateCheck(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(".cat-item").first().waitFor({ timeout: 20000 });
}

/** The stems a job really has, from the server, not counting the mix. */
async function stemFiles(page, id) {
  const res = await page.request.get(`/api/jobs/${id}`);
  const state = await res.json();
  return (state.stems || []).filter((s) => (s.name ?? s) !== "original").length;
}

// Both fixture jobs, as seedLibrary has them, with the first one changed.
// Seeding only the first would not work: the startup sync adopts the second
// from the server, and as they share a source the catalog's dedup (#542)
// replaces the first with it.
function withTracks(first) {
  const tracks = {
    [JOB_ID]: first,
    [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, "E2E Fixture Track (again)"),
  };
  return {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: Object.keys(tracks), color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks,
  };
}

test.describe("library rows", () => {
  test("a track adopted from the server reads like any other", async ({ page }) => {
    // No local library at all: both jobs arrive through the startup sync, the
    // path that used to leave the line as " · 0 stems".
    await open(page);
    const expected = await stemFiles(page, SIBLING_JOB_ID);
    expect(expected).toBeGreaterThan(0);

    const line = await rowText(page, SIBLING_JOB_ID);
    expect(line).not.toMatch(/^·/);
    expect(line).toMatch(new RegExp(`(^|· )${expected} stems$`));
  });

  test("an old stored label is not shown, and not searched", async ({ page }) => {
    // Tracks saved before this change still carry the label in `channel`.
    await seedCatalogState(
      page,
      withTracks({ ...fixtureTrack(JOB_ID, "Labelled"), channel: "Extracted" }),
    );
    await open(page);

    expect(await rowText(page, JOB_ID)).not.toContain("Extracted");

    await page.locator("#catalogSearch, .cat-search input, input[type=search]").first().fill("Extracted");
    await expect(page.locator(`.cat-item[data-id="${JOB_ID}"]`)).toHaveCount(0);
  });

  test("a track still being processed says so", async ({ page }) => {
    await seedCatalogState(
      page,
      withTracks({ ...fixtureTrack(JOB_ID, "Busy"), status: "separating" }),
    );
    await open(page);
    expect(await rowText(page, JOB_ID)).toBe("Processing");
  });

  test("a failed import says so", async ({ page }) => {
    await seedCatalogState(
      page,
      withTracks({ ...fixtureTrack(JOB_ID, "Broken"), status: "error" }),
    );
    await open(page);
    expect(await rowText(page, JOB_ID)).toBe("Import failed");
  });

  test("the line follows the language rather than keeping the one it was saved in", async ({ page }) => {
    await page.addInitScript(() =>
      localStorage.setItem("stemdeck.language", JSON.stringify("de")),
    );
    await seedCatalogState(
      page,
      withTracks({ ...fixtureTrack(JOB_ID, "Beschriftet"), channel: "Extracted", status: "separating" }),
    );
    await open(page);
    // German, and nothing left over from the English it was stored in.
    expect(await rowText(page, JOB_ID)).toBe("Wird verarbeitet");
  });

  test("each track is listed once", async ({ page }) => {
    await seedLibrary(page);
    await open(page);

    await expect(page.locator(`.cat-item[data-id="${JOB_ID}"]`)).toHaveCount(1);
    await expect(page.locator(`.cat-item[data-id="${SIBLING_JOB_ID}"]`)).toHaveCount(1);
    await expect(page.locator(".lib-section-head", { hasText: /^Recent$/i })).toHaveCount(0);
  });

  test("a search that matches nothing says so in the chosen language", async ({ page }) => {
    await page.addInitScript(() =>
      localStorage.setItem("stemdeck.language", JSON.stringify("de")),
    );
    await seedLibrary(page);
    await open(page);

    await page.locator("#catalogSearch, .cat-search input, input[type=search]").first().fill("zzzz-nothing");
    await expect(page.getByText("Keine Titel entsprechen deiner Suche")).toBeVisible();
  });
});
