// The work a soundtrack is from: a song from a musical or a film has the show
// behind it, found by the server (app/pipeline/work_lookup.py) and kept on the
// track. The now-playing card names it beside the song ("Popular · Wicked"),
// and the box shows it under the song and before any performer, whose history
// is folded behind "More about ...".
//
// Every Wikimedia request is answered here from answers trimmed from the real
// ones for Wicked (Q616439), so the tests run offline.
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

const WICKED = { id: "Q616439", kind: "musical", name: "Wicked", englishName: "Wicked" };
const BAND = { id: "Q229379", name: "Kristin Chenoweth", englishName: "Kristin Chenoweth" };
const TAGS = { title: "Popular", album: "Wicked (Original Broadway Cast Recording)" };

const item = (id) => ({ rank: "normal", mainsnak: { datavalue: { value: { id } } } });

const ENTITIES = {
  Q616439: {
    id: "Q616439",
    labels: { en: { value: "Wicked" } },
    descriptions: { en: { value: "2003 musical based on the 1995 novel" } },
    sitelinks: { enwiki: { site: "enwiki", title: "Wicked (musical)", url: "https://en.wikipedia.org/wiki/Wicked_(musical)" } },
    claims: {
      P31: [item("Q58483083")],
      P1191: [{ mainsnak: { datavalue: { value: { time: "+2003-06-10T00:00:00Z" } } } }],
      P86: [item("Q542484")],
      P676: [item("Q542484")],
      P87: [item("Q1790231")],
    },
  },
  Q229379: {
    id: "Q229379",
    labels: { en: { value: "Kristin Chenoweth" } },
    descriptions: { en: { value: "American actress and singer" } },
    sitelinks: { enwiki: { site: "enwiki", title: "Kristin Chenoweth", url: "https://en.wikipedia.org/wiki/Kristin_Chenoweth" } },
    claims: { P434: [{}] },
  },
};
const PEOPLE = {
  entities: {
    Q542484: { labels: { en: { value: "Stephen Schwartz" } } },
    Q1790231: { labels: { en: { value: "Winnie Holzman" } } },
  },
};
const ARTICLES = {
  "Wicked (musical)": "Wicked is a musical.\n== Synopsis ==\nIn the Land of Oz, the citizens celebrate the death of the Wicked Witch.",
  "Kristin Chenoweth": "Lead.\n== Career ==\nChenoweth originated the role of Glinda in Wicked.",
};

async function stubWikimedia(page) {
  const asked = [];
  await page.route(WIKIMEDIA, async (route) => {
    const url = new URL(route.request().url());
    asked.push(url.toString());
    const p = url.searchParams;
    let body;
    if (url.host === "query.wikidata.org") body = { results: { bindings: [] } };
    else if (url.host.endsWith("wikipedia.org")) body = { query: { pages: [{ extract: ARTICLES[p.get("titles")] || "" }] } };
    else if (p.get("action") === "wbsearchentities") body = { search: [] };
    else if (p.get("props") === "labels") body = PEOPLE;
    else body = { entities: Object.fromEntries(p.get("ids").split("|").filter((id) => ENTITIES[id]).map((id) => [id, ENTITIES[id]])) };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "access-control-allow-origin": "*" },
      body: JSON.stringify(body),
    });
  });
  return asked;
}

// Both fixture jobs are always in the library: they share a source, and a
// job the server lists that the library lacks replaces its twin (#542).
async function openPage(page, tracks, { audioTags = null } = {}) {
  tracks = { [JOB_ID]: fixtureTrack(JOB_ID, "E2E Fixture Track"), [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, "E2E Fixture Track (again)"), ...tracks };
  await seedCatalogState(page, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: Object.keys(tracks), color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks,
  });
  await stubExportEndpoints(page);
  await stubUpdateCheck(page);
  const asked = await stubAudioTags(page, audioTags);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(".cat-item").first().waitFor({ timeout: 20000 });
  return asked;
}

async function openTrack(page, id = JOB_ID) {
  await page.locator(`.cat-item[data-id="${id}"]`).first().click();
  await expect(page.locator(".app")).not.toHaveClass(/no-track/, { timeout: 15000 });
}

