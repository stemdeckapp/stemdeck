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
const HISTORY_HEADINGS = {
  en: ["history", "biography", "career"],
  pl: ["historia", "biografia", "kariera"],
  ja: ["略歴", "経歴", "来歴", "歴史", "バイオグラフィ"],
  ko: ["역사", "연혁", "경력", "활동"],
  zh: ["历史", "歷史", "经历", "經歷", "简历", "簡歷", "樂團歷史", "乐队历史"],
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

/** Studio albums from the SPARQL answer, oldest first, one row per album. */
export function albumsFromSparql(json) {
  const seen = new Set();
  const albums = [];
  for (const row of json?.results?.bindings || []) {
    const id = row.album?.value;
    const title = row.albumLabel?.value;
    // The label service falls back to the bare Q-number when an album has no
    // label in any language asked for. That is not a title.
    if (!id || !title || /^Q\d+$/.test(title) || seen.has(id)) continue;
    seen.add(id);
    const year = /^(\d{4})/.exec(row.released?.value || "")?.[1] || "";
    albums.push({ title, year });
  }
  // SPARQL sorted already; sorted again so a caller never depends on it, and
  // an album with no date goes last rather than first.
  return albums.sort((a, b) => (a.year || "9999").localeCompare(b.year || "9999"));
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

  let start = lines.findIndex((line) =>
    isLevel2(line) && headings.some((h) => headingText(line).startsWith(h)));
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
    if (length + line.length <= maxChars) {
      paragraphs.push(line);
      length += line.length;
      continue;
    }
    // The paragraph that crosses the cap is cut at its last full sentence
    // inside it, or left out if not even one sentence fits.
    const room = maxChars - length;
    const cut = line.slice(0, room);
    const stop = Math.max(cut.lastIndexOf(". "), cut.lastIndexOf("。"), cut.lastIndexOf("! "), cut.lastIndexOf("? "));
    if (stop > 40) paragraphs.push(`${cut.slice(0, stop + 1)} \u2026`);
    break;
  }
  return paragraphs;
}

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
 * name in the app's language, or in English, is the tag's name once case,
 * accents and punctuation are set aside.
 *
 * Wikidata's search forgives spelling and matches aliases and parts of names,
 * so a near miss still finds some band. That is right in the box, where the
 * answer is on screen to be judged before anyone saves it. Saved on a track
 * with nobody looking, the name has to agree.
 */
