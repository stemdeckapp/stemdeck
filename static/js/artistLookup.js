// Who made the song that is open: history, members and studio albums, looked
// up on Wikidata and Wikipedia for the box the now-playing card opens (#699).
//
// A title often leaves the band out ("Metropolis - Part I" has no "Dream
// Theater" in it), so the name comes from the file's artist tag when it had
// one, and is typed in the box otherwise. A band found either way can be saved
// on the track. This module only takes a name, or a saved band's id, and looks
// it up.
//
// It runs when the box is opened, and once in the background when a track
// whose file carries an artist tag is opened with no band saved on it (see
// artistInfo.js). What is sent is that one name, the search field's or the
// tag's, nothing else about the track.
//
// Wikidata rather than Wikipedia's own sections for members and albums:
// section titles and list layouts differ per article and per language, and
// parsing them is a guess. Wikidata has them as data. Wikipedia supplies only
// the prose, the History section, which Wikidata does not have.
//
// No DOM here, and fetch is passed in, so all of it runs under node for the
// unit tests (tests/js/artist-lookup.test.mjs).

import { toSimplified } from "./zhVariants.js";

const WIKIDATA_API = "https://www.wikidata.org/w/api.php";
const WIKIDATA_SPARQL = "https://query.wikidata.org/sparql";

// Having a MusicBrainz artist ID is what separates the band from the album
// named after it, a cinema that shares its name, or a disambiguation page. A
// search for "Dream Theater" returns all of those, band first only sometimes.
const MUSICBRAINZ_ARTIST_ID = "P434";
const HAS_PART = "P527";
const END_TIME = "P582";
const IMAGE = "P18";

// Studio albums only. Everything credited to a band includes demos, bootlegs
// and video releases: over sixty rows for Dream Theater, and the query took ten
// seconds. Studio albums are modelled two ways on Wikidata, as their own class
// or as an album whose form is "studio album", so the query takes both.
const STUDIO_ALBUM = "Q208569";
const ALBUM = "Q482994";
// A deluxe or remastered edition is its own item, typed as a studio album.
// It is marked either by pointing back at the original ("edition or
// translation of") or by naming its edition ("edition/version"); Adele's "19 -
// deluxe edition" only has the second. Either one leaves it out.
const EDITION_OF = "P629";
const EDITION_NAME = "P9767";

// How long an answer may take before the box gives up and says so.
const TIMEOUT_MS = 12000;

// Caps, so a long article or a large collective cannot make the box a wall.
const HISTORY_MAX_CHARS = 1400;
const MEMBERS_MAX = 40;

// The app's language codes to Wikipedia editions. zh-Hans reads the Chinese
// Wikipedia, which serves simplified script to a zh-Hans reader through its
// own variant handling; pt-PT reads the one Portuguese Wikipedia.
const WIKI_LANG = {
  "en": "en", "pl": "pl", "ja": "ja", "ko": "ko", "zh-Hans": "zh",
  "de": "de", "fr": "fr", "es": "es", "pt": "pt", "pt-PT": "pt", "id": "id",
};

// The level-two heading that holds a band's history, per edition, lower case
// and matched as a prefix. English is always tried as well, because an
// article the chosen language does not have is read from the English edition.
//
// Chinese and Japanese headings are matched anywhere in the heading, not only
// at its start: they are written without spaces, so the career of a singer is
// "音乐事业" (music career) or "演艺生涯" (performing career), a band's is
// "乐团历程", and 陳奕迅's is "音乐生涯". The lists come from the headings real
// articles use (周杰倫, 鄧麗君, 五月天, 王菲, 林俊傑, 張學友, 蔡依林; 宇多田ヒカル,
// 米津玄師, YOASOBI, 美空ひばり, 中島みゆき; 아이유, 뉴진스, 싸이, 조용필). "早年"
// (early years) is left out on purpose, as English leaves out "early life":
// the career that follows it is the history.
const HISTORY_HEADINGS = {
  en: ["history", "biography", "career"],
  pl: ["historia", "biografia", "kariera"],
  ja: ["略歴", "経歴", "来歴", "歴史", "生涯", "バイオグラフィ"],
  ko: ["역사", "연혁", "경력", "약력", "이력", "생애", "활동"],
  zh: [
    "历史", "歷史", "经历", "經歷", "历程", "歷程", "简历", "簡歷",
    "生平", "事业", "事業", "生涯",
  ],
  de: ["geschichte", "bandgeschichte", "biografie", "biographie", "werdegang"],
  fr: ["historique", "histoire", "biographie", "carrière"],
  es: ["historia", "biografía", "trayectoria"],
  pt: ["história", "biografia", "carreira"],
  id: ["sejarah", "biografi", "karier"],
};

/** The Wikipedia edition for an app language code. */
export function wikiLanguage(appLang) {
  return WIKI_LANG[appLang] || "en";
}

// ─── Languages other than the reader's ───
//
// A Chinese, Japanese or Korean artist often has a long article in their own
// language and a short one, or none, in English, and a label in the reader's
// language that is not the name they are known by at home. So an artist is
// read in three languages, in this order: the app's, English, and the
// artist's own. Names are asked for in all three, and the artist's own name is
// shown beside the one in the reader's language when the two differ.

// The app's language codes to the Wikidata language names are asked in. Not
// the edition's code: zh-Hans asks for the simplified label, which Wikidata
// keeps apart from the plain "zh" one (the plain one mixes both scripts, and
// was vandalised for BTS while "zh-hans" was right), and the app's
// Portuguese is Brazilian, "pt-br" there, with plain "pt" behind it.
const LABEL_LANG = {
  "en": "en", "pl": "pl", "ja": "ja", "ko": "ko", "zh-Hans": "zh-hans",
  "de": "de", "fr": "fr", "es": "es", "pt": "pt-br", "pt-PT": "pt", "id": "id",
};

/** The Wikidata language labels are asked in for an app language code. */
export function labelLanguage(appLang) {
  return LABEL_LANG[appLang] || "en";
}

