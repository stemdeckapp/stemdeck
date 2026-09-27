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
  albumList,
  chineseVariant,
  labelChain,
  labelLanguage,
  languageTag,
  nativeLabel,
  nativeLanguage,
  nativeNameToShow,
  scriptLanguage,
  searchLanguages,
  textWeight,
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
      : u.searchParams.get("props").startsWith("labels") ? "members"
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
check(
  "members by name",
  same(artist?.members, { current: [{ name: "John Petrucci", native: "" }], former: [{ name: "Kevin Moore", native: "" }] }),
);
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

// ── Artists who are not English-speaking ──
//
// A Chinese, Japanese or Korean artist was read in the app's language or in
// English only, so an English app showed two lines of Jay Chou's history
// where the Chinese Wikipedia has pages, and a simplified-Chinese app showed
// his name in traditional script. The fixtures are trimmed from the live
// answers for Jay Chou (Q238819), whose English article is long in fact; here
// it is made a stub to test the fallback.

// The script a name is written in says which labels to search it among.
check("Han is Chinese", scriptLanguage("周杰倫") === "zh");
check("kana is Japanese, even with kanji", scriptLanguage("宇多田ヒカル") === "ja");
check("Hangul is Korean", scriptLanguage("아이유") === "ko");
check("Cyrillic is Russian", scriptLanguage("Кино") === "ru");
check("Latin says nothing", scriptLanguage("Rosalía") === "");
check("a Chinese name is searched in Chinese, then English", same(searchLanguages("周杰倫", "en"), ["zh", "en"]));
check("a Korean name under Polish", same(searchLanguages("아이유", "pl"), ["ko", "en"]));
check(
  "a Latin name under a Chinese app is searched in English, not among Chinese labels",
  same(searchLanguages("Queen", "zh-Hans"), ["en"]),
);
check("a Latin name under Polish is searched in Polish first", same(searchLanguages("Dawid Podsiadło", "pl"), ["pl", "en"]));

// Chinese variants: the simplified label, not the plain "zh" one.
check("zh-Hans asks for simplified labels", labelLanguage("zh-Hans") === "zh-hans");
check(
  "and falls back through zh-cn and zh before English",
  same(labelChain("zh-Hans").slice(0, 4), ["zh-hans", "zh-cn", "zh", "en"]),
);
check("the app's Portuguese is Brazilian", same(labelChain("pt").slice(0, 2), ["pt-br", "pt"]));
check("European Portuguese is plain pt", labelLanguage("pt-PT") === "pt");
check("the native language comes last", same(labelChain("en", "zh-tw"), ["en", "mul", "zh-tw", "zh"]));

// The artist's own language.
const itemClaim = (id) => ({ rank: "normal", mainsnak: { datavalue: { value: { id } } } });
const text = (language, value) => ({ rank: "normal", mainsnak: { datavalue: { value: { language, text: value } } } });
check("Taiwanese citizenship is traditional Chinese", nativeLanguage({ P27: [itemClaim("Q865")] }) === "zh-tw");
check(
  "a Chinese native name is narrowed by the country: Faye Wong",
  nativeLanguage({ P1559: [text("zh", "王菲"), text("en", "Faye Wong")], P27: [itemClaim("Q148")] }) === "zh-cn",
);
check(
  "Teresa Teng's zh-hant name, from Taiwan",
  nativeLanguage({ P1559: [text("zh-hant", "鄧麗君")], P27: [itemClaim("Q865")] }) === "zh-tw",
);
check("IU's Korean name", nativeLanguage({ P1559: [text("ko", "이지은")] }) === "ko");
check("the first citizenship: Utada is Japanese", nativeLanguage({ P27: [itemClaim("Q17"), itemClaim("Q30")] }) === "ja");
check(
  "a country of several languages gives way to the one spoken: Stromae",
  nativeLanguage({ P27: [itemClaim("Q31")], P1412: [itemClaim("Q150"), itemClaim("Q1860")] }) === "fr",
);
check("a band's country of origin", nativeLanguage({ P495: [itemClaim("Q884")] }) === "ko");
check("nothing else, the name's script", nativeLanguage({}, "米津玄師") === "zh");
check("nothing at all", nativeLanguage({}, "Queen") === "");

const jayLabels = {
  en: { language: "en", value: "Jay Chou" },
  "zh-tw": { language: "zh-tw", value: "周杰倫" },
  ko: { language: "en", value: "Jay Chou" }, // Wikidata's fallback, not Korean
};
check("the native label", nativeLabel({ labels: jayLabels }, "zh-tw") === "周杰倫");
check("not a label filled in from another language", nativeLabel({ labels: jayLabels }, "ko") === "");
check(
  "else the native name the claims give",
  nativeLabel({ labels: {} }, "zh-tw", { P1559: [text("zh-hant", "鄧麗君")] }) === "鄧麗君",
);

