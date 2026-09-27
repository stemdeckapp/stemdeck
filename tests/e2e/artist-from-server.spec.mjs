// The band arrives with the finished import (#699): the server looks it up from
// the artist tag while the job separates, and the job's state carries it. The
// page keeps it on the track and shows it with no lookup of its own and no
// click on the info button.
//
// Every Wikimedia request is answered here and recorded, so the tests can
// assert the page asked Wikimedia nothing at all until the box was opened, and
// then only for the saved band by its id, never a search.
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

// Wide enough for the now-playing card, which the bar hides below 1460px.
test.use({ viewport: { width: 1600, height: 900 } });

const WIKIMEDIA = /^https:\/\/([a-z]+\.wikipedia\.org|www\.wikidata\.org|query\.wikidata\.org)\//;

// artistInfo.js waits this long after a track opens before looking the band
// up itself, so a test that proves it did not has to wait longer than that.
const PAST_THE_WAIT_MS = 2000;

const BAND = { id: "Q162586", name: "Dream Theater", englishName: "Dream Theater" };
const TAGS = { artist: "Dream Theater", title: "Pull Me Under" };

const ANSWERS = {
  search: { search: [{ id: "Q162586" }] },
  entities: {
    entities: {
      Q162586: {
        id: "Q162586",
        labels: { en: { value: "Dream Theater" } },
        descriptions: { en: { value: "American progressive metal band" } },
        sitelinks: {
          enwiki: { site: "enwiki", title: "Dream Theater", url: "https://en.wikipedia.org/wiki/Dream_Theater" },
        },
        claims: { P434: [{}] },
      },
    },
  },
  members: { entities: {} },
  sparql: { results: { bindings: [] } },
  extract: { query: { pages: [{ extract: "Lead.\n== History ==\nIn 1985, three Berklee students formed a band." }] } },
};

async function stubWikimedia(page) {
  const asked = [];
  await page.route(WIKIMEDIA, async (route) => {
    const url = new URL(route.request().url());
    asked.push(url.toString());
    const kind =
      url.host === "query.wikidata.org" ? "sparql"
      : url.host.endsWith("wikipedia.org") ? "extract"
      : url.searchParams.get("action") === "wbsearchentities" ? "search"
      : url.searchParams.get("props") === "labels" ? "members"
      : "entities";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "access-control-allow-origin": "*" },
      body: JSON.stringify(ANSWERS[kind]),
    });
  });
  return asked;
}

/** The fixture job's real state, as the server would send it once the
 *  pipeline had found `band` from `tags`. */
async function stubServerBand(page, id, { band = BAND, tags = TAGS } = {}) {
  const withBand = async () => {
    const real = await (await page.request.get(`/api/jobs/${id}`)).json();
    return { ...real, audio_tags: tags, artist: band };
  };
  await page.route(`**/api/jobs/${id}`, async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(await withBand()) });
  });
  return withBand;
}

async function openPage(page, tracks) {
  await seedCatalogState(page, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: Object.keys(tracks), color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks,
  });
  await stubExportEndpoints(page);
  await stubUpdateCheck(page);
  await stubAudioTags(page, null);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(".cat-item").first().waitFor({ timeout: 20000 });
}

const savedArtist = async (page, id = JOB_ID) => (await readCatalogState(page))?.tracks?.[id]?.artist ?? null;

test.describe("band found by the server during the import", () => {
  test("a finished import shows its band at once, with nothing asked of Wikimedia", async ({ page }) => {
    const asked = await stubWikimedia(page);
    const doneState = await stubServerBand(page, JOB_ID);
    // The import: the server accepts it as the fixture job, and its event
    // stream says it is done, carrying the band the pipeline found.
    await page.route("**/api/jobs", (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ job_id: JOB_ID }) });
    });
    await page.route(`**/api/jobs/${JOB_ID}/events`, async (route) =>
      route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: `data: ${JSON.stringify(await doneState())}\n\n`,
      }));
    // Only the sibling is in the library, so nothing is open and the import
    // takes the studio.
    await openPage(page, { [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, "E2E Fixture Track (again)") });

    await page.locator("#url").fill("https://www.youtube.com/watch?v=dQw4w9WgXcQ");
    await page.locator("#submit").click();

    await expect(page.locator(".app")).not.toHaveClass(/no-track/, { timeout: 15000 });
    await expect(page.locator("#np-artist")).toHaveText("Dream Theater", { timeout: 15000 });
    await expect(page.locator("#title")).toHaveText("Pull Me Under");
    await expect.poll(() => savedArtist(page), { timeout: 15000 }).toEqual(BAND);

    await page.waitForTimeout(PAST_THE_WAIT_MS);
    expect(asked, "the page looked nothing up itself").toEqual([]);

    // The box opens straight on the band's details, asked for by its id.
    await page.locator("#np-details-btn").click();
    await expect(page.locator(".artist-name")).toHaveText("Dream Theater");
    await expect(page.locator(".artist-history")).toContainText("Berklee");
    await expect(page.locator(".artist-save")).toHaveText("Saved for this track");
    expect(asked.some((url) => url.includes("action=wbsearchentities"))).toBe(false);
    expect(asked.some((url) => url.includes("ids=Q162586"))).toBe(true);
  });

  test("a library track learns its band from the server, and one saved by hand wins", async ({ page }) => {
    const asked = await stubWikimedia(page);
    await stubServerBand(page, JOB_ID);
    const chosen = { id: "Q1", name: "Chosen Band", englishName: "Chosen Band" };
    await stubServerBand(page, SIBLING_JOB_ID, { tags: { artist: "Dream Theater", title: "Metropolis" } });
    await openPage(page, {
      [JOB_ID]: fixtureTrack(JOB_ID, "E2E Fixture Track"),
      [SIBLING_JOB_ID]: { ...fixtureTrack(SIBLING_JOB_ID, "E2E Fixture Track (again)"), artist: chosen },
    });

    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    await expect(page.locator("#np-artist")).toHaveText("Dream Theater", { timeout: 15000 });
    await expect.poll(() => savedArtist(page), { timeout: 15000 }).toEqual(BAND);

    await page.locator(`.cat-item[data-id="${SIBLING_JOB_ID}"]`).first().click();
    await expect(page.locator("#title")).toHaveText("Metropolis", { timeout: 15000 });
    await expect(page.locator("#np-artist")).toHaveText("Chosen Band");
    expect(await savedArtist(page, SIBLING_JOB_ID)).toEqual(chosen);

    await page.waitForTimeout(PAST_THE_WAIT_MS);
    expect(asked).toEqual([]);
  });
});