/**
 * The Wikidata languages to take a name in, most wanted first: the app's,
 * English, the language-neutral "mul" label many names now have instead of
 * one per language, and then `native`, the artist's own.
 */
export function labelChain(appLang, native = "") {
  const ui = labelLanguage(appLang);
  const chain = [ui];
  if (ui === "zh-hans") chain.push("zh-cn", "zh");
  if (ui === "pt-br") chain.push("pt");
  chain.push("en", "mul");
  if (native) chain.push(native, native.split("-")[0]);
  return [...new Set(chain)];
}

// Where an artist is from, as the Wikidata language their name is written in
// at home. Chinese is narrowed to the script the place writes: traditional in
// Taiwan and Hong Kong, simplified on the mainland. Countries with more than
// one language (Belgium, Switzerland, Canada, India, Singapore) are left out,
// so the languages the artist speaks decide instead.
const COUNTRY_LANG = {
  Q148: "zh-cn", Q865: "zh-tw", Q8646: "zh-hk", Q14773: "zh-hk",
  Q17: "ja", Q884: "ko", Q423: "ko",
  Q36: "pl", Q183: "de", Q40: "de", Q142: "fr", Q155: "pt-br", Q45: "pt", Q252: "id",
  Q29: "es", Q96: "es", Q414: "es", Q298: "es", Q739: "es", Q717: "es", Q419: "es",
  Q241: "es", Q733: "es", Q77: "es", Q750: "es", Q736: "es", Q786: "es", Q774: "es", Q1183: "es",
  Q38: "it", Q159: "ru", Q212: "uk", Q55: "nl", Q34: "sv", Q20: "nb", Q35: "da", Q33: "fi",
  Q41: "el", Q43: "tr", Q869: "th", Q881: "vi", Q794: "fa", Q801: "he", Q213: "cs",
  Q28: "hu", Q218: "ro",
  Q30: "en", Q145: "en", Q408: "en", Q664: "en", Q27: "en",
};

// Language items, as a native language (P103), a language spoken (P1412), or
// the original language of a film or series (P364), to Wikidata language codes.
const LANGUAGE_ITEM = {
  Q1860: "en", Q7850: "zh", Q9192: "zh", Q727694: "zh", Q262828: "zh-tw",
  Q9186: "zh-hk", Q7033959: "zh-hk", Q5287: "ja", Q9176: "ko",
  Q809: "pl", Q188: "de", Q150: "fr", Q1321: "es", Q5146: "pt", Q750553: "pt-br", Q9240: "id",
  Q7026: "ca", Q652: "it", Q7737: "ru", Q8798: "uk", Q7411: "nl", Q9027: "sv", Q9035: "da",
  Q1412: "fi", Q9043: "nb", Q25167: "nb", Q36510: "el", Q9056: "cs", Q9058: "sk", Q7913: "ro",
  Q9217: "th", Q9199: "vi", Q256: "tr", Q9168: "fa", Q9288: "he", Q13955: "ar",
};

const NATIVE_LABEL = "P1705";
const NAME_IN_NATIVE_LANGUAGE = "P1559";
const TITLE = "P1476";
const NATIVE_LANGUAGE = "P103";
const LANGUAGES_SPOKEN = "P1412";
const ORIGINAL_LANGUAGE = "P364";
const LANGUAGE_OF_WORK = "P407";
const COUNTRY_OF_ORIGIN = "P495";
const CITIZENSHIP = "P27";
const COUNTRY = "P17";

/** The edition a Wikidata language code is written in: zh-tw reads zh. */
function editionOf(code) {
  const base = String(code || "").split("-")[0];
  return base === "nb" ? "no" : base;
}

// Every edition the box can read from: the app's, and any artist's own. The
// sitelinks asked for are these, so a Japanese band's jawiki is there to fall
// back to under any language, without every one of its eighty.
const EDITIONS = [...new Set([
  ...Object.values(WIKI_LANG),
  ...Object.values(COUNTRY_LANG).map(editionOf),
  ...Object.values(LANGUAGE_ITEM).map(editionOf),
])];
const EDITION_SITES = EDITIONS.map((edition) => `${edition}wiki`).join("|");

/**
 * The language a name's own script says it is in, or "" for Latin script and
 * anything else that says nothing: kana is Japanese, Hangul Korean, Han with
 * neither Chinese, and so on. The server's search_language
 * (app/pipeline/artist_lookup.py) is the same rule.
 */
export function scriptLanguage(name) {
  const text = String(name || "");
  if (/[\p{Script=Hiragana}\p{Script=Katakana}]/u.test(text)) return "ja";
  if (/\p{Script=Hangul}/u.test(text)) return "ko";
  if (/\p{Script=Han}/u.test(text)) return "zh";
  if (/\p{Script=Cyrillic}/u.test(text)) return "ru";
  if (/\p{Script=Greek}/u.test(text)) return "el";
  if (/\p{Script=Thai}/u.test(text)) return "th";
  if (/\p{Script=Hebrew}/u.test(text)) return "he";
  if (/\p{Script=Arabic}/u.test(text)) return "ar";
  return "";
}

// UI languages whose own labels are in a script of their own: a Latin-script
// name searched among them ranks whatever their label for it happens to be,
// so "Queen" in Chinese found Queen Latifah before the band.
const NON_LATIN_UI = new Set(["ja", "ko", "zh-hans"]);

/**
 * The Wikidata languages to search a name in, in order: the one its script
 * says ("周杰倫" among Chinese labels, "아이유" among Korean ones), else the
 * app's own for a Latin-script name when the app is in a Latin-script
 * language, else English; and English after that, for a name the first
 * search found no artist for.
 */
export function searchLanguages(name, appLang) {
  const ui = labelLanguage(appLang);
  const first = scriptLanguage(name) || (NON_LATIN_UI.has(ui) ? "en" : ui);
  return [...new Set([first, "en"])];
}

function mappedIds(claims, prop, map) {
  const codes = [];
  for (const statement of claims?.[prop] || []) {
    if (statement?.rank === "deprecated") continue;
    const code = map[statement?.mainsnak?.datavalue?.value?.id];
    if (code) codes.push(code);
  }
  return codes;
}