// Shown beside the name only when it says something new.
check("周杰倫 beside Jay Chou", nativeNameToShow("Jay Chou", "周杰倫") === "周杰倫");
check("not the same name again", nativeNameToShow("Dream Theater", "Dream Theater") === "");
check("not the same Han characters in the other script", nativeNameToShow("周杰伦", "周杰倫") === "");
check("but a Japanese name beside its Chinese reading", nativeNameToShow("宇多田光", "宇多田ヒカル") === "宇多田ヒカル");
check("no native name, nothing", nativeNameToShow("Queen", "") === "");

check("zh-tw as an HTML language", languageTag("zh-tw") === "zh-TW");
check("zh-hans as an HTML language", languageTag("zh-hans") === "zh-Hans");
check("pt-br as an HTML language", languageTag("pt-br") === "pt-BR");
check("ja as it is", languageTag("ja") === "ja");

check("a simplified-Chinese app reads simplified", chineseVariant("zh-Hans", "zh-tw") === "zh-hans");
check("anyone else reads the artist's own script", chineseVariant("en", "zh-tw") === "zh-tw");
check("Hong Kong is traditional too", chineseVariant("ja", "zh-hk") === "zh-hk");
check("no region known, simplified", chineseVariant("en", "") === "zh-hans");

// Weighted length: a CJK character counts as two.
check("CJK counts double", textWeight("周杰倫 ab") === 9);
const zhLong = `== 音乐事业 ==\n${"周杰伦于二零零零年发行首张专辑。".repeat(80)}`;
const zhCapped = historyFromExtract(zhLong, historyHeadingsFor("zh"));
check("a Chinese history is capped by weight", textWeight(zhCapped.join("")) <= 1400, String(textWeight(zhCapped.join(""))));
check("and cut after its full stop, with no space before the ellipsis", zhCapped.at(-1).endsWith("。…"));

// Headings real Chinese, Japanese and Korean articles use.
const zhArticle = "周杰伦，台湾男歌手。\n== 早年 ==\n童年。\n== 音乐事业 ==\n2000年发行首张专辑。\n== 个人生活 ==\nx";
check("音乐事业 is the history, 早年 is not", same(historyFromExtract(zhArticle, historyHeadingsFor("zh")), ["2000年发行首张专辑。"]));
check("乐团历程, a band's", same(historyFromExtract("x\n== 成员介绍 ==\ny\n== 乐团历程 ==\n1999年出道。", historyHeadingsFor("zh")), ["1999年出道。"]));
check("生涯, Misora Hibari's", same(historyFromExtract("x\n== 生涯 ==\n横浜に生まれる。", historyHeadingsFor("ja")), ["横浜に生まれる。"]));
check("생애, Cho Yong-pil's", same(historyFromExtract("x\n== 학력 ==\ny\n== 생애 ==\n1950년 출생.", historyHeadingsFor("ko")), ["1950년 출생."]));

// Albums: plain albums when there are too few studio ones.
const albumRow = (id, title, date, studio) => ({
  album: { value: `http://www.wikidata.org/entity/${id}` },
  albumLabel: { value: title },
  studio: { value: studio ? "1" : "0" },
  ...(date ? { released: { value: `${date}-01-01T00:00:00Z` } } : {}),
});
const iu = albumList({
  results: {
    bindings: [
      albumRow("Q1", "Lost and Found (아이유의 음반)", "2008", false),
      albumRow("Q2", "Palette", "2017", false),
      albumRow("Q3", "Summer Love", null, false),
      albumRow("Q4", "Modern Times", "2013", true),
    ],
  },
});
check("too few studio albums: the plain ones as well", !iu.studioOnly && iu.albums.length === 3, JSON.stringify(iu));
check("the undated plain one left out", !iu.albums.some((a) => a.title === "Summer Love"));
check("the article's qualifier left off the title", iu.albums[0].title === "Lost and Found");
const plenty = albumList({
  results: { bindings: [albumRow("Q1", "A", "2001", true), albumRow("Q2", "B", "2002", true), albumRow("Q3", "C", "2003", true), albumRow("Q4", "Live", "2004", false)] },
});
check("enough studio albums: those only", plenty.studioOnly && same(plenty.albums.map((a) => a.title), ["A", "B", "C"]));
const zhRow = (id, title, lang) => ({ ...albumRow(id, title, "2001", true), albumLabel: { value: title, "xml:lang": lang } });
const folded = albumList(
  { results: { bindings: [zhRow("Q1", "五月天第一張創作專輯", "zh"), zhRow("Q2", "裏町酒場", "ja"), zhRow("Q3", "范特西", "zh-hans")] } },
  { simplify: true },
);
check(
  "a simplified-Chinese reader gets traditional Chinese titles in simplified script, and Japanese ones as they are",
  same(folded.albums.map((a) => a.title), ["五月天第一张创作专辑", "裏町酒場", "范特西"]),
  JSON.stringify(folded.albums),
);
check("nobody else has titles folded", albumList({ results: { bindings: [zhRow("Q1", "五月天第一張創作專輯", "zh")] } }).albums[0].title === "五月天第一張創作專輯");
check("an answer with no studio column is all studio albums", albumList({ results: { bindings: [row("Q9", "Awake", "1994")] } }).albums.length === 1);

