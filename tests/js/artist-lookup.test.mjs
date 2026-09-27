// The artist box's lookup (#699): which search result it takes as the
// artist, a saved band found by its id, how it reads members, albums and the
// History section out of what Wikidata and Wikipedia return, and how it tells
// "no such artist" from "no connection".
//
// No network. lookupArtist takes its fetch as an argument, and the fake below
// answers from fixtures shaped like the real responses (trimmed from live ones
// for Dream Theater, Q162586).
//
// Run:  node tests/js/artist-lookup.test.mjs

import {
  pickArtist,
  membersFromClaims,
  albumsFromSparql,
  historyFromExtract,
  historyHeadingsFor,
  wikiLanguage,
  lookupArtist,
  artistMatchesName,
  taggedArtistName,
} from "../../static/js/artistLookup.js";

let pass = 0,
  fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) {
    pass++;
  } else {
    fail++;
    console.error(`FAIL  ${name}${detail ? `\n      ${detail}` : ""}`);
  }
};
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// ── wikiLanguage ──
check("zh-Hans reads the Chinese edition", wikiLanguage("zh-Hans") === "zh");
check("pt-PT reads the Portuguese edition", wikiLanguage("pt-PT") === "pt");
check("an unknown code reads English", wikiLanguage("xx") === "en");

// ── pickArtist ──
const band = { id: "Q162586", claims: { P434: [{}], P31: [{}] } };
const album = { id: "Q13420662", claims: { P31: [{}] } };
check(
  "skips the album of the same name for the band",
  pickArtist({ Q13420662: album, Q162586: band }, ["Q13420662", "Q162586"])?.id === "Q162586",
);
check("nothing that is an artist gives null", pickArtist({ Q13420662: album }, ["Q13420662"]) === null);

// ── membersFromClaims ──
const member = (id, ended = false, rank = "normal") => ({
  rank,
  mainsnak: { datavalue: { value: { id } } },
  qualifiers: ended ? { P582: [{}] } : {},
});
const members = membersFromClaims({
  P527: [
    member("Q175102"),
    member("Q221468", true), // Portnoy, left in 2010
    member("Q221468"), //       and back in 2023
    member("Q296039", true),
    member("Q999999", false, "deprecated"),
  ],
});
check("a member who came back is current", members.current.includes("Q221468"));
check("and is not also former", !members.former.includes("Q221468"));
check("an ended membership is former", same(members.former, ["Q296039"]));
check("a deprecated statement is ignored", !members.current.includes("Q999999"));
check("a solo artist has no members", same(membersFromClaims({}), { current: [], former: [] }));

// ── albumsFromSparql ──
const row = (id, title, date) => ({
  album: { value: `http://www.wikidata.org/entity/${id}` },
  albumLabel: { value: title },
  ...(date ? { released: { value: `${date}-01-01T00:00:00Z` } } : {}),
});
const albums = albumsFromSparql({
  results: {
    bindings: [
      row("Q2", "Images and Words", "1992"),
      row("Q1", "When Dream and Day Unite", "1989"),
      row("Q2", "Images and Words", "1992"),
      row("Q3", "Q3"),
      row("Q4", "Undated"),
    ],
  },
});
check(
  "oldest first, one row each, undated last, bare ids dropped",
  same(albums, [
    { title: "When Dream and Day Unite", year: "1989" },
    { title: "Images and Words", year: "1992" },
    { title: "Undated", year: "" },
  ]),
  JSON.stringify(albums),
);
check("no answer, no albums", same(albumsFromSparql(null), []));

// ── historyFromExtract ──
const article = [
  "Dream Theater is an American progressive metal band.",
  "",
  "== History ==",
  "=== Formation (1985-1986) ===",
  "In 1985, three students formed a band.",
  "They called it Majesty.",
  "== Band members ==",
  "John Petrucci",
].join("\n");
check(
  "the History section, without its sub-headings or what follows",
  same(historyFromExtract(article, historyHeadingsFor("en")), [
    "In 1985, three students formed a band.",
    "They called it Majesty.",
  ]),
);
const german = "Einleitung.\n== Geschichte ==\nIm September 1985 gegründet.\n== Diskografie ==\nx";
check("a heading in the edition's language", same(historyFromExtract(german, historyHeadingsFor("de")), ["Im September 1985 gegründet."]));
check(
  "no History heading falls back to the summary",
  same(historyFromExtract("A summary.\n== Discography ==\nx", historyHeadingsFor("en")), ["A summary."]),
);
const long = `== History ==\n${"A sentence that goes on. ".repeat(120)}`;
const capped = historyFromExtract(long, historyHeadingsFor("en"));
check("a long history is capped at a sentence", capped.join("").length <= 1410 && capped.at(-1).endsWith("…"));

// ── lookupArtist ──
const fixtures = {
  wbsearchentities: { search: [{ id: "Q13420662" }, { id: "Q162586" }] },
  bandEntities: {
    entities: {
      Q13420662: album,
      Q162586: {
        id: "Q162586",
        labels: { en: { value: "Dream Theater" } },
        descriptions: { en: { value: "American progressive metal band" } },
        sitelinks: { enwiki: { site: "enwiki", title: "Dream Theater", url: "https://en.wikipedia.org/wiki/Dream_Theater" } },
        claims: {
          P434: [{}],
          P18: [{ mainsnak: { datavalue: { value: "Dream theater live.jpg" } } }],
          P527: [member("Q175102"), member("Q296039", true)],
        },
      },
    },
  },
  memberEntities: {
    entities: {
      Q175102: { labels: { en: { value: "John Petrucci" } } },
      Q296039: { labels: { en: { value: "Kevin Moore" } } },
    },
  },
  sparql: { results: { bindings: [row("Q9", "Awake", "1994")] } },
  extract: { query: { pages: [{ extract: "Lead.\n== History ==\nFormed in 1985." }] } },
};

