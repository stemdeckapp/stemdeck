// The artist box fills a band from Discogs when Wikipedia has none of it:
// NIHIL, a Portuguese sludge band with a Discogs page and no Wikipedia
// article. The page never talks to Discogs; the server does, with the user's
// token, and answers GET /api/jobs/{id}/artist-extra. That endpoint is
// stubbed here with the shape the server gives (tests/test_artist_extra_api.py),
// and Wikimedia is answered too, so the run is offline.
import { test, expect } from "@playwright/test";
import {
  JOB_ID,
  SIBLING_JOB_ID,
  fixtureTrack,
  seedCatalogState,
  stubAudioTags,
  stubExportEndpoints,
  stubUpdateCheck,
} from "./helpers.mjs";

// Wide enough for the now-playing card, which the bar hides below 1460px.
test.use({ viewport: { width: 1600, height: 900 } });

const WIKIMEDIA = /^https:\/\/([a-z]+\.wikipedia\.org|www\.wikidata\.org|query\.wikidata\.org)\//;
const DISCOGS_PAGE = "https://www.discogs.com/artist/555501-Nihil-5";

const EXTRA = {
  id: 555501,
  name: "Nihil",
  real_name: "",
  profile: [
    "Portuguese sludge band from Porto, formed in 2016 by Rui Barros and Ana Lima.",
    "Their debut was released on Raging Planet in 2021.",
  ],
  members: { current: ["Rui Barros", "Ana Lima"], former: ["Pedro Sá"] },
  groups: [],
  links: [
    { kind: "website", url: "http://www.nihilband.pt/" },
    { kind: "bandcamp", url: "https://nihil.bandcamp.com/" },
    { kind: "instagram", url: "https://www.instagram.com/nihil.porto/" },
    { kind: "facebook", url: "https://www.facebook.com/nihilporto" },
    { kind: "youtube", url: "https://www.youtube.com/@nihilporto" },
  ],
  releases: [
    { year: "2019", title: "Barro" },
    { year: "2021", title: "Lama" },
  ],
  url: DISCOGS_PAGE,
};

const member = (id) => ({ rank: "normal", mainsnak: { datavalue: { value: { id } } }, qualifiers: {} });

// A Wikidata band called Nihil with members and albums but no article, and
// optionally with its history too.
function wikidataNihil({ article = false } = {}) {
  return {
    search: { search: [{ id: "Q424242" }] },
    entities: {
      entities: {
        Q424242: {
          id: "Q424242",
          labels: { en: { value: "Nihil" } },
          descriptions: { en: { value: "Portuguese band" } },
          sitelinks: article
            ? { enwiki: { site: "enwiki", title: "Nihil (band)", url: "https://en.wikipedia.org/wiki/Nihil_(band)" } }
            : {},
          claims: { P434: [{}], P527: [member("Q1001")] },
        },
      },
    },
    members: { entities: { Q1001: { labels: { en: { value: "Wiki Member" } } } } },
    sparql: {
      results: {
        bindings: [
          {
            album: { value: "http://www.wikidata.org/entity/Q2002" },
            albumLabel: { value: "Wiki Album" },
            released: { value: "2021-01-01T00:00:00Z" },
          },
        ],
      },
    },
    extract: { query: { pages: [{ extract: "Lead.\n== History ==\nFrom the Wikipedia article." }] } },
  };
}

async function stubWikimedia(page, answers) {
  await page.route(WIKIMEDIA, async (route) => {
    const url = new URL(route.request().url());
    const kind =
      url.host === "query.wikidata.org" ? "sparql"
      : url.host.endsWith("wikipedia.org") ? "extract"
      : url.searchParams.get("action") === "wbsearchentities" ? "search"
      : url.searchParams.get("props").startsWith("labels") ? "members"
      : "entities";
    const body = answers ? answers[kind] : kind === "search" ? { search: [] } : {};
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "access-control-allow-origin": "*" },
      body: JSON.stringify(body),
    });
  });
}

/** Answer the Discogs endpoint, recording which jobs were asked about. */
async function stubExtra(page, answer = EXTRA) {
  const asked = [];
  await page.route("**/api/jobs/*/artist-extra", (route) => {
    asked.push(new URL(route.request().url()).pathname.split("/")[3]);
    return answer
      ? route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(answer) })
      : route.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"no artist details"}' });
  });
  return asked;
}