// Monolingual text values ({ language, text }) of a property, in order.
function monolingual(claims, prop) {
  const values = [];
  for (const statement of claims?.[prop] || []) {
    if (statement?.rank === "deprecated") continue;
    const value = statement?.mainsnak?.datavalue?.value;
    if (typeof value?.text === "string" && typeof value?.language === "string" && value.language !== "mul") {
      values.push({ language: value.language.toLowerCase(), text: value.text });
    }
  }
  return values;
}

const ZH_REGION = /^zh-(tw|hk|mo|cn|sg|my)$/;

/**
 * The Wikidata language an artist's (or a work's) own name is in, or "".
 * From, in order: the language of its native name or title, its native
 * language, the country it is from, a language it works in, and the script
 * of the name it was searched by. Chinese is then narrowed to its region
 * when anything says which, so Jay Chou (Taiwan) is zh-tw and Faye Wong
 * (the mainland) zh-cn.
 */
export function nativeLanguage(claims, name = "") {
  const candidates = [
    ...[TITLE, NATIVE_LABEL, NAME_IN_NATIVE_LANGUAGE].flatMap((prop) => monolingual(claims, prop).map((v) => v.language)),
    ...mappedIds(claims, NATIVE_LANGUAGE, LANGUAGE_ITEM),
    ...mappedIds(claims, ORIGINAL_LANGUAGE, LANGUAGE_ITEM),
    ...[COUNTRY_OF_ORIGIN, CITIZENSHIP, COUNTRY].flatMap((prop) => mappedIds(claims, prop, COUNTRY_LANG)),
    ...mappedIds(claims, LANGUAGE_OF_WORK, LANGUAGE_ITEM),
    ...mappedIds(claims, LANGUAGES_SPOKEN, LANGUAGE_ITEM),
    scriptLanguage(name),
  ].filter((code) => /^[a-z]{2,3}(-[a-z]{2,4})?$/.test(code));
  let native = candidates[0] || "";
  if (native.split("-")[0] === "zh" && !ZH_REGION.test(native)) {
    native = candidates.find((code) => ZH_REGION.test(code)) || native;
  }
  return native;
}

/**
 * An entity's name in `native`, its own language, or "" when it has none:
 * a label in that language (not one Wikidata filled in from another), else
 * the native name or title its claims give in that language.
 */
export function nativeLabel(entity, native, claims = entity?.claims) {
  const base = String(native || "").split("-")[0];
  if (!base) return "";
  for (const code of [native, base]) {
    const label = entity?.labels?.[code];
    const from = String(label?.language || code).split("-")[0];
    if (label?.value && from === base) return label.value;
  }
  for (const prop of [NATIVE_LABEL, NAME_IN_NATIVE_LANGUAGE, TITLE]) {
    const value = monolingual(claims, prop).find((v) => v.language.split("-")[0] === base);
    if (value) return value.text;
  }
  return "";
}

// Han characters only (and the punctuation between them).
const HAN_ONLY = /^[\p{Script=Han}\p{P}\s]+$/u;

/**
 * The native name to show beside `name`, or "" when it would say nothing new:
 * none, the same name once case and punctuation are set aside, or the same
 * Han characters in the other script (周杰伦 and 周杰倫, 米津玄师 and 米津玄師),
 * which a reader of either script already reads as one name.
 */
export function nativeNameToShow(name, native) {
  const want = String(native || "").trim();
  if (!want || artistNameKey(want) === artistNameKey(name)) return "";
  if (HAN_ONLY.test(want) && HAN_ONLY.test(String(name || "")) && [...want].length === [...String(name)].length) return "";
  return want;
}

/**
 * A Wikidata language code as an HTML lang attribute: "zh-tw" is "zh-TW",
 * "zh-hans" is "zh-Hans". It picks the font: the same Han character is drawn
 * differently in Japanese and in traditional and simplified Chinese.
 */
export function languageTag(code) {
  const [base, region] = String(code || "").toLowerCase().split("-");
  if (!region) return base || "";
  if (region.length === 4) return `${base}-${region[0].toUpperCase()}${region.slice(1)}`;
  return `${base}-${region.toUpperCase()}`;
}

// A Chinese, Japanese or Korean character says about what two Latin letters
// do, and takes as much room on screen. Weighed as two, the caps below give
// those languages as much history as any other, not twice the wall of it,
// and a full article in them is not mistaken for a stub.
const WIDE_CHAR = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u;

/** Text length with CJK characters counted twice. */
export function textWeight(text) {
  let weight = 0;
  for (const ch of String(text || "")) weight += WIDE_CHAR.test(ch) ? 2 : 1;
  return weight;
}

/**
 * The first search result that is an artist, in the order the search ranked
 * them, or null. `entities` is wbgetentities' `entities` object.
 */
export function pickArtist(entities, rankedIds) {
  for (const id of rankedIds) {
    const entity = entities?.[id];
    if (entity?.claims?.[MUSICBRAINZ_ARTIST_ID]?.length) return entity;
  }
  return null;
}

/**
 * Current and former members from a band's "has part" statements.
 *
 * A member who left and came back has two statements, one with an end date
 * and one without, so anyone with an open statement is current and is not
 * also listed as former. A solo artist has no such statements and gets two
 * empty lists, which the box shows as no Members section at all.
 */
export function membersFromClaims(claims) {
  const current = new Set();
  const former = new Set();
  for (const statement of claims?.[HAS_PART] || []) {
    if (statement.rank === "deprecated") continue;
    const id = statement.mainsnak?.datavalue?.value?.id;
    if (!id) continue;
    if (statement.qualifiers?.[END_TIME]?.length) former.add(id);
    else current.add(id);
  }
  for (const id of current) former.delete(id);
  return {
    current: [...current].slice(0, MEMBERS_MAX),
    former: [...former].slice(0, MEMBERS_MAX),
  };
}

/**
 * Studio albums from the SPARQL answer, oldest first, one row per album.
 * `studioOnly: false` also takes the rows marked as plain albums of no stated
 * form (see sparqlQuery), less any with no date: undated ones are nearly all
 * soundtracks and tour recordings.
 */
