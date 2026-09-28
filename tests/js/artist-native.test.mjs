// The artist box's other languages (static/js/artistLookup.js): which
// Wikipedia editions a history is read from and in what order, what happens
// when one of them fails or all are stubs, and the rules for the artist's own
// name shown beside the reader's. artist-lookup.test.mjs covers the common
// paths; these are the branches it leaves unpinned.
//
// No network: lookupArtist takes its fetch as an argument.
//
// Run:  node tests/js/artist-native.test.mjs

import {
  articleEditions,
  chineseVariant,
  labelChain,
  lookupArtist,
  nativeLabel,
  nativeLanguage,
  nativeNameToShow,
} from "../../static/js/artistLookup.js";

let pass = 0;
let fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) {
    pass++;
  } else {
    fail++;
    console.error(`FAIL  ${name}${detail ? `\n      ${detail}` : ""}`);
  }
};
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
// Quiet the lookup's own console.warn for the failures provoked on purpose.
console.warn = () => {};

const site = (edition, title) => ({ site: `${edition}wiki`, title, url: `https://${edition}.wikipedia.org/wiki/${encodeURIComponent(title)}` });
const itemClaim = (id, rank = "normal") => ({ rank, mainsnak: { datavalue: { value: { id } } } });
const text = (language, value, rank = "normal") => ({ rank, mainsnak: { datavalue: { value: { language, text: value } } } });

// ── articleEditions ──

const all = { plwiki: site("pl", "X"), enwiki: site("en", "X"), jawiki: site("ja", "X"), zhwiki: site("zh", "X"), nowiki: site("no", "X") };
check("the app's, then English, then the artist's own", same(articleEditions("pl", "ja", all), ["pl", "en", "ja"]));
check("an English reader is not asked English twice", same(articleEditions("en", "ja", all), ["en", "ja"]));
check("an edition without a sitelink is left out", same(articleEditions("pl", "ja", { enwiki: site("en", "X"), jawiki: site("ja", "X") }), ["en", "ja"]));
check("an empty sitelink title is no article", same(articleEditions("pl", "", { plwiki: { title: "" }, enwiki: site("en", "X") }), ["en"]));
check("zh-tw reads the zh edition", same(articleEditions("en", "zh-tw", all), ["en", "zh"]));
check("Norwegian Bokmal reads the no edition", same(articleEditions("en", "nb", all), ["en", "no"]));
check("no sitelinks, no editions", same(articleEditions("en", "ja", undefined), []));

// ── chineseVariant ──

check("Macau reads Hong Kong's traditional script", chineseVariant("en", "zh-mo") === "zh-hk");
check("the mainland reads simplified", chineseVariant("ja", "zh-cn") === "zh-hans");
check("a simplified-Chinese reader of a Hong Kong singer reads simplified", chineseVariant("zh-Hans", "zh-hk") === "zh-hans");

// ── labelChain ──

check("a regional native name asks its base language too", same(labelChain("pl", "zh-tw"), ["pl", "en", "mul", "zh-tw", "zh"]));
check("each language once", same(labelChain("en", "en"), ["en", "mul"]));

// ── nativeLanguage ──

check("a deprecated citizenship is ignored", nativeLanguage({ P27: [itemClaim("Q17", "deprecated"), itemClaim("Q884")] }) === "ko");
check("a language-neutral (mul) name says nothing", nativeLanguage({ P1559: [text("mul", "IU")], P27: [itemClaim("Q884")] }) === "ko");
check(
  "a plain zh name is narrowed by the country",
  nativeLanguage({ P1559: [text("zh", "周杰倫")], P27: [itemClaim("Q865")] }) === "zh-tw",
);
check("with no region anywhere, plain zh stays", nativeLanguage({ P1559: [text("zh", "王菲")] }) === "zh");
check("an unknown country falls through to the name's script", nativeLanguage({ P27: [itemClaim("Q999999999")] }, "아이유") === "ko");

// ── nativeLabel ──