function fakeFetch({ fail: failing = [], nothing = false } = {}) {
  const calls = [];
  const fetchJson = async (url) => {
    calls.push(url);
    const u = new URL(url);
    const kind =
      u.host === "query.wikidata.org" ? "sparql"
      : u.host.endsWith("wikipedia.org") ? "extract"
      : u.searchParams.get("action") === "wbsearchentities" ? "search"
      : u.searchParams.get("props") === "labels" ? "members"
      : "entities";
    if (failing.includes(kind)) throw new Error(`offline (${kind})`);
    if (kind === "search") return nothing ? { search: [] } : fixtures.wbsearchentities;
    if (kind === "entities") return fixtures.bandEntities;
    if (kind === "members") return fixtures.memberEntities;
    if (kind === "sparql") return fixtures.sparql;
    return fixtures.extract;
  };
  return { fetchJson, calls };
}

const { fetchJson, calls } = fakeFetch();
const artist = await lookupArtist("Dream Theater", "en", { fetchJson });
check("finds the band, not the album", artist?.id === "Q162586");
check("name and description", artist?.name === "Dream Theater" && artist?.description === "American progressive metal band");
check("history from the article", same(artist?.history, ["Formed in 1985."]));
check("members by name", same(artist?.members, { current: ["John Petrucci"], former: ["Kevin Moore"] }));
check("albums", same(artist?.albums, [{ title: "Awake", year: "1994" }]));
check("article link", artist?.articleUrl === "https://en.wikipedia.org/wiki/Dream_Theater");
check("photo from Commons, name encoded", artist?.image.includes("Special:FilePath/Dream%20theater%20live.jpg"));
check(
  "only Wikimedia hosts are asked",
  calls.every((url) => /^https:\/\/(www\.wikidata\.org|query\.wikidata\.org|[a-z]+\.wikipedia\.org)\//.test(url)),
);
check("the search sends the name and nothing else about the track", calls[0].includes("search=Dream+Theater"));

check("a name that is no artist gives null", (await lookupArtist("zzz", "en", { fetchJson: fakeFetch({ nothing: true }).fetchJson })) === null);
check("an empty name asks nothing", (await lookupArtist("  ", "en", { fetchJson: () => { throw new Error("asked"); } })) === null);

let rejected = false;
try {
  await lookupArtist("Dream Theater", "en", { fetchJson: fakeFetch({ fail: ["search"] }).fetchJson });
} catch {
  rejected = true;
}
check("no connection rejects, rather than reading as not found", rejected);

const partial = await lookupArtist("Dream Theater", "en", { fetchJson: fakeFetch({ fail: ["sparql", "extract"] }).fetchJson });
check(
  "one section failing leaves the rest",
  partial && same(partial.albums, []) && same(partial.history, []) && partial.members.current.length === 1,
);

// A band saved on a track is looked up by its id, with no search at all.
const byId = fakeFetch();
const saved = await lookupArtist("", "en", { id: "Q162586", fetchJson: byId.fetchJson });
check("a saved id finds the band", saved?.id === "Q162586");
check(
  "and asks no name search",
  !byId.calls.some((url) => new URL(url).searchParams.get("action") === "wbsearchentities"),
);
check(
  "an id that is not a Wikidata item is not used",
  (await lookupArtist("", "en", { id: "Q1 OR 1=1", fetchJson: () => { throw new Error("asked"); } })) === null,
);

// ── The band found from a file's tags, saved with nobody looking ──
const dt = { name: "Dream Theater", englishName: "Dream Theater" };
check("the same name matches", artistMatchesName(dt, "Dream Theater"));
check("case and spacing do not matter", artistMatchesName(dt, "  dream  THEATER "));
check("punctuation does not matter", artistMatchesName({ name: "AC/DC", englishName: "AC/DC" }, "ACDC"));
check("Latin accents do not matter", artistMatchesName({ name: "Beyoncé", englishName: "Beyoncé" }, "Beyonce"));
check("the English name matches too", artistMatchesName({ name: "ドリーム・シアター", englishName: "Dream Theater" }, "Dream Theater"));
check("a near miss does not", !artistMatchesName(dt, "Dream Theatre"));
check("part of the name does not", !artistMatchesName(dt, "Theater"));
check("a name that is only punctuation matches nothing", !artistMatchesName({ name: "!!!", englishName: "" }, "!!!"));
check("no answer matches nothing", !artistMatchesName(null, "Dream Theater"));
check("a featured artist is left off", taggedArtistName("Dream Theater feat. Someone") === "Dream Theater");
check("in brackets too", taggedArtistName("Dream Theater (ft. Someone)") === "Dream Theater");
check("a plain tag is kept", taggedArtistName("  Dream Theater ") === "Dream Theater");
check("no tag, no name", taggedArtistName(undefined) === "");

console.log(`${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