export function albumsFromSparql(json, { studioOnly = true, simplify = false } = {}) {
  const seen = new Set();
  const albums = [];
  for (const row of json?.results?.bindings || []) {
    const id = row.album?.value;
    const title = row.albumLabel?.value;
    // The label service falls back to the bare Q-number when an album has no
    // label in any language asked for. That is not a title.
    if (!id || !title || /^Q\d+$/.test(title) || seen.has(id)) continue;
    const studio = row.studio?.value !== "0";
    const year = /^(\d{4})/.exec(row.released?.value || "")?.[1] || "";
    if (!studio && (studioOnly || !year)) continue;
    seen.add(id);
    const shown = inScript(title, row.albumLabel["xml:lang"], simplify);
    albums.push({ title: shown.replace(ALBUM_QUALIFIER, "") || shown, year });
  }
  // SPARQL sorted already; sorted again so a caller never depends on it, and
  // an album with no date goes last rather than first.
  return albums.sort((a, b) => (a.year || "9999").localeCompare(b.year || "9999"));
}

// The article title's qualifier some labels carry, most often Chinese, Korean
// and Japanese ones: "Diorama (音樂專輯)", "Lost and Found (아이유의 음반)",
// "WAKE UP (防彈少年團專輯)". It says the album is an album, in a list of them.
const ALBUM_QUALIFIER = /\s*[(（][^()（）]*(?:专辑|專輯|アルバム|음반|앨범|\balbum\b)[)）]\s*$/iu;

// Fewer studio albums than this and the plain albums are shown as well.
const STUDIO_ALBUMS_ENOUGH = 3;

/**
 * The album list for the box, and whether it is studio albums only. Most
 * Chinese, Japanese and Korean albums on Wikidata are typed as plain albums
 * with no form given, so asking for studio albums alone found none for IU,
 * Mayday, YOASOBI or Misora Hibari. For an artist with too few studio albums
 * the plain ones are shown too, under "Albums" rather than "Studio albums".
 */
export function albumList(json, { simplify = false } = {}) {
  const studio = albumsFromSparql(json, { simplify });
  if (studio.length >= STUDIO_ALBUMS_ENOUGH) return { albums: studio, studioOnly: true };
  const all = albumsFromSparql(json, { studioOnly: false, simplify });
  return all.length > studio.length ? { albums: all, studioOnly: false } : { albums: studio, studioOnly: true };
}

/**
 * The History section of a plain-text article extract, as paragraphs, capped
 * at about `maxChars` (HISTORY_MAX_CHARS) and cut at a sentence. Falls back to the text
 * before the first heading, the article's own summary, when no heading in
 * `headings` is found. Sub-headings inside the section are dropped: the box
 * shows prose, not the article's outline.
 */
export function historyFromExtract(extract, headings, maxChars = HISTORY_MAX_CHARS) {
  const text = String(extract || "");
  const lines = text.split("\n");
  const isLevel2 = (line) => /^==[^=].*[^=]==\s*$/.test(line.trim());
  const headingText = (line) => line.trim().replace(/^=+\s*|\s*=+$/g, "").toLowerCase();
  const matches = (text, h) => text.startsWith(h) || (UNSPACED.test(h) && text.includes(h));

  let start = lines.findIndex((line) =>
    isLevel2(line) && headings.some((h) => matches(headingText(line), h)));
  let body;
  if (start >= 0) {
    const rest = lines.slice(start + 1);
    const end = rest.findIndex(isLevel2);
    body = end >= 0 ? rest.slice(0, end) : rest;
  } else {
    const first = lines.findIndex((line) => /^=+.*=+\s*$/.test(line.trim()));
    body = first >= 0 ? lines.slice(0, first) : lines;
  }

  const paragraphs = [];
  let length = 0;
  for (const raw of body) {
    const line = raw.trim();
    if (!line || /^=+.*=+$/.test(line)) continue;
    const weight = textWeight(line);
    if (length + weight <= maxChars) {
      paragraphs.push(line);
      length += weight;
      continue;
    }
    // The paragraph that crosses the cap is cut at its last full sentence
    // inside it, or left out if not even one sentence fits. The cap is by
    // weight (textWeight), so the cut is found character by character.
    const room = maxChars - length;
    let end = 0;
    let used = 0;
    for (const ch of line) {
      used += WIDE_CHAR.test(ch) ? 2 : 1;
      if (used > room) break;
      end += ch.length;
    }
    const cut = line.slice(0, end);
    const stop = Math.max(
      cut.lastIndexOf(". "), cut.lastIndexOf("! "), cut.lastIndexOf("? "),
      cut.lastIndexOf("。"), cut.lastIndexOf("！"), cut.lastIndexOf("？"),
    );
    // No space before the ellipsis after a full-width stop, as the script
    // would write it.
    const gap = /[。！？]/.test(cut[stop]) ? "" : " ";
    if (stop > 40) paragraphs.push(`${cut.slice(0, stop + 1)}${gap}${ELLIPSIS}`);
    break;
  }
  return paragraphs;
}

const ELLIPSIS = String.fromCodePoint(0x2026);
// A heading in a script written without spaces, matched anywhere in a heading.
const UNSPACED = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]/u;

/** Headings to try for an edition: its own, then the English ones. */
export function historyHeadingsFor(wikiLang) {
  return [...new Set([...(HISTORY_HEADINGS[wikiLang] || []), ...HISTORY_HEADINGS.en])];
}

// ─── The work a soundtrack is from ───
//
// A song from a musical or a film has the work behind it as well as, or
// instead of, a band. The server finds which work (app/pipeline/work_lookup.py)
// and keeps its Wikidata id on the track; this looks it up by that id for the
// box: year, picture, a short synopsis, and who wrote the music, the lyrics and
// the book, from Wikidata; the synopsis from Wikipedia, the same way as a
// band's History.

