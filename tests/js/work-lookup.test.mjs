// The work a soundtrack is from, as the artist box looks it up by the id the
// server found (artistLookup.js lookupWork): its year, the people who wrote
// it, and a short synopsis read out of the Wikipedia article, per language.
//
// No network. lookupWork takes its fetch as an argument, and the fake below
// answers from fixtures trimmed from the live ones for Wicked (Q616439).
//
// Run:  node tests/js/work-lookup.test.mjs

import {
  lookupWork,
  synopsisFromExtract,
  synopsisHeadingsFor,
  workFacts,
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

const item = (id) => ({ rank: "normal", mainsnak: { datavalue: { value: { id } } } });
const time = (t) => ({ mainsnak: { datavalue: { value: { time: t } } } });

// ── workFacts ──
const claims = {
  P1191: [time("+2003-06-10T00:00:00Z"), time("+2003-05-28T00:00:00Z")],
  P86: [item("Q542484")],
  P676: [item("Q542484")],
  P87: [item("Q1790231"), { ...item("Q9"), rank: "deprecated" }],
};
const facts = workFacts(claims);
check("the earliest first performance is the year", facts.year === "2003");
check("composers", same(facts.composers, ["Q542484"]));
check("lyricists", same(facts.lyricists, ["Q542484"]));
check("book writers, deprecated left out", same(facts.bookWriters, ["Q1790231"]));
check("a film's publication date is its year", workFacts({ P577: [time("+1939-08-25T00:00:00Z")] }).year === "1939");
check("no dates, no year", workFacts({}).year === "");

// ── synopsisFromExtract ──
const article = [
  "Wicked is a musical with music and lyrics by Stephen Schwartz.",
  "== Background ==",
  "Schwartz discovered the novel in 1996.",
  "== Synopsis ==",
  "=== Act I ===",
  "In the Land of Oz, the citizens celebrate the death of the Wicked Witch.",
  "== Productions ==",
  "It opened on Broadway in 2003.",
].join("\n");
check(
  "the Synopsis section, less its sub-headings",
  same(synopsisFromExtract(article, "en"), ["In the Land of Oz, the citizens celebrate the death of the Wicked Witch."]),
);
check(
  "a Plot section too",
  same(synopsisFromExtract("Lead.\n== Plot ==\nDorothy is swept to Oz.", "en"), ["Dorothy is swept to Oz."]),
);
check(
  "no story section: the lead",
  same(synopsisFromExtract("Lead text.\n== Cast ==\nSomeone.", "en"), ["Lead text."]),
);
check(
  "a German article's Handlung",
  same(synopsisFromExtract("Einleitung.\n== Handlung ==\nDorothy reist nach Oz.", "de"), ["Dorothy reist nach Oz."]),
);
check("an edition's own headings, then English", synopsisHeadingsFor("ja")[0] === "あらすじ" && synopsisHeadingsFor("ja").includes("plot"));
const long = Array.from({ length: 40 }, (_, i) => `Sentence number ${i} of the plot goes here.`).join(" ");
const cut = synopsisFromExtract(`Lead.\n== Plot ==\n${long}`, "en");
check("a long plot is kept short", cut.join(" ").length <= 710, `length ${cut.join(" ").length}`);

// ── lookupWork ──
const ANSWERS = {
  entities: {
    entities: {
      Q616439: {
        id: "Q616439",
        labels: { en: { value: "Wicked" } },
        descriptions: { en: { value: "2003 musical based on the 1995 Gregory Maguire's novel" } },
        sitelinks: { enwiki: { site: "enwiki", title: "Wicked (musical)", url: "https://en.wikipedia.org/wiki/Wicked_(musical)" } },
        claims: { ...claims, P18: [{ mainsnak: { datavalue: { value: "Wicked musical logo.jpg" } } }] },
      },
    },
  },
  people: {
    entities: {
      Q542484: { labels: { en: { value: "Stephen Schwartz" } } },
      Q1790231: { labels: { en: { value: "Winnie Holzman" } } },
    },
  },
  extract: { query: { pages: [{ extract: article }] } },
};

function fakeFetch({ offline = false, missing = false } = {}) {
  const calls = [];
  const fetchJson = async (url) => {
    calls.push(url);
    if (offline) throw new Error("offline");
    const u = new URL(url);
    if (u.host.endsWith("wikipedia.org")) return ANSWERS.extract;
    if (u.searchParams.get("props") === "labels") return ANSWERS.people;
    if (missing) return { entities: { Q616439: { id: "Q616439", missing: "" } } };
    return ANSWERS.entities;
  };
  return { calls, fetchJson };
}

const { calls, fetchJson } = fakeFetch();
const work = await lookupWork("Q616439", "en", { fetchJson });
check("the name", work?.name === "Wicked");
check("the year", work?.year === "2003");
check("the description", work?.description.startsWith("2003 musical"));
check("the picture, from Commons", work?.image.startsWith("https://commons.wikimedia.org/wiki/Special:FilePath/Wicked%20musical%20logo.jpg"));
check("the synopsis", work?.synopsis[0]?.startsWith("In the Land of Oz"));
check("music by", same(work?.composers, ["Stephen Schwartz"]));
check("lyrics by", same(work?.lyricists, ["Stephen Schwartz"]));
check("book by", same(work?.bookWriters, ["Winnie Holzman"]));
check("read more", work?.articleUrl === "https://en.wikipedia.org/wiki/Wicked_(musical)");
check("asked by id, never by name", calls.every((url) => new URL(url).searchParams.get("action") !== "wbsearchentities"));
check("three requests: the item, its people, its article", calls.length === 3, String(calls.length));

check("an id that is not a Wikidata item asks nothing", (await lookupWork("Q1 OR 1=1", "en", { fetchJson: () => { throw new Error("asked"); } })) === null);
check("a missing item is null", (await lookupWork("Q616439", "en", fakeFetch({ missing: true }))) === null);
let rejected = false;
try {
  await lookupWork("Q616439", "en", fakeFetch({ offline: true }));
} catch {
  rejected = true;
}
check("no connection rejects, so the box can say so", rejected);

// ── A work in another language ──
// A Japanese film: its own title beside the English one, and its story read
// from the Japanese Wikipedia when the English article has none. Trimmed
// from Spirited Away (Q155653), with the English article made a stub.
const SPIRITED = {
  id: "Q155653",
  labels: { en: { language: "en", value: "Spirited Away" } },
  descriptions: { en: { value: "2001 film by Hayao Miyazaki" } },
  sitelinks: {
    enwiki: { site: "enwiki", title: "Spirited Away", url: "https://en.wikipedia.org/wiki/Spirited_Away" },
    jawiki: { site: "jawiki", title: "千と千尋の神隠し", url: "https://ja.wikipedia.org/wiki/%E5%8D%83" },
  },
  claims: {
    P1476: [{ rank: "normal", mainsnak: { datavalue: { value: { language: "ja", text: "千と千尋の神隠し" } } } }],
    P577: [time("+2001-07-20T00:00:00Z")],
  },
};
const jaCalls = [];
const spirited = await lookupWork("Q155653", "en", {
  fetchJson: async (url) => {
    jaCalls.push(url);
    const u = new URL(url);
    if (u.host === "ja.wikipedia.org") return { query: { pages: [{ extract: `概要。\n== あらすじ ==\n${"千尋は両親と共に不思議な町に迷い込む。".repeat(20)}` }] } };
    if (u.host.endsWith("wikipedia.org")) return { query: { pages: [{ extract: "A film." }] } };
    if (u.searchParams.get("props") === "labels") return { entities: {} };
    return { entities: { Q155653: SPIRITED } };
  },
});
check("the title in the reader's language", spirited?.name === "Spirited Away");
check("and in its own, from its title claim", spirited?.nativeName === "千と千尋の神隠し" && spirited?.nativeLang === "ja");
check("the story from the Japanese article", spirited?.synopsisLang === "ja" && spirited?.synopsis[0]?.startsWith("千尋は"));
check("the link is to that article", spirited?.articleUrl === "https://ja.wikipedia.org/wiki/%E5%8D%83");
check("the English article asked first", new URL(jaCalls.find((url) => url.includes("wikipedia.org"))).host === "en.wikipedia.org");

console.log(`${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
