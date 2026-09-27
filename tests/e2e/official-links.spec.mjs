// Official links in the artist box: a band's or a show's own site, Instagram,
// Spotify and Apple Music, read from the Wikidata item the box already asks
// for. Only the ones that exist are shown, and no row at all when none do.
//
// Every Wikimedia request is answered here, from answers trimmed from the real
// ones for Metallica (Q15920), Keala Settle (Q13560560), The Greatest Showman
// (Q27942936) and Norbert Leo Butz (Q5484158), so the tests run offline.
// What counts as a valid value is covered in tests/js/official-links.test.mjs.
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

const claim = (value, rank = "normal") => ({ rank, mainsnak: { snaktype: "value", datavalue: { value } } });
const item = (id) => claim({ id });

const ENTITIES = {
  Q15920: {
    id: "Q15920",
    labels: { en: { value: "Metallica" } },
    descriptions: { en: { value: "American heavy metal band" } },
    sitelinks: { enwiki: { site: "enwiki", title: "Metallica", url: "https://en.wikipedia.org/wiki/Metallica" } },
    claims: {
      P434: [{}],
      P856: [claim("https://www.metallica.com")],
      P2003: [claim("metallica")],
      P1902: [claim("2ye2Wgw4gimLv2eAKyk1NB")],
      P2850: [claim("3996865")],
    },
  },
  Q5484158: {
    id: "Q5484158",
    labels: { en: { value: "Norbert Leo Butz" } },
    descriptions: { en: { value: "American actor" } },
    sitelinks: { enwiki: { site: "enwiki", title: "Norbert Leo Butz", url: "https://en.wikipedia.org/wiki/Norbert_Leo_Butz" } },
    claims: { P434: [{}] },
  },
  Q13560560: {
    id: "Q13560560",
    labels: { en: { value: "Keala Settle" } },
    descriptions: { en: { value: "American actress and singer" } },
    sitelinks: { enwiki: { site: "enwiki", title: "Keala Settle", url: "https://en.wikipedia.org/wiki/Keala_Settle" } },
    claims: {
      P434: [{}],
      // A hostile site and a deprecated Apple Music id: neither is shown.
      P856: [claim("javascript:alert(document.domain)")],
      P2003: [claim("kealasettle")],
      P1902: [claim("7HV2RI2qNug4EcQqLbCAKS")],
      P2850: [claim("1234", "deprecated")],
    },
  },
  Q27942936: {
    id: "Q27942936",
    labels: { en: { value: "The Greatest Showman" } },
    descriptions: { en: { value: "2017 film directed by Michael Gracey" } },
    sitelinks: { enwiki: { site: "enwiki", title: "The Greatest Showman", url: "https://en.wikipedia.org/wiki/The_Greatest_Showman" } },
    claims: {
      P31: [item("Q11424")],
      P577: [{ mainsnak: { datavalue: { value: { time: "+2017-12-20T00:00:00Z" } } } }],
      P856: [claim("https://www.foxmovies.com/movies/the-greatest-showman")],
    },
  },
};

async function stubWikimedia(page) {
  await page.route(WIKIMEDIA, async (route) => {
    const url = new URL(route.request().url());
    const p = url.searchParams;
    let body;
    if (url.host === "query.wikidata.org") body = { results: { bindings: [] } };
    else if (url.host.endsWith("wikipedia.org")) body = { query: { pages: [{ extract: "Lead.\n== Career ==\nA career." }] } };
    else if (p.get("action") === "wbsearchentities") body = { search: [] };
    else if (p.get("props") === "labels") body = { entities: {} };
    else body = { entities: Object.fromEntries(p.get("ids").split("|").filter((id) => ENTITIES[id]).map((id) => [id, ENTITIES[id]])) };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "access-control-allow-origin": "*" },
      body: JSON.stringify(body),
    });
  });
}