// The whole lookup, for a Taiwanese singer whose English article is a stub.
const JAY = {
  id: "Q238819",
  labels: { en: { language: "en", value: "Jay Chou" }, "zh-hans": { language: "zh-hans", value: "周杰伦" } },
  descriptions: { en: { value: "Taiwanese singer" } },
  sitelinks: {
    enwiki: { site: "enwiki", title: "Jay Chou", url: "https://en.wikipedia.org/wiki/Jay_Chou" },
    zhwiki: { site: "zhwiki", title: "周杰倫", url: "https://zh.wikipedia.org/wiki/%E5%91%A8%E6%9D%B0%E5%80%AB" },
    plwiki: { site: "plwiki", title: "Jay Chou", url: "https://pl.wikipedia.org/wiki/Jay_Chou" },
  },
  claims: { P434: [{}], P27: [itemClaim("Q865")] },
};
const JAY_LONG_ZH = `周杰伦，台湾男歌手。\n== 早年 ==\n童年。\n== 音乐事业 ==\n${"周杰伦于二零零零年发行首张专辑。".repeat(30)}`;
const JAY_LONG_EN = `Lead.\n== Career ==\n${"Chou released his debut album in 2000. ".repeat(20)}`;

function cjkFetch({ en = "Jay Chou is a singer.", pl = "Piosenkarz." } = {}) {
  const calls = [];
  const fetchJson = async (url) => {
    calls.push(url);
    const u = new URL(url);
    const p = u.searchParams;
    if (u.host === "query.wikidata.org") return { results: { bindings: [] } };
    if (u.host.endsWith("wikipedia.org")) {
      const edition = u.host.split(".")[0];
      const extract = edition === "zh" ? JAY_LONG_ZH : edition === "en" ? en : pl;
      return { query: { pages: [{ extract }] } };
    }
    if (p.get("action") === "wbsearchentities") return { search: [{ id: "Q238819" }] };
    if (p.get("props").startsWith("labels")) {
      return { entities: { Q238819: { labels: { ...JAY.labels, "zh-tw": { language: "zh-tw", value: "周杰倫" } } } } };
    }
    return { entities: { Q238819: JAY } };
  };
  return { calls, fetchJson };
}
const wikipediaCalls = (calls) => calls.filter((url) => new URL(url).host.endsWith("wikipedia.org")).map((url) => new URL(url));

const underEnglish = cjkFetch();
const jayEn = await lookupArtist("周杰倫", "en", { fetchJson: underEnglish.fetchJson });
check("a Chinese name is searched among Chinese labels", new URL(underEnglish.calls[0]).searchParams.get("language") === "zh");
check("the name in English", jayEn?.name === "Jay Chou");
check("with his own name beside it", jayEn?.nativeName === "周杰倫" && jayEn?.nativeLang === "zh-tw");
check("an English stub gives way to the Chinese article", jayEn?.historyLang === "zh" && textWeight(jayEn.history.join("")) > 400);
check("read in the script Taiwan writes", jayEn?.historyVariant === "zh-tw");
check(
  "asked in that script",
  wikipediaCalls(underEnglish.calls).some((u) => u.host === "zh.wikipedia.org" && u.searchParams.get("variant") === "zh-tw"),
);
check("the link opens the article in that script", jayEn?.articleUrl.startsWith("https://zh.wikipedia.org/zh-tw/"));
check("a file tagged 周杰倫 matches him under English", artistMatchesName(jayEn, "周杰倫"));

const underChinese = cjkFetch();
const jayZh = await lookupArtist("周杰倫", "zh-Hans", { fetchJson: underChinese.fetchJson });
check("the simplified name under a simplified-Chinese app", jayZh?.name === "周杰伦");
check("and no second name in the other script", nativeNameToShow(jayZh?.name, jayZh?.nativeName) === "");
check("the Chinese article in simplified script", jayZh?.historyLang === "zh" && jayZh?.historyVariant === "zh-hans");
check("asked once, the reader's own edition being long", wikipediaCalls(underChinese.calls).length === 1);
check(
  "simplified labels are asked for",
  underChinese.calls.some((url) => (new URL(url).searchParams.get("languages") || "").startsWith("zh-hans|zh-cn|zh|en")),
);

const underPolish = cjkFetch({ en: JAY_LONG_EN });
const jayPl = await lookupArtist("", "pl", { id: "Q238819", fetchJson: underPolish.fetchJson });
check("a Polish stub gives way to English before Chinese", jayPl?.historyLang === "en", jayPl?.historyLang);
check("the link is the article shown", jayPl?.articleUrl === "https://en.wikipedia.org/wiki/Jay_Chou");

const allStubs = await lookupArtist("", "pl", { id: "Q238819", fetchJson: cjkFetch().fetchJson });
check("the Chinese article when both others are stubs", allStubs?.historyLang === "zh");

console.log(`${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