const filledIn = { labels: { ja: { language: "en", value: "Hikaru Utada" } } };
check("a label Wikidata filled in from English is not the native name", nativeLabel(filledIn, "ja", {}) === "");
check(
  "the native name claim stands in for it",
  nativeLabel(filledIn, "ja", { P1559: [text("ja", "宇多田ヒカル")] }) === "宇多田ヒカル",
);
check("the base language's label serves a regional code", nativeLabel({ labels: { zh: { language: "zh", value: "周杰倫" } } }, "zh-tw", {}) === "周杰倫");
check("no native language, no native name", nativeLabel({ labels: { en: { value: "Queen" } } }, "", {}) === "");

// ── nativeNameToShow ──

check("case and punctuation aside, the same name is not shown twice", nativeNameToShow("AC/DC", "ac dc") === "");
check("Han names of different lengths are different names", nativeNameToShow("王菲", "王靖雯") === "王靖雯");
check("a Han name beside a Latin one is shown", nativeNameToShow("Faye Wong", "王菲") === "王菲");

// ── which edition the history comes from ──

const LONG = (edition) => `${edition}: ${"A sentence about the band's long career. ".repeat(20)}`;
const SHORT = (edition) => `${edition}: a stub.`;

function editionsFetch(extracts) {
  const asked = [];
  const fetchJson = async (url) => {
    const u = new URL(url);
    if (u.host === "query.wikidata.org") return { results: { bindings: [] } };
    if (u.host.endsWith("wikipedia.org")) {
      const edition = u.host.split(".")[0];
      asked.push(edition);
      const extract = extracts[edition];
      if (extract instanceof Error) throw extract;
      return { query: { pages: [{ extract }] } };
    }
    if (u.searchParams.get("props").startsWith("labels")) return { entities: {} };
    return {
      entities: {
        Q1: {
          id: "Q1",
          labels: { en: { value: "Band" } },
          sitelinks: { plwiki: site("pl", "Band"), enwiki: site("en", "Band"), jawiki: site("ja", "バンド") },
          // Japanese: the artist's own edition is ja.
          claims: { P434: [{}], P27: [itemClaim("Q17")] },
        },
      },
    };
  };
  return { fetchJson, asked };
}

{
  const f = editionsFetch({ pl: LONG("pl"), en: LONG("en"), ja: LONG("ja") });
  const a = await lookupArtist("", "pl", { id: "Q1", fetchJson: f.fetchJson });
  check("the reader's own edition, when long, is the only one asked", a.historyLang === "pl" && same(f.asked, ["pl"]), JSON.stringify(f.asked));
}
{
  const f = editionsFetch({ pl: new Error("offline"), en: LONG("en"), ja: LONG("ja") });
  const a = await lookupArtist("", "pl", { id: "Q1", fetchJson: f.fetchJson });
  check("a failed edition counts as empty: the next long one is read", a?.historyLang === "en", a?.historyLang);
  check("and the link is that edition's", a?.articleUrl === "https://en.wikipedia.org/wiki/Band");
}
{
  const f = editionsFetch({ pl: SHORT("pl"), en: SHORT("en"), ja: `ja: ${"長い経歴。".repeat(10)}` });
  const a = await lookupArtist("", "pl", { id: "Q1", fetchJson: f.fetchJson });
  check("all stubs: the longest by weight, the artist's own here", a?.historyLang === "ja", a?.historyLang);
  check("the rest are asked side by side after the first", same(f.asked.slice(0, 1), ["pl"]) && f.asked.length === 3);
}
{
  const f = editionsFetch({ pl: "pl: same length", en: "en: same length", ja: "ja: same length" });
  const a = await lookupArtist("", "pl", { id: "Q1", fetchJson: f.fetchJson });
  check("stubs of equal weight: the first edition wins the tie", a?.historyLang === "pl", a?.historyLang);
}
{
  const f = editionsFetch({ pl: new Error("offline"), en: new Error("offline"), ja: new Error("offline") });
  const a = await lookupArtist("", "pl", { id: "Q1", fetchJson: f.fetchJson });
  check("every edition failing leaves the artist with no history, not no artist", a?.id === "Q1" && same(a.history, []));
}

console.log(`${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