// The track opens with its band saved on it, so the box asks for it by id.
async function openBox(page, extra) {
  await stubWikimedia(page);
  const tracks = {
    [JOB_ID]: { ...fixtureTrack(JOB_ID, "E2E Fixture Track"), audioTagsChecked: true, workChecked: true, ...extra },
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
  await stubAudioTags(page, null);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(".cat-item").first().waitFor({ timeout: 20000 });
  await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
  await expect(page.locator(".app")).not.toHaveClass(/no-track/, { timeout: 15000 });
  await page.locator("#np-details-btn").click();
}

const band = (id, name) => ({ id, name, englishName: name });

test.describe("official links in the artist box", () => {
  test("a band with all four shows all four, opening outside the app", async ({ page }) => {
    await openBox(page, { artist: band("Q15920", "Metallica") });
    await expect(page.locator(".artist-name")).toHaveText("Metallica", { timeout: 15000 });

    const row = page.locator("#artistBody .artist-links");
    await expect(row).toHaveCount(1);
    await expect(row).toHaveAttribute("aria-label", "Official links for Metallica");
    const links = row.locator("a.artist-link");
    await expect(links).toHaveText(["Website", "Instagram", "Spotify", "Apple Music"]);
    const hrefs = await links.evaluateAll((els) => els.map((a) => a.getAttribute("href")));
    expect(hrefs).toEqual([
      "https://www.metallica.com/",
      "https://www.instagram.com/metallica/",
      "https://open.spotify.com/artist/2ye2Wgw4gimLv2eAKyk1NB",
      "https://music.apple.com/artist/3996865",
    ]);
    // The same way out as "Read more on Wikipedia": main.js routes these
    // through the desktop app's open_url, a browser opens a new tab.
    for (let i = 0; i < 4; i++) {
      await expect(links.nth(i)).toHaveAttribute("target", "_blank");
      await expect(links.nth(i)).toHaveAttribute("rel", "noopener noreferrer");
      await expect(links.nth(i).locator("svg")).toHaveCount(1);
    }
    // Straight under the band's heading.
    const order = await page.locator("#artistBody > *").evaluateAll((els) => els.map((el) => el.className));
    expect(order.indexOf("artist-links")).toBe(order.findIndex((c) => c.includes("artist-head")) + 1);
  });

  test("a performer with some shows only those, and the film its site", async ({ page }) => {
    const work = { id: "Q27942936", kind: "film", name: "The Greatest Showman", englishName: "The Greatest Showman" };
    await openBox(page, { artist: band("Q13560560", "Keala Settle"), work });
    const performer = page.locator(".artist-performer");
    await expect(performer.locator(".artist-name")).toHaveText("Keala Settle", { timeout: 15000 });

    // The javascript: site and the deprecated Apple Music id are left out.
    const links = performer.locator(".artist-links a.artist-link");
    await expect(links).toHaveText(["Instagram", "Spotify"]);
    await expect(links.first()).toHaveAttribute("href", "https://www.instagram.com/kealasettle/");
    await expect(performer.locator(".artist-links")).toBeVisible();
    expect(await page.locator("#artistBody a[href^='javascript']").count()).toBe(0);

    const film = page.locator(".artist-work .artist-links a.artist-link");
    await expect(film).toHaveText(["Website"]);
    await expect(film).toHaveAttribute("href", "https://www.foxmovies.com/movies/the-greatest-showman");
    await expect(film).toHaveAttribute("target", "_blank");
  });

  test("an artist with none shows no row at all", async ({ page }) => {
    await openBox(page, { artist: band("Q5484158", "Norbert Leo Butz") });
    await expect(page.locator(".artist-name")).toHaveText("Norbert Leo Butz", { timeout: 15000 });
    await expect(page.locator(".artist-more")).toBeVisible();
    await expect(page.locator(".artist-links")).toHaveCount(0);
    await expect(page.locator(".artist-link")).toHaveCount(0);
  });
});