const COMPOSER = "P86";
const LYRICIST = "P676";
const LIBRETTIST = "P87";
// When a work first came out, whichever is earliest: a film is published, a
// stage show first performed, a series starts.
const WORK_DATES = ["P577", "P1191", "P580"];
const PEOPLE_MAX = 6;
const SYNOPSIS_MAX_CHARS = 700;

// The level-two heading that holds a work's story, per edition, as for
// HISTORY_HEADINGS. No such section, and the article's lead stands in for it.
const SYNOPSIS_HEADINGS = {
  en: ["plot", "synopsis", "story", "premise"],
  pl: ["fabuła", "opis fabuły", "treść", "streszczenie"],
  ja: ["あらすじ", "ストーリー", "物語"],
  ko: ["줄거리", "시놉시스"],
  zh: ["剧情", "劇情", "情节", "情節", "故事", "劇情簡介", "剧情简介"],
  de: ["handlung", "inhalt"],
  fr: ["synopsis", "résumé", "intrigue"],
  es: ["argumento", "sinopsis", "trama"],
  pt: ["enredo", "sinopse", "trama"],
  id: ["alur", "sinopsis", "plot", "cerita"],
};

/** Story headings to try for an edition: its own, then the English ones. */
export function synopsisHeadingsFor(wikiLang) {
  return [...new Set([...(SYNOPSIS_HEADINGS[wikiLang] || []), ...SYNOPSIS_HEADINGS.en])];
}

/** A work's story, from an article extract: its Plot or Synopsis, else its lead. */
export function synopsisFromExtract(extract, wikiLang) {
  return historyFromExtract(extract, synopsisHeadingsFor(wikiLang), SYNOPSIS_MAX_CHARS);
}

function claimIds(claims, prop) {
  const ids = [];
  for (const statement of claims?.[prop] || []) {
    if (statement.rank === "deprecated") continue;
    const id = statement.mainsnak?.datavalue?.value?.id;
    if (/^Q\d+$/.test(id || "") && !ids.includes(id)) ids.push(id);
  }
  return ids.slice(0, PEOPLE_MAX);
}

/**
 * What a work's claims say about it: the year it first came out ("" when
 * none), and the ids of its composers, lyricists and book writers.
 */
export function workFacts(claims) {
  const years = [];
  for (const prop of WORK_DATES) {
    for (const statement of claims?.[prop] || []) {
      const year = /^[+]?(\d{4})/.exec(statement.mainsnak?.datavalue?.value?.time || "")?.[1];
      if (year) years.push(year);
    }
  }
  return {
    year: years.sort()[0] || "",
    composers: claimIds(claims, COMPOSER),
    lyricists: claimIds(claims, LYRICIST),
    bookWriters: claimIds(claims, LIBRETTIST),
  };
}

// ─── Official links ───
//
// A band's or a show's own site, Instagram, Spotify and Apple Music pages,
// from the Wikidata item already fetched for the box: no other service is
// asked. Wikidata is edited by anyone, so every value is checked against the
// shape its property allows before a URL is built from it, and a value that
// does not fit is dropped rather than repaired. The box puts them in with
// setAttribute and textContent only.

const OFFICIAL_WEBSITE = "P856";
const INSTAGRAM_USERNAME = "P2003";
const SPOTIFY_ARTIST_ID = "P1902";
const APPLE_MUSIC_ARTIST_ID = "P2850";
const URL_MAX_CHARS = 2048;

// In the order the box shows them.
const LINK_KINDS = [
  {
    kind: "website",
    prop: OFFICIAL_WEBSITE,
    build: (value) => {
      // Nothing that is not plainly a web address: no spaces or control
      // characters for a parser to be lenient about, no javascript: or data:,
      // no user:password@ to disguise where it goes.
      if (value.length > URL_MAX_CHARS || /[\s\p{Cc}]/u.test(value)) return "";
      // A host straight after the scheme: the URL parser would read a third
      // slash or a backslash leniently, and what is shown should be what
      // Wikidata said.
      if (!/^https?:\/\/[^/\\]/i.test(value)) return "";
      let url;
      try {
        url = new URL(value);
      } catch {
        return "";
      }
      if (url.protocol !== "https:" && url.protocol !== "http:") return "";
      if (!url.hostname || url.username || url.password) return "";
      return url.href;
    },
  },
  {
    kind: "instagram",
    prop: INSTAGRAM_USERNAME,
    // Letters, digits, dots and underscores, up to 30, and not only dots.
    build: (value) => (/^(?=.*[A-Za-z0-9_])[A-Za-z0-9._]{1,30}$/.test(value)
      ? `https://www.instagram.com/${encodeURIComponent(value)}/`
      : ""),
  },
  {
    kind: "spotify",
    prop: SPOTIFY_ARTIST_ID,
    // Spotify's base-62 ids are always 22 characters.
    build: (value) => (/^[0-9A-Za-z]{22}$/.test(value)
      ? `https://open.spotify.com/artist/${encodeURIComponent(value)}`
      : ""),
  },
  {
    kind: "appleMusic",
    prop: APPLE_MUSIC_ARTIST_ID,
    // The iTunes artist id: digits only.
    build: (value) => (/^[1-9][0-9]{0,14}$/.test(value)
      ? `https://music.apple.com/artist/${encodeURIComponent(value)}`
      : ""),
  },
];

/**
 * The URL for one official link, or "" when `value` is not the shape `kind`
 * allows. `kind` is one of "website", "instagram", "spotify", "appleMusic".
 */
export function officialLinkUrl(kind, value) {
  const spec = LINK_KINDS.find((k) => k.kind === kind);
  if (!spec || typeof value !== "string") return "";
  return spec.build(value);
}

/**
 * The official links in an item's claims, as [{ kind, url }], in the order
 * website, Instagram, Spotify, Apple Music, and only those present. Per
 * property, the preferred statement wins over normal ones, the first of each
 * rank before later ones; deprecated ones never count, and nor does one whose
 * value fails its check.
 */
