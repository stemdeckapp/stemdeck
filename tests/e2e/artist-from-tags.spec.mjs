// A track whose file was tagged with an artist gets its band found and saved
// on its own when it is opened, and the now-playing card names the artist
// beside the title (#699). No click on the info button is involved.
//
// Every Wikimedia request is answered here, so the tests run offline. The
// answers are the Dream Theater ones artist-info.spec.mjs uses; what the box
// does with each shape is covered there and in tests/js/artist-lookup.test.mjs.
// This is what happens without the box: what is saved, what is not, what is
// asked, and what the card shows.
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

// artistInfo.js waits this long after a track opens before asking, so a test
// that proves nothing was asked has to wait longer than that.
const PAST_THE_WAIT_MS = 2000;

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

/**
 * Answer Wikimedia from ANSWERS, recording each URL asked for. `hold`, when
 * given, is awaited before the search is answered, so a test can open another
 * track while the lookup is still out.
 */
async function stubWikimedia(page, { hold = null } = {}) {
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
    if (hold && kind === "search") await hold;
    try {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { "access-control-allow-origin": "*" },
        body: JSON.stringify(ANSWERS[kind]),
      });
    } catch {
      // The page aborted the request while it was held, which is the point.
    }
  });
  return asked;
}

/** Both fixture jobs, the first carrying `tags` (none for the second). */
async function openStudio(page, tags) {
  const tracks = {
    [JOB_ID]: { ...fixtureTrack(JOB_ID, "E2E Fixture Track"), ...(tags ? { audioTags: tags } : {}) },
    [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, "E2E Fixture Track (again)"),
  };
  await seedCatalogState(page, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: Object.keys(tracks), color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks,
  });
  await stubExportEndpoints(page);
  await stubUpdateCheck(page);
  // An untagged track asks the server to read its tags when opened. None
  // here, so the tags seeded above are all there is.
  await stubAudioTags(page, null);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(".cat-item").first().waitFor({ timeout: 20000 });
}

async function openTrack(page, id = JOB_ID) {
  await page.locator(`.cat-item[data-id="${id}"]`).first().click();
  await expect(page.locator(".app")).not.toHaveClass(/no-track/, { timeout: 15000 });
}

const savedArtist = async (page, id = JOB_ID) => (await readCatalogState(page))?.tracks?.[id]?.artist ?? null;

test.describe("band from the file's tags", () => {
  test("a tagged track gets its band saved without a click, and the card names it", async ({ page }) => {
    const asked = await stubWikimedia(page);
    await openStudio(page, { artist: "Dream Theater", title: "Metropolis Pt. 1" });
    await openTrack(page);

    // The file's own title rather than the job's, with the artist beside it.
    const artist = page.locator("#np-artist");
    await expect(page.locator("#title")).toHaveText("Metropolis Pt. 1");
    await expect(artist).toBeVisible();
    await expect(artist).toHaveText("Dream Theater");

    await expect.poll(() => savedArtist(page), { timeout: 15000 })
      .toEqual({ id: "Q162586", name: "Dream Theater", englishName: "Dream Theater" });
    // The tag's artist, to Wikimedia, and nothing else about the track.
    expect(asked[0]).toContain("search=Dream+Theater");
    expect(asked.join(" ")).not.toContain("Metropolis");
    expect(asked.join(" ")).not.toContain("E2E");

    // On the title's line, which is what keeps the card at its height.
    const [titleBox, artistBox] = await Promise.all([
      page.locator("#title").boundingBox(),
      artist.boundingBox(),
    ]);
    expect(Math.abs(titleBox.y - artistBox.y)).toBeLessThan(3);
    expect(artistBox.x).toBeGreaterThan(titleBox.x);

    // The box opens straight on the saved band, from the answer already had.
    const before = asked.length;
    await page.locator("#np-details-btn").click();
    await expect(page.locator(".artist-name")).toHaveText("Dream Theater");
    await expect(page.locator(".artist-save")).toHaveText("Saved for this track");
    expect(asked.length).toBe(before);
  });

  test("an untagged track shows no artist and asks Wikimedia nothing", async ({ page }) => {
    const asked = await stubWikimedia(page);
    await openStudio(page, null);
    await openTrack(page);
    await expect(page.locator("#title")).toHaveText("E2E Fixture Track");
    await expect(page.locator("#np-artist")).toBeHidden();
    await page.waitForTimeout(PAST_THE_WAIT_MS);
    expect(asked).toEqual([]);
    expect(await savedArtist(page)).toBeNull();
  });

  test("an answer with a different name is not saved", async ({ page }) => {
    const asked = await stubWikimedia(page);
    // Wikidata's search forgives the spelling and answers Dream Theater.
    await openStudio(page, { artist: "Dream Theatre" });
    await openTrack(page);
    await expect.poll(() => asked.some((url) => url.includes("action=wbgetentities")), { timeout: 15000 }).toBe(true);
    await page.waitForTimeout(PAST_THE_WAIT_MS);
    expect(await savedArtist(page)).toBeNull();
    // The card still names what the file says.
    await expect(page.locator("#np-artist")).toHaveText("Dream Theatre");
    // And the job's title stays, with no title tag to prefer.
    await expect(page.locator("#title")).toHaveText("E2E Fixture Track");
  });

  test("an answer landing after another track was opened is saved on neither", async ({ page }) => {
    let release;
    const hold = new Promise((resolve) => { release = resolve; });
    const asked = await stubWikimedia(page, { hold });
    await openStudio(page, { artist: "Dream Theater" });
    await openTrack(page);
    await expect.poll(() => asked.length, { timeout: 15000 }).toBeGreaterThan(0);

    await openTrack(page, SIBLING_JOB_ID);
    await expect(page.locator("#title")).toHaveText("E2E Fixture Track (again)", { timeout: 15000 });
    await expect(page.locator("#np-artist")).toBeHidden();
    release();
    await page.waitForTimeout(PAST_THE_WAIT_MS);
    expect(await savedArtist(page, JOB_ID)).toBeNull();
    expect(await savedArtist(page, SIBLING_JOB_ID)).toBeNull();
  });
});