export function artistMatchesName(artist, name) {
  const want = artistNameKey(name);
  if (!want || !artist) return false;
  return [artist.name, artist.englishName].some((candidate) => artistNameKey(candidate) === want);
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

function sparqlQuery(qid, lang) {
  // qid is only ever an id taken from Wikidata's own answer, checked against
  // /^Q\d+$/ before it gets here, so it cannot carry anything into the query.
  const sparql = `SELECT ?album ?albumLabel (MIN(?date) AS ?released) WHERE {
  ?album wdt:P175 wd:${qid} .
  { ?album wdt:P31 wd:${STUDIO_ALBUM} } UNION { ?album wdt:P31 wd:${ALBUM} ; wdt:P7937 wd:${STUDIO_ALBUM} }
  FILTER NOT EXISTS { ?album wdt:${EDITION_OF} ?original }
  FILTER NOT EXISTS { ?album wdt:${EDITION_NAME} ?edition }
  OPTIONAL { ?album wdt:P577 ?date }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "${lang},en". }
} GROUP BY ?album ?albumLabel ORDER BY ?released`;
  const url = new URL(WIKIDATA_SPARQL);
  url.searchParams.set("query", sparql);
  url.searchParams.set("format", "json");
  return url.toString();
}

function label(entity, lang) {
  return entity?.labels?.[lang]?.value || entity?.labels?.en?.value || "";
}

function commonsImage(claims) {
  const file = claims?.[IMAGE]?.[0]?.mainsnak?.datavalue?.value;
  return typeof file === "string"
    ? `https://commons.wikimedia.org/wiki/Special:FilePath/${encodeURIComponent(file)}?width=240`
    : "";
}

/**
 * Look up the work with Wikidata id `id` for the box. Resolves to null when
 * Wikidata has no such item, and rejects when Wikidata cannot be reached.
 * The people and the synopsis are each optional, as a band's members and
 * history are: one failing leaves that part out.
 */
export async function lookupWork(id, appLang, { fetchJson = defaultFetchJson, signal } = {}) {
  if (!/^Q\d+$/.test(String(id || ""))) return null;
  const lang = wikiLanguage(appLang);
  const { signal: sig, done } = withTimeout(signal);
  const get = (url) => fetchJson(url, sig);
  try {
    const entities = (await get(query(WIKIDATA_API, {
      action: "wbgetentities", ids: id,
      props: "claims|sitelinks/urls|labels|descriptions",
      languages: lang === "en" ? "en" : `${lang}|en`, languagefallback: "1",
      sitefilter: lang === "en" ? "enwiki" : `${lang}wiki|enwiki`,
    })))?.entities;
    const work = entities?.[id];
    if (!work || work.missing !== undefined) return null;

    const site = work.sitelinks?.[`${lang}wiki`] || work.sitelinks?.enwiki || null;
    const siteLang = site?.site === `${lang}wiki` ? lang : "en";
    const facts = workFacts(work.claims);
    const peopleIds = [...new Set([...facts.composers, ...facts.lyricists, ...facts.bookWriters])];

    const [people, extractJson] = await Promise.all([
      peopleIds.length
        ? get(query(WIKIDATA_API, {
          action: "wbgetentities", ids: peopleIds.join("|"), props: "labels",
          languages: lang === "en" ? "en" : `${lang}|en`, languagefallback: "1",
        })).catch((err) => { console.warn("work credits lookup failed", err); return null; })
        : null,
      site
        ? get(query(`https://${siteLang}.wikipedia.org/w/api.php`, {
          action: "query", prop: "extracts", explaintext: "1", exsectionformat: "wiki",
          titles: site.title, formatversion: "2", redirects: "1",
        })).catch((err) => { console.warn("work synopsis lookup failed", err); return null; })
        : null,
    ]);
    const names = (ids) => ids.map((pid) => label(people?.entities?.[pid], lang)).filter(Boolean);

    return {
      id,
      name: label(work, lang),
      description: work.descriptions?.[lang]?.value || work.descriptions?.en?.value || "",
      year: facts.year,
      image: commonsImage(work.claims),
      synopsis: synopsisFromExtract(extractJson?.query?.pages?.[0]?.extract, siteLang),
      composers: names(facts.composers),
      lyricists: names(facts.lyricists),
      bookWriters: names(facts.bookWriters),
      links: officialLinks(work.claims),
      articleUrl: site?.url || "",
    };
  } finally {
    done();
  }
}

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
  const lang = wikiLanguage(appLang);
  const { signal: sig, done } = withTimeout(signal);
  const get = (url) => fetchJson(url, sig);

  try {
    let ids = [known];
    if (!known) {
      const found = await get(query(WIKIDATA_API, {
        action: "wbsearchentities", search, language: lang, uselang: lang,
        type: "item", limit: "10",
      }));
      ids = (found?.search || []).map((s) => s.id).filter((qid) => /^Q\d+$/.test(qid));
      if (!ids.length) return null;
    }

    const entities = (await get(query(WIKIDATA_API, {
      action: "wbgetentities", ids: ids.join("|"),
      props: "claims|sitelinks/urls|labels|descriptions",
      languages: lang === "en" ? "en" : `${lang}|en`, languagefallback: "1",
      sitefilter: lang === "en" ? "enwiki" : `${lang}wiki|enwiki`,
    })))?.entities;
    const artist = pickArtist(entities, ids);
    if (!artist) return null;

    const site = artist.sitelinks?.[`${lang}wiki`] || artist.sitelinks?.enwiki || null;
    const siteLang = site?.site === `${lang}wiki` ? lang : "en";
    const members = membersFromClaims(artist.claims);
    const memberIds = [...members.current, ...members.former];
    const imageFile = artist.claims?.[IMAGE]?.[0]?.mainsnak?.datavalue?.value;

    const [memberEntities, albumsJson, extractJson] = await Promise.all([
      memberIds.length
        ? get(query(WIKIDATA_API, {
          action: "wbgetentities", ids: memberIds.join("|"), props: "labels",
          languages: lang === "en" ? "en" : `${lang}|en`, languagefallback: "1",
        })).catch((err) => { console.warn("artist members lookup failed", err); return null; })
        : null,
      get(sparqlQuery(artist.id, lang))
        .catch((err) => { console.warn("artist albums lookup failed", err); return null; }),
      site
        ? get(query(`https://${siteLang}.wikipedia.org/w/api.php`, {
          action: "query", prop: "extracts", explaintext: "1", exsectionformat: "wiki",
          titles: site.title, formatversion: "2", redirects: "1",
        })).catch((err) => { console.warn("artist history lookup failed", err); return null; })
        : null,
    ]);

    const names = (list) => list
      .map((id) => label(memberEntities?.entities?.[id], lang))
      .filter(Boolean);

    return {
      id: artist.id,
      name: label(artist, lang) || search,
      // Kept beside the name in the app's language: LRCLIB lists artists by
      // the name they release under, which the English label nearly always is.
      englishName: artist.labels?.en?.value || "",
      description: artist.descriptions?.[lang]?.value || artist.descriptions?.en?.value || "",
      image: typeof imageFile === "string"
        ? `https://commons.wikimedia.org/wiki/Special:FilePath/${encodeURIComponent(imageFile)}?width=240`
        : "",
      history: historyFromExtract(extractJson?.query?.pages?.[0]?.extract, historyHeadingsFor(siteLang)),
      members: { current: names(members.current), former: names(members.former) },
      albums: albumsFromSparql(albumsJson),
      links: officialLinks(artist.claims),
      articleUrl: site?.url || "",
    };
  } finally {
    done();
  }
}