export function officialLinks(claims) {
  const links = [];
  for (const { kind, prop, build } of LINK_KINDS) {
    const statements = (Array.isArray(claims?.[prop]) ? claims[prop] : [])
      .filter((s) => s?.rank === "preferred" || s?.rank === "normal");
    const ordered = [
      ...statements.filter((s) => s.rank === "preferred"),
      ...statements.filter((s) => s.rank === "normal"),
    ];
    for (const statement of ordered) {
      const value = statement.mainsnak?.snaktype === "value" || statement.mainsnak?.snaktype === undefined
        ? statement.mainsnak?.datavalue?.value
        : undefined;
      const url = typeof value === "string" ? build(value) : "";
      if (url) {
        links.push({ kind, url });
        break;
      }
    }
  }
  return links;
}

const DISCOGS_ARTIST_ID = "P1953";

/**
 * The Discogs artist id an item names (P1953), as a string of digits, or ""
 * when it names none or more than one.
 */
export function discogsArtistId(claims) {
  const ids = new Set((Array.isArray(claims?.[DISCOGS_ARTIST_ID]) ? claims[DISCOGS_ARTIST_ID] : [])
    .filter((s) => s?.rank !== "deprecated")
    .map((s) => s?.mainsnak?.datavalue?.value)
    .filter((v) => typeof v === "string" && /^[1-9][0-9]{0,11}$/.test(v)));
  return ids.size === 1 ? [...ids][0] : "";
}

// Latin accents, as the combining marks NFKD leaves them as, so a file tagged
// "Beyonce" still matches "Beyoncé". Only this block: the marks in a Japanese
// or Korean name are part of it, and folding them could make two names one.
const LATIN_ACCENTS = new RegExp(`[${String.fromCodePoint(0x300)}-${String.fromCodePoint(0x36f)}]`, "gu");

/** A name reduced to what tells it apart: no case, accents, punctuation or spaces. */
export function artistNameKey(name) {
  return String(name || "")
    .normalize("NFKD")
    .replace(LATIN_ACCENTS, "")
    .toLowerCase()
    .replace(/[\p{P}\p{S}\s]+/gu, "");
}

/**
 * The name to look up from a file's artist tag: the tag, less any featured
 * artist ("Artist feat. Someone", "Artist (ft. Someone)"), since no band is
 * called that.
 */
export function taggedArtistName(tag) {
  return String(tag || "")
    .split(/\s+[([]?(?:feat\.?|ft\.?|featuring)\s+/i)[0]
    .trim();
}

/**
 * Whether a looked-up artist is plausibly the one a file was tagged with: its
 * name in the app's language, in English, in its own language, or in another
 * language it was fetched in, is the tag's name once case, accents and
 * punctuation are set aside. A file tagged "周杰倫" is Jay Chou's in any app
 * language, and "鄧麗君" matches Teresa Teng under a simplified-Chinese app
 * whose label for her is 邓丽君.
 *
 * Wikidata's search forgives spelling and matches aliases and parts of names,
 * so a near miss still finds some band. That is right in the box, where the
 * answer is on screen to be judged before anyone saves it. Saved on a track
 * with nobody looking, the name has to agree.
 */
export function artistMatchesName(artist, name) {
  const want = artistNameKey(name);
  if (!want || !artist) return false;
  return [artist.name, artist.englishName, artist.nativeName, ...(artist.names || [])]
    .some((candidate) => artistNameKey(candidate) === want);
}

function withTimeout(signal) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new Error("timeout")), TIMEOUT_MS);
  signal?.addEventListener("abort", () => controller.abort(signal.reason), { once: true });
  return { signal: controller.signal, done: () => clearTimeout(timer) };
}