// A track as the library keeps one whose server found the work while it ran.
const castTrack = (id, extra = {}) => ({
  ...fixtureTrack(id, "Popular (Wicked OBC)"),
  audioTags: TAGS,
  audioTagsChecked: true,
  workChecked: true,
  work: WICKED,
  ...extra,
});

test.describe("the work a soundtrack is from", () => {
  test("with no band, the card names the musical and the box shows it under the song", async ({ page }) => {
    const asked = await stubWikimedia(page);
    await openPage(page, { [JOB_ID]: castTrack(JOB_ID) });
    await openTrack(page);

    await expect(page.locator("#title")).toHaveText("Popular");
    await expect(page.locator("#np-artist")).toHaveText("Wicked");

    await page.locator("#np-details-btn").click();
    // The song from its tags heads the box, then the musical.
    const song = page.locator(".artist-song");
    await expect(song.locator(".artist-song-title")).toHaveText("Popular");
    await expect(song.locator(".artist-song-from")).toHaveText("From the musical Wicked");
    await expect(song.locator(".artist-song-facts")).toContainText("Wicked (Original Broadway Cast Recording)");
    const work = page.locator(".artist-work");
    await expect(work).toBeVisible({ timeout: 15000 });
    await expect(work).not.toHaveClass(/lead/);
    await expect(work.locator(".artist-section-title")).toHaveText("From the musical");
    await expect(work.locator(".artist-work-name")).toHaveText("Wicked 2003");
    await expect(work.locator(".artist-desc")).toHaveText("2003 musical based on the 1995 novel");
    await expect(work.locator(".artist-work-synopsis")).toContainText("In the Land of Oz");
    await expect(work.locator(".artist-work-credits")).toContainText("Music");
    await expect(work.locator(".artist-work-credits")).toContainText("Stephen Schwartz");
    await expect(work.locator(".artist-work-credits")).toContainText("Book");
    await expect(work.locator(".artist-work-credits")).toContainText("Winnie Holzman");
    await expect(work.locator(".artist-more")).toHaveAttribute("href", "https://en.wikipedia.org/wiki/Wicked_(musical)");
    await expect(work.locator(".artist-more")).toHaveText("Read more about Wicked on Wikipedia");
    // No empty search in front of it, and no band section.
    await expect(page.locator("#artistSearch")).toBeHidden();
    await expect(page.locator(".artist-name")).toHaveCount(0);
    // Asked for by its id, never searched for by name.
    expect(asked.some((url) => url.includes("action=wbsearchentities"))).toBe(false);
    expect(asked.some((url) => url.includes("ids=Q616439"))).toBe(true);

    // The search for a band is still one click away.
    await page.locator(".artist-other-btn").click();
    await expect(page.locator("#artistSearch")).toBeVisible();
    await expect(page.locator("#artistQuery")).toBeFocused();
  });

  test("the musical comes before the performer, whose history is a click away", async ({ page }) => {
    await stubWikimedia(page);
    await openPage(page, { [JOB_ID]: castTrack(JOB_ID, { artist: BAND }) });
    await openTrack(page);
    // The show names a cast recording better than one of its cast.
    await expect(page.locator("#np-artist")).toHaveText("Wicked");

    await page.locator("#np-details-btn").click();
    const performer = page.locator(".artist-performer");
    await expect(performer.locator(".artist-name")).toHaveText("Kristin Chenoweth", { timeout: 15000 });
    await expect(performer.locator(".artist-section-title").first()).toHaveText("Performer");
    await expect(performer.locator(".artist-desc")).toHaveText("American actress and singer");
    const work = page.locator(".artist-work");
    await expect(work).toBeVisible();
    await expect(work.locator(".artist-work-name")).toHaveText("Wicked 2003");
    // The song, then the musical, then the performer, then the footer.
    const order = await page.locator("#artistBody > *").evaluateAll((els) => els.map((el) => el.className));
    const song = order.findIndex((c) => c.includes("artist-song"));
    const section = order.findIndex((c) => c.includes("artist-work"));
    const person = order.findIndex((c) => c.includes("artist-performer"));
    const foot = order.findIndex((c) => c.includes("artist-foot"));
    expect(song).toBe(0);
    expect(song).toBeLessThan(section);
    expect(section).toBeLessThan(person);
    expect(person).toBeLessThan(foot);

    // A cast member is a performer, not a band.
    await expect(performer.locator(".artist-other-btn").first()).toHaveText("Not this performer? Search");
    // What is sent where sits at the foot of the box, not at its top.
    await expect(page.locator(".artist-card-foot .artist-privacy")).toBeVisible();
    const privacyTop = (await page.locator(".artist-privacy").boundingBox()).y;
    expect(privacyTop).toBeGreaterThan((await page.locator(".artist-song-title").boundingBox()).y);

    // The biography waits behind "More about ...".
    await expect(performer.locator(".artist-history")).toBeHidden();
    await performer.getByRole("button", { name: "More about Kristin Chenoweth" }).click();
    await expect(performer.locator(".artist-history")).toContainText("originated the role of Glinda");

    // One link per article, each saying which.
    const links = page.locator("#artistBody .artist-more");
    await expect(links).toHaveText([
      "Read more about Wicked on Wikipedia",
      "Read more about Kristin Chenoweth on Wikipedia",
    ]);
    await expect(links.nth(1)).toHaveAttribute("href", "https://en.wikipedia.org/wiki/Kristin_Chenoweth");
  });

  test("a performer that cannot be reached leaves the musical in the box", async ({ page }) => {
    await stubWikimedia(page);
    // Registered last, so it runs first: the performer's own requests fail.
    await page.route(WIKIMEDIA, (route) => {
      const url = route.request().url();
      return /Q229379|Kristin/.test(decodeURIComponent(url)) ? route.abort("failed") : route.fallback();
    });
    await openPage(page, { [JOB_ID]: castTrack(JOB_ID, { artist: BAND }) });
    await openTrack(page);

    await page.locator("#np-details-btn").click();
    const work = page.locator(".artist-work");
    await expect(work).toBeVisible({ timeout: 15000 });
    await expect(work.locator(".artist-work-name")).toHaveText("Wicked 2003");
    await expect(page.locator(".artist-status.error")).toHaveCount(0);
  });

  test("an identified recording names its song on the card and heads the box", async ({ page }) => {
    await stubWikimedia(page);
    const raw = 'Dancing Through Life (From "Wicked" Original Broadway Cast Recording/2003 / Audio)';
    const identity = {
      source: "musicbrainz",
      title: "Dancing Through Life",
      artist: "Norbert Leo Butz, Kristin Chenoweth and Idina Menzel",
      album: "Wicked: Original Broadway Cast Recording",
      year: 2003,
      secondaryTypes: ["Soundtrack"],
    };
    await openPage(page, {
      [JOB_ID]: { ...fixtureTrack(JOB_ID, raw), audioTagsChecked: true, workChecked: true, work: WICKED, identity },
    });
    await openTrack(page);

    await expect(page.locator("#title")).toHaveText("Dancing Through Life");
    await expect(page.locator("#np-artist")).toHaveText("Wicked");
    // The library keeps the name the track was imported with.
    await expect(page.locator(`.cat-item[data-id="${JOB_ID}"]`).first()).toContainText("Original Broadway Cast Recording");

    await page.locator("#np-details-btn").click();
    const song = page.locator(".artist-song");
    await expect(song.locator(".artist-song-title")).toHaveText("Dancing Through Life");
    await expect(song.locator(".artist-song-from")).toHaveText("From the musical Wicked");
    await expect(song.locator(".artist-song-facts dd")).toHaveText([
      "Norbert Leo Butz, Kristin Chenoweth and Idina Menzel",
      "Wicked: Original Broadway Cast Recording, 2003",
    ]);
    await expect(page.locator(".artist-work .artist-work-name")).toHaveText("Wicked 2003", { timeout: 15000 });
    await expect(page.locator("#artistSearch")).toBeHidden();
  });

  test("an identity the server only read from the tags does not rename the card", async ({ page }) => {
    const identity = { source: "tags", title: "Guessed Title", artist: "Someone", album: "", secondaryTypes: [] };
    await openPage(page, {
      [JOB_ID]: { ...fixtureTrack(JOB_ID, "E2E Fixture Track"), audioTagsChecked: true, workChecked: true, identity },
    });
    await openTrack(page);
    await expect(page.locator("#title")).toHaveText("E2E Fixture Track");
  });

  test("an older track learns its work from the server and the card names it", async ({ page }) => {
    await stubWikimedia(page);
    // A track from before the server looked for works: tags known, no work.
    const older = { ...fixtureTrack(JOB_ID, "Popular (Wicked OBC)"), audioTags: TAGS, audioTagsChecked: true };
    await seedCatalogState(page, {
      folders: [
        { id: "f-unsorted", name: "Unsorted", items: [JOB_ID, SIBLING_JOB_ID], color: null },
        { id: "trash", name: "Trash", items: [], color: null },
      ],
      tracks: { [JOB_ID]: older, [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, "E2E Fixture Track (again)") },
    });
    await stubExportEndpoints(page);
    await stubUpdateCheck(page);
    const asked = [];
    await page.route("**/api/jobs/*/audio-tags", (route) => {
      asked.push(new URL(route.request().url()).pathname.split("/")[3]);
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ audio_tags: TAGS, artist: null, has_lyrics: false, identity: null, work: WICKED }),
      });
    });
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.locator(".cat-item").first().waitFor({ timeout: 20000 });
    await openTrack(page);

    await expect(page.locator("#np-artist")).toHaveText("Wicked", { timeout: 15000 });
    await expect.poll(async () => (await readCatalogState(page))?.tracks?.[JOB_ID]?.work, { timeout: 15000 }).toEqual(WICKED);
    expect((await readCatalogState(page)).tracks[JOB_ID].workChecked).toBe(true);

    // Asked once: opening it again asks nothing.
    await openTrack(page, SIBLING_JOB_ID);
    await openTrack(page);
    await page.waitForTimeout(500);
    expect(asked.filter((id) => id === JOB_ID)).toHaveLength(1);
  });

  test("a finished import carries its work to the card", async ({ page }) => {
    await stubWikimedia(page);
    const real = await (await page.request.get(`/api/jobs/${JOB_ID}`)).json();
    const identity = {
      source: "musicbrainz",
      score: 1,
      title: "Popular",
      artist: "Kristin Chenoweth",
      album: "Wicked",
      secondary_types: ["Soundtrack"],
      year: 2003,
    };
    const done = { ...real, audio_tags: TAGS, artist: null, work: WICKED, identity };
    await page.route(`**/api/jobs/${JOB_ID}`, (route) =>
      route.request().method() === "GET"
        ? route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(done) })
        : route.fallback());
    await page.route("**/api/jobs", (route) =>
      route.request().method() === "POST"
        ? route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ job_id: JOB_ID }) })
        : route.fallback());
    await page.route(`**/api/jobs/${JOB_ID}/events`, (route) =>
      route.fulfill({ status: 200, contentType: "text/event-stream", body: `data: ${JSON.stringify(done)}\n\n` }));
    const tagAsks = await openPage(page, { [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, "E2E Fixture Track (again)") });

    await page.locator("#url").fill("https://www.youtube.com/watch?v=6Nn8rBmhDjE");
    await page.locator("#submit").click();

    await expect(page.locator("#title")).toHaveText("Popular", { timeout: 15000 });
    await expect(page.locator("#np-artist")).toHaveText("Wicked", { timeout: 15000 });
    await expect.poll(async () => (await readCatalogState(page))?.tracks?.[JOB_ID]?.work, { timeout: 15000 }).toEqual(WICKED);
    // The server looked while the job ran, so the track never asks again.
    expect((await readCatalogState(page)).tracks[JOB_ID].workChecked).toBe(true);
    expect(tagAsks).not.toContain(JOB_ID);
    expect((await readCatalogState(page)).tracks[JOB_ID].identity.year).toBe(2003);

    // An album named like the show still shows, for the year it came out.
    await page.locator("#np-details-btn").click();
    await expect(page.locator(".artist-song-facts dd")).toHaveText(["Kristin Chenoweth", "Wicked, 2003"]);
  });
});

test("the About box credits where song details come from", async ({ page }) => {
  await openPage(page, { [JOB_ID]: fixtureTrack(JOB_ID, "E2E Fixture Track") });
  await page.evaluate(() => document.getElementById("aboutBtn")?.click());
  const credits = page.locator("#aboutDialog .about-credits");
  await expect(credits).toBeVisible();
  for (const name of ["AcoustID", "MusicBrainz (CC0)", "Wikipedia (CC BY-SA)", "Wikidata (CC0)", "LRCLIB", "OpenAI Whisper (MIT)"]) {
    await expect(credits).toContainText(name);
  }
});