async function openTagged(page) {
  await stubAudioTags(page, null);
  await seedCatalogState(page, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: [JOB_ID, SIBLING_JOB_ID], color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks: {
      [JOB_ID]: { ...fixtureTrack(JOB_ID, "NIHIL | Barro"), audioTags: { artist: "NIHIL", title: "Barro" }, workChecked: true },
      [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, "E2E Fixture Track (again)"),
    },
  });
  await stubExportEndpoints(page);
  await stubUpdateCheck(page);
  // The browser must never reach Discogs itself: only the server does.
  const discogsAsked = [];
  page.on("request", (request) => {
    if (/discogs\.com/.test(new URL(request.url()).host)) discogsAsked.push(request.url());
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(".cat-item").first().waitFor({ timeout: 20000 });
  await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
  await expect(page.locator(".app")).not.toHaveClass(/no-track/, { timeout: 15000 });
  await page.locator("#np-details-btn").click();
  return discogsAsked;
}

test.describe("artist box from Discogs", () => {
  test("a band Wikipedia has no article on is shown from Discogs", async ({ page }) => {
    await stubWikimedia(page, null);
    const asked = await stubExtra(page);
    const discogsAsked = await openTagged(page);
    const dialog = page.locator("#artistDialog");

    await expect(dialog.locator(".artist-name")).toHaveText("Nihil");
    expect(asked).toContain(JOB_ID);
    await expect(dialog.locator(".artist-section-title")).toHaveText(["History", "Members", "Releases"]);
    await expect(dialog.locator(".artist-history")).toContainText("Portuguese sludge band from Porto");
    await expect(dialog.locator(".artist-names").first()).toContainText("Rui Barros");
    await expect(dialog.locator(".artist-subtitle")).toHaveText("Former members");
    await expect(dialog.locator(".artist-names").nth(1)).toContainText("Pedro Sá");
    await expect(dialog.locator(".artist-albums li")).toHaveCount(2);
    await expect(dialog.locator(".artist-albums")).toContainText("2019");
    await expect(dialog.locator(".artist-albums")).toContainText("Barro");

    // The band's own links, each opened outside the app.
    const links = dialog.locator(".artist-links .artist-link");
    await expect(links).toHaveText(["Website", "Bandcamp", "Instagram", "Facebook", "YouTube"]);
    await expect(dialog.locator(".artist-link-bandcamp")).toHaveAttribute("href", "https://nihil.bandcamp.com/");
    await expect(dialog.locator(".artist-link-bandcamp")).toHaveAttribute("target", "_blank");

    // Credited to Discogs, linked to the band's page there, as its terms ask.
    await expect(dialog.locator(".artist-source")).toHaveText("From Discogs");
    const credit = dialog.locator(".artist-discogs-credit");
    await expect(credit).toHaveText("Data provided by Discogs");
    await expect(credit).toHaveAttribute("href", DISCOGS_PAGE);
    await expect(credit).toHaveAttribute("target", "_blank");

    // Not a Wikidata band, so nothing to save on the track; the search is a
    // click away for a wrong one.
    await expect(dialog.locator(".artist-save")).toHaveCount(0);
    await expect(dialog.locator(".artist-other-btn")).toHaveText("Not this band? Search");
    expect(discogsAsked).toEqual([]);
  });

  test("with no Discogs token the box says nothing was found, as before", async ({ page }) => {
    await stubWikimedia(page, null);
    const asked = await stubExtra(page, null);
    await openTagged(page);
    await expect(page.locator("#artistDialog .artist-status")).toContainText("NIHIL");
    expect(asked).toContain(JOB_ID);
    await expect(page.locator(".artist-discogs-credit")).toHaveCount(0);
  });

  test("with a Wikipedia band, Discogs only fills its gaps", async ({ page }) => {
    await stubWikimedia(page, wikidataNihil({ article: false }));
    await stubExtra(page);
    await openTagged(page);
    const dialog = page.locator("#artistDialog");

    await expect(dialog.locator(".artist-name")).toHaveText("Nihil");
    // No article: the history is Discogs', and says so.
    await expect(dialog.locator(".artist-history")).toContainText("Portuguese sludge band");
    await expect(dialog.locator(".artist-from-discogs")).toHaveText(["From Discogs"]);
    // Wikidata's members and albums stay; Discogs' are not added to them.
    await expect(dialog.locator(".artist-names").first()).toHaveText("Wiki Member");
    await expect(dialog.locator(".artist-subtitle")).toHaveCount(0);
    await expect(dialog.locator(".artist-albums")).toContainText("Wiki Album");
    await expect(dialog.locator(".artist-albums")).not.toContainText("Barro");
    await expect(dialog.locator(".artist-section-title")).toHaveText(["History", "Members", "Studio albums"]);
    // Links Wikidata has none of come from Discogs, marked as such.
    await expect(dialog.locator(".artist-link.from-discogs")).toHaveCount(5);
    // Credited to both, and still a Wikidata band that can be saved.
    await expect(dialog.locator(".artist-source")).toHaveText("From Wikipedia and Wikidata, CC BY-SA");
    await expect(dialog.locator(".artist-discogs-credit")).toHaveAttribute("href", DISCOGS_PAGE);
    await expect(dialog.locator(".artist-save")).toBeVisible();
  });

  test("with a complete Wikipedia band, Discogs is not asked", async ({ page }) => {
    await stubWikimedia(page, wikidataNihil({ article: true }));
    const asked = await stubExtra(page);
    await openTagged(page);
    const dialog = page.locator("#artistDialog");
    await expect(dialog.locator(".artist-history")).toContainText("From the Wikipedia article.");
    await expect(dialog.locator(".artist-more")).toHaveAttribute("href", "https://en.wikipedia.org/wiki/Nihil_(band)");
    await page.waitForTimeout(500);
    expect(asked).toEqual([]);
    await expect(dialog.locator(".artist-discogs-credit")).toHaveCount(0);
    await expect(dialog.locator(".artist-from-discogs")).toHaveCount(0);
  });
});
