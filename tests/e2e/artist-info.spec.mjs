// The artist box the info button on the now-playing card opens (#699): band name
// on top, then History, Members and Studio albums, from Wikidata and
// Wikipedia, and a band saved on the track so it opens straight on it later.
//
// Every Wikimedia request is answered here, so the tests run offline and do
// not depend on what an article says today. The answers are trimmed from the
// real ones for Dream Theater. What the box does with each shape of answer is
// covered in tests/js/artist-lookup.test.mjs; this is the page: that the card
// opens it, what is and is not sent, and what each outcome looks like.
import { test, expect } from "@playwright/test";
import {
  JOB_ID,
  SIBLING_JOB_ID,
  fixtureTrack,
  readCatalogState,
  seedCatalogState,
  seedLibrary,
  stubAudioTags,
  stubExportEndpoints,
  stubUpdateCheck,
} from "./helpers.mjs";

// Wide enough for the now-playing card, which the bar hides below 1460px.
test.use({ viewport: { width: 1600, height: 900 } });

const WIKIMEDIA = /^https:\/\/([a-z]+\.wikipedia\.org|www\.wikidata\.org|query\.wikidata\.org)\//;

const member = (id, ended = false) => ({
  rank: "normal",
  mainsnak: { datavalue: { value: { id } } },
  qualifiers: ended ? { P582: [{}] } : {},
});

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
        claims: { P434: [{}], P527: [member("Q175102"), member("Q296039", true)] },
      },
    },
  },
  members: {
    entities: {
      Q175102: { labels: { en: { value: "John Petrucci" } } },
      Q296039: { labels: { en: { value: "Kevin Moore" } } },
    },
  },
  sparql: {
    results: {
      bindings: [
        {
          album: { value: "http://www.wikidata.org/entity/Q1" },
          albumLabel: { value: "Images and Words" },
          released: { value: "1992-07-07T00:00:00Z" },
        },
      ],
    },
  },
  extract: {
    query: { pages: [{ extract: "Lead.\n== History ==\nIn 1985, three Berklee students formed a band." }] },
  },
};

/** Answer Wikimedia from ANSWERS, recording each URL asked for. */
async function stubWikimedia(page, { offline = false, nothing = false } = {}) {
  const asked = [];
  await page.route(WIKIMEDIA, async (route) => {
    const url = new URL(route.request().url());
    asked.push(url.toString());
    if (offline) return route.abort("internetdisconnected");
    const kind =
      url.host === "query.wikidata.org" ? "sparql"
      : url.host.endsWith("wikipedia.org") ? "extract"
      : url.searchParams.get("action") === "wbsearchentities" ? "search"
      : url.searchParams.get("props").startsWith("labels") ? "members"
      : "entities";
    const body = nothing && kind === "search" ? { search: [] } : ANSWERS[kind];
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "access-control-allow-origin": "*" },
      body: JSON.stringify(body),
    });
  });
  return asked;
}

async function openStudio(page) {
  await seedLibrary(page);
  await stubExportEndpoints(page);
  await stubUpdateCheck(page);
  // The untagged fixture asks for its tags when opened; it has none, so the
  // box has nothing to go on and opens on the search field.
  await stubAudioTags(page, null);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(".cat-item").first().waitFor({ timeout: 20000 });
}

async function openTrack(page) {
  await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
  await expect(page.locator(".app")).not.toHaveClass(/no-track/, { timeout: 15000 });
}

async function lookUp(page, name) {
  await page.locator("#artistQuery").fill(name);
  await page.locator("#artistQuery").press("Enter");
}