async function defaultFetchJson(url, signal) {
  const res = await fetch(url, { signal, headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`HTTP ${res.status} from ${new URL(url).host}`);
  return res.json();
}

function query(base, params) {
  const url = new URL(base);
  for (const [key, value] of Object.entries({ ...params, format: "json", origin: "*" })) {
    url.searchParams.set(key, value);
  }
  return url.toString();
}

// Album types that are not a studio album, for the plain albums of no stated
// form that the query also takes: a release typed "album" and also one of these
// is that instead.
const NOT_STUDIO = [
  "Q134556", // single
  "Q169930", // extended play
  "Q209939", // live album
  "Q222910", // compilation album
  "Q723849", // greatest hits album
  "Q4176708", // soundtrack album
  "Q963099", // remix album
  "Q220935", // demo
  "Q10590726", // video album
  "Q1892995", // mixtape
];

function sparqlQuery(qid, languages) {
  // qid is only ever an id taken from Wikidata's own answer, checked against
  // /^Q\d+$/ before it gets here, so it cannot carry anything into the query.
  // languages are codes from labelChain, the app's own tables and claims
  // checked against /^[a-z]{2,3}(-[a-z]{2,4})?$/, so they cannot either.
  // ?studio is 1 for a studio album and 0 for a plain album of no stated
  // form; albumList decides whether those are shown.
  const sparql = `SELECT ?album ?albumLabel (MIN(?date) AS ?released) (MAX(?isStudio) AS ?studio) WHERE {
  ?album wdt:P175 wd:${qid} .
  { ?album wdt:P31 wd:${STUDIO_ALBUM} . BIND(1 AS ?isStudio) }
  UNION { ?album wdt:P31 wd:${ALBUM} ; wdt:P7937 wd:${STUDIO_ALBUM} . BIND(1 AS ?isStudio) }
  UNION {
    ?album wdt:P31 wd:${ALBUM} .
    FILTER NOT EXISTS { ?album wdt:P7937 ?form }
    FILTER NOT EXISTS { VALUES ?other { ${NOT_STUDIO.map((q) => `wd:${q}`).join(" ")} } ?album wdt:P31 ?other }
    BIND(0 AS ?isStudio)
  }
  FILTER NOT EXISTS { ?album wdt:${EDITION_OF} ?original }
  FILTER NOT EXISTS { ?album wdt:${EDITION_NAME} ?edition }
  OPTIONAL { ?album wdt:P577 ?date }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "${languages.join(",")}". }
} GROUP BY ?album ?albumLabel ORDER BY ?released`;
  const url = new URL(WIKIDATA_SPARQL);
  url.searchParams.set("query", sparql);
  url.searchParams.set("format", "json");
  return url.toString();
}

// Chinese in traditional script, or in the plain "zh" that mixes both.
const TRADITIONAL = /^zh(-(hant|tw|hk|mo))?$/;

/**
 * A term in the reader's script: for a simplified-Chinese reader
 * (`simplify`), one Wikidata only has in traditional or mixed Chinese is
 * folded to simplified, so Mayday's first album is 五月天第一张创作专辑 and
 * not 五月天第一張創作專輯. Only Chinese is folded: the same characters in a
 * Japanese title are Japanese, and stay as they are.
 */
function inScript(value, language, simplify) {
  return simplify && TRADITIONAL.test(String(language || "").toLowerCase()) ? toSimplified(value) : value;
}

/** The first of `chain` an entity has a label in, or "". */
function label(entity, chain, simplify = false) {
  for (const code of chain) {
    const term = entity?.labels?.[code];
    if (term?.value) return inScript(term.value, term.language || code, simplify);
  }
  return "";
}

function description(entities, chain, simplify = false) {
  for (const code of chain) {
    for (const entity of entities) {
      const term = entity?.descriptions?.[code];
      if (term?.value) return inScript(term.value, term.language || code, simplify);
    }
  }
  return "";
}

// Every name an entity was fetched with, for artistMatchesName.
function allLabels(...entities) {
  const names = new Set();
  for (const entity of entities) {
    for (const value of Object.values(entity?.labels || {})) {
      if (value?.value) names.add(value.value);
    }
  }
  return [...names];
}

function commonsImage(claims) {
  const file = claims?.[IMAGE]?.[0]?.mainsnak?.datavalue?.value;
  return typeof file === "string"
    ? `https://commons.wikimedia.org/wiki/Special:FilePath/${encodeURIComponent(file)}?width=240`
    : "";
}

// Below this weight (textWeight) a history is a stub: a line or two, where
// another edition may have the whole story.
const HISTORY_STUB = 400;
const SYNOPSIS_STUB = 250;

/**
 * The script the Chinese Wikipedia is read in. It serves either from one
 * article, but only when asked: unasked, the text is the mix of both its
 * editors wrote. The app's simplified Chinese reads simplified; anyone else
 * reads the script the artist's own place writes, else simplified.
 */
export function chineseVariant(appLang, native = "") {
  if (appLang === "zh-Hans") return "zh-hans";
  if (native === "zh-tw" || native === "zh-hk" || native === "zh-mo") return native === "zh-mo" ? "zh-hk" : native;
  return "zh-hans";
}

/**
 * The editions to read an article from, in order: the app's, English, and
 * the artist's own, those the item has a sitelink for.
 */
export function articleEditions(appLang, native, sitelinks) {
  return [...new Set([wikiLanguage(appLang), "en", editionOf(native)])]
    .filter((edition) => edition && sitelinks?.[`${edition}wiki`]?.title);
}

/**
 * Read the article from the first of `editions` whose text is not a stub.
 * The first is asked alone, which is all it takes nearly always; only a stub,
 * or nothing, asks the rest, side by side. None of them better, the longest
 * wins, and the first on a tie. Resolves to { paragraphs, edition, variant,
 * url }; a failed request counts as an empty article, as a failed History
 * always has.
 */
async function readArticle(entity, editions, { get, read, stub, variant, what }) {
  if (!editions.length) return { paragraphs: [], edition: "", variant: "", url: "" };
  const fetchOne = async (edition) => {
    const site = entity.sitelinks[`${edition}wiki`];
    const zh = edition === "zh" ? variant : "";
    const json = await get(query(`https://${edition}.wikipedia.org/w/api.php`, {
      action: "query", prop: "extracts", explaintext: "1", exsectionformat: "wiki",
      titles: site.title, formatversion: "2", redirects: "1",
      ...(zh ? { variant: zh } : {}),
    })).catch((err) => { console.warn(`${what} lookup failed`, err); return null; });
    const paragraphs = read(json?.query?.pages?.[0]?.extract, edition);
    // The Chinese article's link opens in the script its text was shown in.
    const url = zh && typeof site.url === "string"
      ? site.url.replace(/^https:\/\/zh\.wikipedia\.org\/wiki\//, `https://zh.wikipedia.org/${zh}/`)
      : site.url || "";
    return { paragraphs, edition, variant: zh, url, weight: textWeight(paragraphs.join("")) };
  };
  const first = await fetchOne(editions[0]);
  if (first.weight >= stub || editions.length === 1) return first;
  const all = [first, ...await Promise.all(editions.slice(1).map(fetchOne))];
  return all.find((a) => a.weight >= stub) || all.reduce((best, a) => (a.weight > best.weight ? a : best));
}

/**
 * Look up the work with Wikidata id `id` for the box. Resolves to null when
 * Wikidata has no such item, and rejects when Wikidata cannot be reached.
 * The people and the synopsis are each optional, as a band's members and
 * history are: one failing leaves that part out.
 */
export async function lookupWork(id, appLang, { fetchJson = defaultFetchJson, signal } = {}) {
  if (!/^Q\d+$/.test(String(id || ""))) return null;
  const { signal: sig, done } = withTimeout(signal);
  const get = (url) => fetchJson(url, sig);
  try {
    const entities = (await get(query(WIKIDATA_API, {
      action: "wbgetentities", ids: id,
      props: "claims|sitelinks/urls|labels|descriptions",
      languages: labelChain(appLang).join("|"), languagefallback: "1",
      sitefilter: EDITION_SITES,
    })))?.entities;
    const work = entities?.[id];
    if (!work || work.missing !== undefined) return null;

    // A film or a series is in its original language, a stage show in the
    // language of its title: 千と千尋の神隠し beside Spirited Away.
    const native = nativeLanguage(work.claims);
    const chain = labelChain(appLang, native);
    const simplify = appLang === "zh-Hans";
    const facts = workFacts(work.claims);
    const peopleIds = [...new Set([...facts.composers, ...facts.lyricists, ...facts.bookWriters])];

    const [people, article] = await Promise.all([
      // The work itself too, for its name in its own language.
      get(query(WIKIDATA_API, {
        action: "wbgetentities", ids: [id, ...peopleIds].join("|"), props: "labels",
        languages: chain.join("|"), languagefallback: "1",
      })).catch((err) => { console.warn("work credits lookup failed", err); return null; }),
      readArticle(work, articleEditions(appLang, native, work.sitelinks), {
        get, read: synopsisFromExtract, stub: SYNOPSIS_STUB,
        variant: chineseVariant(appLang, native), what: "work synopsis",
      }),
    ]);
    const names = (ids) => ids.map((pid) => label(people?.entities?.[pid], chain, simplify)).filter(Boolean);
    const own = people?.entities?.[id];

    return {
      id,
      name: label(work, chain, simplify) || label(own, chain, simplify),
      nativeName: nativeLabel(own, native, work.claims) || nativeLabel(work, native),
      nativeLang: native,
      description: description([work], chain, simplify),
      year: facts.year,
      image: commonsImage(work.claims),
      synopsis: article.paragraphs,
      synopsisLang: article.edition,
      synopsisVariant: article.variant,
      composers: names(facts.composers),
      lyricists: names(facts.lyricists),
      bookWriters: names(facts.bookWriters),
      links: officialLinks(work.claims),
      articleUrl: article.url,
    };
  } finally {
    done();
  }
}

// wbgetentities takes at most fifty ids at a time.
const IDS_MAX = 50;

/**
 * Look an artist up. Resolves to null when nothing on Wikidata is an artist
 * by that name, and rejects when Wikidata or Wikipedia cannot be reached, so
 * the box can tell "no such band" from "no connection".
 *
 * `id` skips the search: a band saved on a track is kept as its Wikidata id,
 * so opening it again asks for exactly that band, in whatever language the app
 * is in now, rather than searching a name that was a label in another one.
 *
 * Members, albums and history are fetched side by side and each is optional:
 * one of them failing leaves that section out rather than failing the box.
 */
export async function lookupArtist(name, appLang, { id = "", fetchJson = defaultFetchJson, signal } = {}) {
  const search = String(name || "").trim();
  const known = /^Q\d+$/.test(id) ? id : "";
  if (!search && !known) return null;
  const ui = labelLanguage(appLang);
  const { signal: sig, done } = withTimeout(signal);
  const get = (url) => fetchJson(url, sig);
  const entitiesFor = async (ids) => (await get(query(WIKIDATA_API, {
    action: "wbgetentities", ids: ids.join("|"),
    props: "claims|sitelinks/urls|labels|descriptions",
    languages: labelChain(appLang).join("|"), languagefallback: "1",
    sitefilter: EDITION_SITES,
  })))?.entities;

  try {
    let artist = null;
    if (known) {
      artist = pickArtist(await entitiesFor([known]), [known]);
    } else {
      // In the language the name's script says first, then in English: a
      // name one search ranks no artist for, the other may.
      for (const language of searchLanguages(search, appLang)) {
        const found = await get(query(WIKIDATA_API, {
          action: "wbsearchentities", search, language, uselang: ui,
          type: "item", limit: "10",
        }));
        const ids = (found?.search || []).map((s) => s.id).filter((qid) => /^Q\d+$/.test(qid));
        if (!ids.length) continue;
        artist = pickArtist(await entitiesFor(ids), ids);
        if (artist) break;
      }
    }
    if (!artist) return null;

    const native = nativeLanguage(artist.claims, search);
    const chain = labelChain(appLang, native);
    const simplify = appLang === "zh-Hans";
    const members = membersFromClaims(artist.claims);
    const memberIds = [...members.current, ...members.former];

    const [named, albumsJson, article] = await Promise.all([
      // The artist again, for its name and description in its own language,
      // with its members' names in the same request.
      get(query(WIKIDATA_API, {
        action: "wbgetentities", ids: [artist.id, ...memberIds].slice(0, IDS_MAX).join("|"),
        props: "labels|descriptions",
        languages: chain.join("|"), languagefallback: "1",
      })).catch((err) => { console.warn("artist members lookup failed", err); return null; }),
      get(sparqlQuery(artist.id, chain))
        .catch((err) => { console.warn("artist albums lookup failed", err); return null; }),
      readArticle(artist, articleEditions(appLang, native, artist.sitelinks), {
        get, read: (extract, edition) => historyFromExtract(extract, historyHeadingsFor(edition)),
        stub: HISTORY_STUB, variant: chineseVariant(appLang, native), what: "artist history",
      }),
    ]);

    const own = named?.entities?.[artist.id];
    const people = (list) => list
      .map((mid) => {
        const entity = named?.entities?.[mid];
        return { name: label(entity, chain, simplify), native: nativeLabel(entity, native, {}) };
      })
      .filter((m) => m.name);
    const nativeName = nativeLabel(own, native, artist.claims) || nativeLabel(artist, native);
    const { albums, studioOnly } = albumList(albumsJson, { simplify });

    return {
      id: artist.id,
      name: label(artist, chain, simplify) || label(own, chain, simplify) || search,
      // Kept beside the name in the app's language: LRCLIB lists artists by
      // the name they release under, which the English label nearly always is.
      englishName: artist.labels?.en?.value || "",
      // The artist's name in their own language, and which language that is.
      nativeName,
      nativeLang: native,
      names: [...new Set([...allLabels(artist, own), nativeName].filter(Boolean))],
      description: description([artist, own], chain, simplify),
      image: commonsImage(artist.claims),
      history: article.paragraphs,
      // The edition the history was read from, which is not always the
      // app's own, and the Chinese script it was read in.
      historyLang: article.edition,
      historyVariant: article.variant,
      members: { current: people(members.current), former: people(members.former) },
      albums,
      albumsStudioOnly: studioOnly,
      links: officialLinks(artist.claims),
      articleUrl: article.url,
      // The Discogs artist the item names, which tells the box whether a
      // Discogs profile from the server is this band's (artistDiscogs.js).
      discogsId: discogsArtistId(artist.claims),
    };
  } finally {
    done();
  }
}
