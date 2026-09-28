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
    // Waited for, not read at once: the request can go out just after the text.
    await expect.poll(() => asked).toContain(JOB_ID);
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

// ── only Wikipedia searched, and a name typed with a token saved ──

/** Answer GET /api/settings as the server does, with discogs_token_set as given. */
async function stubTokenSet(page, set) {
  await page.route("**/api/settings", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const response = await route.fetch();
    const json = await response.json();
    return route.fulfill({ response, json: { ...json, discogs_token_set: set } });
  });
}

const CANDIDATES = {
  candidates: [
    { id: 555501, name: "Nihil (5)", profile: "Portuguese sludge band from Porto, formed in 2016." },
    { id: 12, name: "Nihil", profile: "German industrial project." },
  ],
};

/** Answer the name search and the picked artist, recording what was asked. */
async function stubDiscogsSearch(page, answer = CANDIDATES) {
  const asked = { names: [], ids: [] };
  await page.route(/\/api\/discogs\/artist\?/, (route) => {
    asked.names.push(new URL(route.request().url()).searchParams.get("q"));
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(answer) });
  });
  await page.route(/\/api\/discogs\/artist\/\d+$/, (route) => {
    asked.ids.push(new URL(route.request().url()).pathname.split("/").pop());
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(EXTRA) });
  });
  return asked;
}

const NOTE =
  "Only Wikipedia was searched. Add a free Discogs token in Settings, Song details, to also search Discogs, which covers many smaller and independent bands.";

test.describe("artist box: only Wikipedia, or Discogs too", () => {
  test("with no token, a name Wikipedia does not know says so and opens Song details", async ({ page }) => {
    await stubTokenSet(page, false);
    await stubWikimedia(page, null);
    await stubExtra(page, null);
    const asked = await stubDiscogsSearch(page);
    await openTagged(page);
    const dialog = page.locator("#artistDialog");

    await expect(dialog.locator(".artist-status")).toHaveText("Nothing on Wikipedia matches “NIHIL” as a band or artist.");
    const note = dialog.locator(".artist-discogs-note");
    await expect(note).toContainText(NOTE);
    const open = note.locator(".artist-discogs-open");
    await expect(open).toHaveText("Open Song details settings");
    // No token: Discogs is not searched, not even by the server.
    expect(asked.names).toEqual([]);

    await open.click();
    await expect(dialog).toBeHidden();
    const settings = page.locator(".library-editor-backdrop");
    await expect(settings).toBeVisible();
    await expect(settings.locator('.settings-tab[data-tab="details"]')).toHaveClass(/active/);
    await expect(settings.locator('.settings-pane[data-pane="details"]')).toBeVisible();
    await expect(settings.locator(".set-discogs-token")).toBeFocused();
  });

  test("with no token, a band Wikipedia has little on gets the note too", async ({ page }) => {
    await stubTokenSet(page, false);
    await stubWikimedia(page, wikidataNihil({ article: false }));
    await stubExtra(page, null);
    await openTagged(page);
    const dialog = page.locator("#artistDialog");
    await expect(dialog.locator(".artist-name")).toHaveText("Nihil");
    await expect(dialog.locator(".artist-discogs-note")).toContainText(NOTE);
    await expect(dialog.locator(".artist-source")).toHaveText("From Wikipedia and Wikidata, CC BY-SA");
  });

  test("never under a band Wikipedia covers in full", async ({ page }) => {
    await stubTokenSet(page, false);
    await stubWikimedia(page, wikidataNihil({ article: true }));
    await stubExtra(page, null);
    await openTagged(page);
    const dialog = page.locator("#artistDialog");
    await expect(dialog.locator(".artist-history")).toContainText("From the Wikipedia article.");
    await page.waitForTimeout(500);
    await expect(dialog.locator(".artist-discogs-note")).toHaveCount(0);
  });

  test("with a token saved, no note, even where Discogs had nothing", async ({ page }) => {
    await stubTokenSet(page, true);
    await stubWikimedia(page, wikidataNihil({ article: false }));
    await stubExtra(page, null);
    await openTagged(page);
    const dialog = page.locator("#artistDialog");
    await expect(dialog.locator(".artist-name")).toHaveText("Nihil");
    await page.waitForTimeout(500);
    await expect(dialog.locator(".artist-discogs-note")).toHaveCount(0);
  });

  test("with a token saved, a typed name Wikipedia does not know is found on Discogs and picked", async ({ page }) => {
    await stubTokenSet(page, true);
    await stubWikimedia(page, null);
    await stubExtra(page, null);
    const asked = await stubDiscogsSearch(page);
    const discogsAsked = await openTagged(page);
    const dialog = page.locator("#artistDialog");

    // The box's own name, found on neither: the field is open for another.
    await expect(dialog.locator(".artist-candidate")).toHaveCount(2);
    await dialog.locator("#artistQuery").fill("Nihil Porto");
    await dialog.locator(".artist-search-btn").click();
    await expect.poll(() => asked.names).toContain("Nihil Porto");

    await expect(dialog.locator(".artist-status")).toHaveText("Nothing on Wikipedia matches “Nihil Porto” as a band or artist.");
    await expect(dialog.locator(".artist-discogs-pick .artist-section-title")).toHaveText("Found on Discogs");
    const candidates = dialog.locator(".artist-candidate");
    await expect(candidates.locator(".artist-candidate-name")).toHaveText(["Nihil (5)", "Nihil"]);
    await expect(candidates.first()).toContainText("Portuguese sludge band from Porto");
    await expect(dialog.locator(".artist-discogs-credit")).toHaveText("Data provided by Discogs");
    await expect(dialog.locator(".artist-discogs-note")).toHaveCount(0);

    await candidates.first().click();
    await expect(dialog.locator(".artist-name")).toHaveText("Nihil");
    expect(asked.ids).toEqual(["555501"]);
    await expect(dialog.locator(".artist-history")).toContainText("Portuguese sludge band from Porto");
    await expect(dialog.locator(".artist-source")).toHaveText("From Discogs");
    const credit = dialog.locator(".artist-discogs-credit");
    await expect(credit).toHaveText("Data provided by Discogs");
    await expect(credit).toHaveAttribute("href", DISCOGS_PAGE);
    await expect(dialog.locator(".artist-save")).toHaveCount(0);
    await expect(dialog.locator(".artist-other-btn")).toHaveText("Not this band? Search");
    expect(discogsAsked).toEqual([]);
  });

  test("with a token saved and nothing on Discogs either, it says both were searched", async ({ page }) => {
    await stubTokenSet(page, true);
    await stubWikimedia(page, null);
    await stubExtra(page, null);
    await stubDiscogsSearch(page, { candidates: [] });
    await openTagged(page);
    const dialog = page.locator("#artistDialog");
    await expect(dialog.locator(".artist-status")).toHaveText("Nothing on Wikipedia or Discogs matches “NIHIL” as a band or artist.");
    await expect(dialog.locator(".artist-discogs-note")).toHaveCount(0);
    await expect(dialog.locator("#artistQuery")).toBeVisible();
  });
});