test.describe("artist box", () => {
  test("the info button opens it, with the name on top and the three sections", async ({ page }) => {
    const asked = await stubWikimedia(page);
    await openStudio(page);
    await openTrack(page);
    // Opening a track asks Wikimedia nothing: only the info button does.
    expect(asked).toEqual([]);

    await page.locator("#np-details-btn").click();
    const dialog = page.locator("#artistDialog");
    await expect(dialog).toBeVisible();
    // No band saved on the track: an empty field, focused, with nothing guessed
    // from the title and nothing searched until a name is typed.
    await expect(page.locator("#artistSearch")).toBeVisible();
    await expect(page.locator("#artistQuery")).toHaveValue("");
    await expect(page.locator("#artistQuery")).toBeFocused();
    await expect(page.locator("#artistQuery")).toHaveAttribute("placeholder", "Search for a band or artist");
    await expect(page.locator("#artistBody")).toBeEmpty();
    expect(asked).toEqual([]);

    await lookUp(page, "Dream Theater");
    await expect(dialog.locator(".artist-name")).toHaveText("Dream Theater");
    await expect(dialog.locator(".artist-section-title")).toHaveText(["History", "Members", "Studio albums"]);
    await expect(dialog.locator(".artist-history")).toContainText("Berklee students");
    await expect(dialog.locator(".artist-names").first()).toContainText("John Petrucci");
    await expect(dialog.locator(".artist-subtitle")).toHaveText("Former members");
    await expect(dialog.locator(".artist-albums")).toContainText("1992");
    await expect(dialog.locator(".artist-albums")).toContainText("Images and Words");
    await expect(dialog.locator(".artist-more")).toHaveAttribute("href", "https://en.wikipedia.org/wiki/Dream_Theater");
    await expect(dialog.locator(".artist-more")).toHaveAttribute("target", "_blank");

    // What went out is the typed name, to Wikimedia, and nothing else.
    expect(asked.length).toBeGreaterThan(0);
    expect(asked[0]).toContain("search=Dream+Theater");
    expect(asked.join(" ")).not.toContain("E2E");

    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
  });

  test("the same name again is not asked for twice", async ({ page }) => {
    const asked = await stubWikimedia(page);
    await openStudio(page);
    await openTrack(page);
    await page.locator("#np-details-btn").click();
    await lookUp(page, "Dream Theater");
    await expect(page.locator(".artist-name")).toHaveText("Dream Theater");
    const first = asked.length;

    await page.keyboard.press("Escape");
    await page.locator("#np-details-btn").click();
    await lookUp(page, "dream theater");
    await expect(page.locator(".artist-name")).toHaveText("Dream Theater");
    expect(asked.length).toBe(first);
  });

  test("a name that is no band says so", async ({ page }) => {
    await stubWikimedia(page, { nothing: true });
    await openStudio(page);
    await openTrack(page);
    await page.locator("#np-details-btn").click();
    await lookUp(page, "Not A Band At All");
    await expect(page.locator(".artist-status")).toHaveText(
      "Nothing on Wikipedia matches “Not A Band At All” as a band or artist.",
    );
  });

  test("no connection says so, rather than 'not found'", async ({ page }) => {
    await stubWikimedia(page, { offline: true });
    await openStudio(page);
    await openTrack(page);
    await page.locator("#np-details-btn").click();
    await lookUp(page, "Dream Theater");
    await expect(page.locator(".artist-status")).toHaveText(
      "Could not reach Wikipedia. Check your connection and try again.",
    );
  });

  test("the heart favourites and does not open the box", async ({ page }) => {
    await stubWikimedia(page);
    await openStudio(page);
    await openTrack(page);
    await page.locator("#fav-btn").click();
    await expect(page.locator("#artistDialog")).toBeHidden();
  });

  test("with no track open the info button does nothing", async ({ page }) => {
    const asked = await stubWikimedia(page);
    await openStudio(page);
    await expect(page.locator(".app")).toHaveClass(/no-track/);
    // CSS takes its pointer events away; dispatched, the handler still refuses.
    await page.locator("#np-details-btn").dispatchEvent("click");
    await expect(page.locator("#artistDialog")).toBeHidden();
    expect(asked).toEqual([]);
  });

  test("the info button opens it from the keyboard", async ({ page }) => {
    await stubWikimedia(page);
    await openStudio(page);
    await openTrack(page);
    await page.locator("#np-details-btn").focus();
    await page.keyboard.press("Enter");
    await expect(page.locator("#artistDialog")).toBeVisible();
  });

  test("the artist the file was tagged with opens straight on the band, without the search row", async ({ page }) => {
    const asked = await stubWikimedia(page);
    // Already tagged, and checked for the work a soundtrack is from, so its
    // tags are never asked for again.
    const tagsAsked = await stubAudioTags(page, null);
    await seedCatalogState(page, {
      folders: [
        { id: "f-unsorted", name: "Unsorted", items: [JOB_ID, SIBLING_JOB_ID], color: null },
        { id: "trash", name: "Trash", items: [], color: null },
      ],
      tracks: {
        [JOB_ID]: { ...fixtureTrack(JOB_ID, "E2E Fixture Track"), audioTags: { artist: "Dream Theater" }, workChecked: true },
        [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, "E2E Fixture Track (again)"),
      },
    });
    await stubExportEndpoints(page);
    await stubUpdateCheck(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.locator(".cat-item").first().waitFor({ timeout: 20000 });
    await openTrack(page);
    await page.locator("#np-details-btn").click();
    await expect(page.locator(".artist-name")).toHaveText("Dream Theater");
    await expect(page.locator("#artistSearch")).toBeHidden();
    await expect(page.locator("#artistClose")).toBeFocused();
    expect(asked[0]).toContain("search=Dream+Theater");
    expect(tagsAsked).toEqual([]);

    // The way back to the field, filled with the name and ready to edit.
    const other = page.locator(".artist-other-btn");
    await expect(other).toHaveText("Not this band? Search");
    await other.click();
    await expect(page.locator("#artistSearch")).toBeVisible();
    await expect(page.locator("#artistQuery")).toHaveValue("Dream Theater");
    await expect(page.locator("#artistQuery")).toBeFocused();
    await expect(other).toHaveCount(0);
    // The details stay until another name is searched.
    await expect(page.locator(".artist-name")).toHaveText("Dream Theater");
  });

  test("the rest of the card does not open it", async ({ page }) => {
    await stubWikimedia(page);
    await openStudio(page);
    await openTrack(page);
    await page.locator("#nowPlayingPanel .track-panel-info").click();
    await expect(page.locator("#artistDialog")).toBeHidden();
  });

  test("a band saved on the track opens straight on it, after a reload too", async ({ page }) => {
    const asked = await stubWikimedia(page);
    await openStudio(page);
    await openTrack(page);
    await page.locator("#np-details-btn").click();
    await lookUp(page, "Dream Theater");
    const save = page.locator(".artist-save");
    await expect(save).toHaveText("Save for this track");
    await save.click();
    await expect(save).toHaveText("Saved for this track");
    await expect(save).toBeDisabled();

    // Kept on the track in the library store: id, name, and the English name
    // the Lyrics tab searches LRCLIB with.
    await expect.poll(async () => (await readCatalogState(page))?.tracks?.[JOB_ID]?.artist)
      .toEqual({ id: "Q162586", name: "Dream Theater", englishName: "Dream Theater" });

    // seedLibrary writes the store on every load, reloads included, so what
    // the app saved is written back over it before reloading. Init scripts run
    // in the order they were added, so this one wins.
    await seedCatalogState(page, await readCatalogState(page));
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(".cat-item").first().waitFor({ timeout: 20000 });
    await openTrack(page);
    asked.length = 0;

    await page.locator("#np-details-btn").click();
    await expect(page.locator(".artist-name")).toHaveText("Dream Theater");
    await expect(page.locator(".artist-save")).toHaveText("Saved for this track");
    // A known band needs no field; it is one click away, filled in.
    await expect(page.locator("#artistSearch")).toBeHidden();
    await expect(page.locator("#artistQuery")).toHaveValue("Dream Theater");
    await expect(page.locator(".artist-other-btn")).toBeVisible();
    // By id, not by searching the name again.
    expect(asked.some((url) => url.includes("action=wbsearchentities"))).toBe(false);
    expect(asked.some((url) => url.includes("ids=Q162586"))).toBe(true);
  });
});
