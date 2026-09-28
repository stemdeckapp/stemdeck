// The artist box: the info button on the now-playing card opens it with the
// band's history, members and studio albums, from Wikidata and Wikipedia
// (#699), and "Save for this track" keeps the band on the track so the box
// opens straight on it next time.
//
// The lookup itself is artistLookup.js. This is the box: where the name comes
// from, what is shown while it loads or when it fails, and the markup. And the
// one lookup made without it, which saves the band from a file's artist tag
// when a track is opened (see "The band from the file's tags" below).
//
// The song leads the box whenever the track is known as one (identified by
// the server, or named by its tags): its title, who performs it, the album,
// and the show a soundtrack is from. A song from a musical or a film then
// shows that work (the server finds which, app/pipeline/work_lookup.py)
// before any performer, and the performer compactly, with their history one
// click away: a cast member's biography says less about the song than the
// show does. Any other band follows the song in full, as it always has.
//
// A band Wikipedia has no article on, or leaves gaps in, is filled from
// Discogs when the user has set a Discogs token: the server asks Discogs and
// answers GET /api/jobs/{id}/artist-extra (artistDiscogs.js), so the token
// never reaches the page. Wikipedia always comes first; what Discogs added is
// labelled, and credited as Discogs' terms ask.
//
// Everything that came from the network goes in with textContent, never as
// HTML. Wikipedia text is written by anyone, and this page can reach the
// desktop app's native commands.

import { t, getLanguage, onLanguageChange } from "./i18n.js";
import {
  artistMatchesName,
  languageTag,
  lookupArtist,
  lookupWork,
  nativeNameToShow,
  taggedArtistName,
  wikiLanguage,
} from "./artistLookup.js";
import { discogsExtraFromJson, needsDiscogs, withDiscogs } from "./artistDiscogs.js";
import {
  getCurrentTrackArtist,
  getCurrentTrackInfo,
  getCurrentTrackSong,
  getCurrentTrackWork,
  isCurrentTrackTagsPending,
  setCurrentTrackArtist,
} from "./catalog.js";

// One answer per language and name, for the life of the page. A band's
// members do not change between two clicks, and asking again costs the user a
// second of waiting and Wikimedia a request for nothing. Failures are not
// kept, so a retry after a dropped connection really does retry.
const answers = new Map();

let dialog = null;
let body = null;
let input = null;
let form = null;
let closeButton = null;
let current = null; // AbortController of the lookup in flight
let lastQuery = "";
let lastId = ""; // the saved band's Wikidata id, when the box opened on one
let returnFocus = null;
let searchShown = true;
let waiting = false; // open on a track whose tags or band are still being found
let searchGen = 0; // bumped by every search and by closing: a late answer checks it

const isOpen = () => dialog && !dialog.classList.contains("hidden");

// The search row is for when there is no band to show: nothing is known about
// the track, the name found nothing, or "Not this band? Search" asked for it.
// A band the track is known by opens straight on its details, since a field
// holding the answer reads as a question.
function showSearch(on, { focus = false } = {}) {
  searchShown = on;
  form?.classList.toggle("hidden", !on);
  if (on) body.querySelector(".artist-other")?.remove();
  if (on && focus) {
    input.focus();
    input.select();
  }
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

// Under the song, which is known before anything is looked up, so a wait or a
// failure still says what the track is.
function showStatus(message, kind = "") {
  body.replaceChildren(...songParts(), el("p", `artist-status${kind ? ` ${kind}` : ""}`, message));
}

function section(titleKey, ...children) {
  const box = el("section", "artist-section");
  box.append(el("h3", "artist-section-title", t(titleKey)), ...children);
  return box;
}

// A name in the artist's own language, after the name in the reader's: 周杰倫
// after "Jay Chou", 아이유 after "IU". Marked with its language, so the right
// font draws it (a Han character is drawn differently in Japanese and in each
// Chinese script) and a screen reader reads it in that language. Nothing when
// it is the same name.
function appendNative(node, name, native, lang) {
  const shown = nativeNameToShow(name, native);
  if (!shown) return node;
  const span = el("span", "artist-native", shown);
  if (lang) span.lang = languageTag(lang);
  span.dir = "auto";
  node.append(" ", span);
  return node;
}

// Members as chips, each with their own-language name when it differs.
function nameList(people, nativeLang = "") {
  const list = el("ul", "artist-names");
  for (const person of people) {
    const { name, native } = typeof person === "string" ? { name: person, native: "" } : person;
    list.append(appendNative(el("li", "", name), name, native, nativeLang));
  }
  return list;
}

// A Wikipedia edition's language, named in the reader's own: "Japanese",
// "japoński", "日语".
function languageName(code) {
  try {
    return new Intl.DisplayNames([getLanguage()], { type: "language" }).of(code) || code;
  } catch (err) {
    console.warn("language names unavailable", err);
    return code;
  }
}

// Prose read from Wikipedia: its paragraphs, marked with the language they
// are in, and when that is not the reader's own edition, a line saying which
// it is. A Chinese singer's history can be read from the Chinese Wikipedia
// under any language, since the English article may say two lines.
function articleProse(className, paragraphs, edition, variant) {
  const prose = el("div", className);
  if (edition) {
    prose.lang = languageTag(variant || edition);
    prose.dir = "auto";
  }
  for (const paragraph of paragraphs) prose.append(el("p", "", paragraph));
  if (!edition || edition === wikiLanguage(getLanguage())) return [prose];
  return [el("p", "artist-edition", t("artist.fromWikipedia", { language: languageName(edition) })), prose];
}

// "Save for this track", or "Saved for this track" once it is. Kept as the
// Wikidata id plus the name, so reopening asks for exactly this band in any
// language, and a different search on the same track can replace it.
function saveButton(artist) {
  const button = el("button", "artist-save");
  button.type = "button";
  const paint = () => {
    const saved = getCurrentTrackArtist()?.id === artist.id;
    button.textContent = t(saved ? "artist.savedForTrack" : "artist.setForTrack");
    button.disabled = saved;
    button.classList.toggle("saved", saved);
  };
  button.addEventListener("click", () => {
    if (setCurrentTrackArtist({ id: artist.id, name: artist.name, englishName: artist.englishName })) paint();
  });
  paint();
  return button;
}

// "From the musical", "From the film": the title of the work's section.
const WORK_TITLE_KEYS = {
  musical: "artist.work.musical",
  film: "artist.work.film",
  tv: "artist.work.tv",
  other: "artist.work.other",
};

// "From the musical Wicked", "From the film The Greatest Showman": the line
// under the song's title.
const SONG_FROM_KEYS = {
  musical: "artist.song.fromMusical",
  film: "artist.song.fromFilm",
  tv: "artist.song.fromTv",
  other: "artist.song.fromOther",
};

// The song the open track is, heading the box: its title, the show it is
// from, who performs it (the whole credit, not only the first name, which is
// the one looked up) and the album, with the year it came out when known.
// Nothing when the track is not known as a song, and the box is then exactly
// what it was before.
function songParts() {
  const song = getCurrentTrackSong();
  if (!song) return [];
  const work = getCurrentTrackWork();
  const head = el("header", "artist-song");
  head.append(el("h2", "artist-song-title", song.title));
  if (work) head.append(el("p", "artist-desc artist-song-from", t(SONG_FROM_KEYS[work.kind] || SONG_FROM_KEYS.other, { name: work.name })));
  // A cast recording's album is often just the show's name, said just above,
  // so it is left out then, unless it carries the year.
  const repeatsWork = work && song.album.toLowerCase() === work.name.toLowerCase();
  let album = repeatsWork && !song.year ? "" : song.album;
  if (album && song.year) album = t("artist.song.albumYear", { album, year: song.year });
  const facts = [
    ["artist.song.performedBy", song.credit],
    ["artist.song.album", album],
  ].filter(([, value]) => value);
  if (facts.length) {
    const list = el("dl", "artist-work-credits artist-song-facts");
    for (const [key, value] of facts) list.append(el("dt", "", t(key)), el("dd", "", value));
    head.append(list);
  }
  return [head];
}

// Labelled by what it opens, since a box with a show and a performer in it has
// one of these for each. target="_blank" is all it takes: main.js routes these
// through the desktop app's open_url, since the webview will not open one
// itself.
function readMoreLink(name, url, edition = "") {
  const link = el("a", "artist-more", t("artist.readMoreAbout", { name }));
  link.href = url;
  if (edition) link.hreflang = languageTag(edition);
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

// Official links, each an icon and a short label. Brand names are the same
// in every language; only "Website" is translated. The icons are drawn here,
// not fetched, and every path is a constant: nothing from Wikidata reaches
// them. Stroked in currentColor, so they follow the text in either theme.
const SVG_NS = "http://www.w3.org/2000/svg";
const LINK_ICONS = {
  website: [
    "M2 12a10 10 0 1 0 20 0a10 10 0 1 0-20 0",
    "M2 12h20",
    "M12 2a15.3 15.3 0 0 1 4 10a15.3 15.3 0 0 1-4 10a15.3 15.3 0 0 1-4-10a15.3 15.3 0 0 1 4-10z",
  ],
  instagram: [
    "M7 2h10a5 5 0 0 1 5 5v10a5 5 0 0 1-5 5H7a5 5 0 0 1-5-5V7a5 5 0 0 1 5-5z",
    "M16 11.37A4 4 0 1 1 12.63 8A4 4 0 0 1 16 11.37z",
    "M17.5 6.5h.01",
  ],
  spotify: [
    "M2 12a10 10 0 1 0 20 0a10 10 0 1 0-20 0",
    "M6.8 9.4c3.5-1.1 7.3-.8 10.4.9",
    "M7.5 12.6c2.9-.8 5.9-.5 8.4.8",
    "M8.2 15.6c2.2-.5 4.4-.3 6.3.6",
  ],
  appleMusic: [
    "M9 18V5l12-2v13",
    "M3 18a3 3 0 1 0 6 0a3 3 0 1 0-6 0",
    "M15 16a3 3 0 1 0 6 0a3 3 0 1 0-6 0",
  ],
  bandcamp: ["M2 18L8.5 6H22l-6.5 12z"],
  facebook: ["M18 2h-3a5 5 0 0 0-5 5v3H7v4h3v8h4v-8h3l1-4h-4V7a1 1 0 0 1 1-1h3z"],
  youtube: [
    "M22.5 6.4a2.8 2.8 0 0 0-1.9-2C18.9 4 12 4 12 4s-6.9 0-8.6.5a2.8 2.8 0 0 0-1.9 2A29 29 0 0 0 1 11.8a29 29 0 0 0 .5 5.3A2.8 2.8 0 0 0 3.4 19c1.7.5 8.6.5 8.6.5s6.9 0 8.6-.5a2.8 2.8 0 0 0 1.9-2 29 29 0 0 0 .5-5.3 29 29 0 0 0-.5-5.3z",
    "M9.8 15l5.7-3.2-5.7-3.3z",
  ],
};
const LINK_LABELS = {
  website: () => t("artist.links.website"),
  instagram: () => "Instagram",
  spotify: () => "Spotify",
  appleMusic: () => "Apple Music",
  bandcamp: () => "Bandcamp",
  facebook: () => "Facebook",
  youtube: () => "YouTube",
};

function linkIcon(kind) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "14");
  svg.setAttribute("height", "14");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  for (const d of LINK_ICONS[kind]) {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

// The row under a band's or a show's heading, or null when it has none, so
// no empty row is left behind. Opened outside the app as readMoreLink is.
// The URLs were checked and built in artistLookup.js; this only accepts
// http(s) again, so nothing else can ever become a link here.
function officialLinksRow(links, name) {
  const usable = (links || []).filter((link) => LINK_ICONS[link?.kind] && /^https?:\/\//i.test(link.url || ""));
  if (!usable.length) return null;
  const list = el("ul", "artist-links");
  list.setAttribute("aria-label", t("artist.links.aria", { name }));
  for (const { kind, url, fromDiscogs } of usable) {
    const link = el("a", `artist-link artist-link-${kind}`);
    link.setAttribute("href", url);
    // Beside a Wikidata band's own links, the ones only Discogs had say so.
    if (fromDiscogs) {
      link.classList.add("from-discogs");
      link.title = t("artist.fromDiscogs");
    }
    link.setAttribute("target", "_blank");
    link.setAttribute("rel", "noopener noreferrer");
    link.dataset.kind = kind;
    link.append(linkIcon(kind), el("span", "", LINK_LABELS[kind]()));
    const item = el("li");
    item.append(link);
    list.append(item);
  }
  return list;
}

// Names in the reader's language's own list style ("A, B and C").
function nameListText(names) {
  try {
    return new Intl.ListFormat(getLanguage(), { style: "long", type: "conjunction" }).format(names);
  } catch (err) {
    console.warn("list format unavailable", err);
    return names.join(", ");
  }
}

// The work a song is from: its name and year, picture, a short synopsis, who
// wrote the music, the lyrics and the book, and the way to its article.
function workSection(work, { lead = false } = {}) {
  const box = el("section", `artist-section artist-work${lead ? " lead" : ""}`);
  box.append(el("h3", "artist-section-title", t(WORK_TITLE_KEYS[work.kind] || WORK_TITLE_KEYS.other)));

  const head = el("div", "artist-head artist-work-head");
  if (work.image) {
    const img = el("img", "artist-photo artist-work-photo");
    img.alt = "";
    img.loading = "lazy";
    img.referrerPolicy = "no-referrer";
    img.addEventListener("error", () => img.remove(), { once: true });
    img.src = work.image;
    head.append(img);
  }
  const titles = el("div", "artist-titles");
  const name = el(lead ? "h2" : "h4", "artist-work-name", work.name);
  appendNative(name, work.name, work.nativeName, work.nativeLang);
  if (work.year) name.append(" ", el("span", "artist-work-year num", work.year));
  titles.append(name);
  if (work.description) titles.append(el("p", "artist-desc", work.description));
  head.append(titles);
  box.append(head);
  const links = officialLinksRow(work.links, work.name);
  if (links) box.append(links);

  if (work.synopsis?.length) {
    box.append(...articleProse("artist-history artist-work-synopsis", work.synopsis, work.synopsisLang, work.synopsisVariant));
  }

  const credits = [
    ["artist.work.music", work.composers],
    ["artist.work.lyrics", work.lyricists],
    ["artist.work.book", work.bookWriters],
  ].filter(([, names]) => names?.length);
  if (credits.length) {
    const list = el("dl", "artist-work-credits");
    for (const [key, names] of credits) list.append(el("dt", "", t(key)), el("dd", "", nameListText(names)));
    box.append(list);
  }

  if (work.articleUrl) {
    const more = el("p", "artist-work-more");
    more.append(readMoreLink(work.name, work.articleUrl, work.synopsisLang));
    box.append(more);
  }
  return box;
}

// A link-styled button that opens the search row: "Not this band? Search"
// under a band, or the search itself when there is no band to show.
function searchLink(key) {
  const other = el("p", "artist-other");
  const link = el("button", "artist-other-btn", t(key));
  link.type = "button";
  link.addEventListener("click", () => showSearch(true, { focus: true }));
  other.append(link);
  return other;
}

// The band's photo, name, one-line description and save button. `compact`
// is the performer under a show: a smaller heading, since the song and the
// show above it are what the box is about.
function bandHead(artist, { compact = false } = {}) {
  const head = el("header", `artist-head${compact ? " artist-performer-head" : ""}`);
  if (artist.image) {
    const img = el("img", "artist-photo");
    img.alt = "";
    img.loading = "lazy";
    img.referrerPolicy = "no-referrer";
    // A photo that fails to load leaves no broken-image box behind.
    img.addEventListener("error", () => img.remove(), { once: true });
    img.src = artist.image;
    head.append(img);
  }
  const titles = el("div", "artist-titles");
  titles.append(appendNative(el(compact ? "h4" : "h2", "artist-name", artist.name), artist.name, artist.nativeName, artist.nativeLang));
  if (artist.description) titles.append(el("p", "artist-desc", artist.description));
  else if (artist.realName) titles.append(el("p", "artist-desc", t("artist.realName", { name: artist.realName })));
  head.append(titles);
  // A band known only to Discogs has no Wikidata id to be saved by.
  if (artist.id) head.append(saveButton(artist));
  return head;
}

// The performer of a song from a show: name and description, and their
// history, members and albums behind "More about <name>", with the link to
// their article. Unless that article is the show's own, already linked above.
function performerSection(artist, work) {
  const box = el("section", "artist-section artist-performer");
  box.append(el("h3", "artist-section-title", t("artist.performer")), bandHead(artist, { compact: true }));
  const links = officialLinksRow(artist.links, artist.name);
  if (links) box.append(links);
  if (!searchShown) box.append(searchLink("artist.notThisPerformer"));
  const details = bandDetails(artist);
  const url = artist.articleUrl && artist.articleUrl !== work?.articleUrl ? artist.articleUrl : "";
  if (url) {
    const more = el("p", "artist-work-more");
    more.append(readMoreLink(artist.name, url, artist.historyLang));
    details.push(more);
  }
  if (!details.length) return box;
  const more = el("div", "artist-performer-more");
  more.hidden = true;
  more.tabIndex = -1;
  more.append(...details);
  const open = el("p", "artist-other artist-performer-open");
  const button = el("button", "artist-other-btn", t("artist.moreAbout", { name: artist.name }));
  button.type = "button";
  button.addEventListener("click", () => {
    more.hidden = false;
    open.remove();
    // Focus goes where the button was, so a keyboard user carries on reading.
    more.focus({ preventScroll: true });
  });
  open.append(button);
  box.append(open, more);
  return box;
}

// The song first when known, then the show it is from, then who performs it:
// compactly under a show, and in full, as the box has always shown a band,
// when there is no show. No band means the search for one is a click away,
// rather than an empty field in front of what is known.
function render(artist, work = null) {
  const parts = songParts();
  const song = parts.length > 0;
  const soundtrack = Boolean(work) || Boolean(getCurrentTrackSong()?.soundtrack);
  if (!artist) showSearch(false);
  const foot = el("footer", "artist-foot");

  if (work) parts.push(workSection(work, { lead: !song }));
  if (artist && soundtrack) {
    parts.push(performerSection(artist, work));
  } else if (artist) {
    const head = bandHead(artist);
    // Under the song, the band starts a section of its own.
    if (song) head.classList.add("artist-band-head");
    parts.push(head);
    const links = officialLinksRow(artist.links, artist.name);
    if (links) parts.push(links);
    // Under the name, where a wrong band is noticed: the way back to the field.
    if (!searchShown) parts.push(searchLink("artist.notThisBand"));
    parts.push(...bandDetails(artist));
    if (artist.articleUrl) foot.append(readMoreLink(artist.name, artist.articleUrl, artist.historyLang));
  } else {
    parts.push(searchLink("artist.searchPlaceholder"));
  }

  // The credit: Wikipedia and Wikidata for what came from them, and Discogs,
  // linked to the band's page there as its terms ask, for what it added.
  if (work || (artist && !artist.discogs?.only)) foot.append(el("span", "artist-source", t("artist.source")));
  if (artist?.discogs?.only) foot.append(el("span", "artist-source artist-source-discogs", t("artist.fromDiscogs")));
  if (artist?.discogs) foot.append(discogsCredit(artist.discogs.url));
  if (artist || work) parts.push(foot);
  body.replaceChildren(...parts);
  body.scrollTop = 0;
}

// "Data provided by Discogs", linked to the band's page there.
function discogsCredit(url) {
  const link = el("a", "artist-more artist-discogs-credit", t("artist.discogsCredit"));
  link.href = /^https:\/\/www\.discogs\.com\//.test(url || "") ? url : "https://www.discogs.com/";
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

// "From Discogs" under a section Discogs filled in beside a Wikipedia band.
function discogsNote(artist, key) {
  return artist.discogs?.filled?.includes(key) ? [el("p", "artist-edition artist-from-discogs", t("artist.fromDiscogs"))] : [];
}

// A band's history, members and studio albums, as sections.
function bandDetails(artist) {
  const parts = [];
  if (artist.history.length) {
    parts.push(section("artist.history", ...discogsNote(artist, "history"), ...articleProse("artist-history", artist.history, artist.historyLang, artist.historyVariant)));
  }

  const { current: now, former } = artist.members;
  if (now.length || former.length) {
    const children = [];
    if (now.length) children.push(nameList(now, artist.nativeLang));
    if (former.length) {
      children.push(el("h4", "artist-subtitle", t("artist.formerMembers")), nameList(former, artist.nativeLang));
    }
    parts.push(section("artist.members", ...discogsNote(artist, "members"), ...children));
  }

  // The bands a person is in, as Discogs lists them.
  if (artist.groups?.length) parts.push(section("artist.memberOf", nameList(artist.groups)));

  if (artist.albums.length) {
    const list = el("ol", "artist-albums");
    for (const album of artist.albums) {
      const row = el("li");
      row.append(el("span", "artist-album-year num", album.year), el("span", "artist-album-title", album.title));
      list.append(row);
    }
    // "Albums" rather than "Studio albums" when the list had to take plain
    // albums as well (artistLookup.js albumList), and "Releases" for Discogs'
    // list, which is every release under the band's name.
    const title = artist.albumsStudioOnly === null ? "artist.releases"
      : artist.albumsStudioOnly === false ? "artist.albumsAll" : "artist.albums";
    parts.push(section(title, ...discogsNote(artist, "albums"), list));
  }
  return parts;
}

// The work the open track's song is from, looked up by its id for the box, or
// null when the track has none. One that cannot be reached is shown by the
// name kept on the track, and asked for again next time.
async function loadWork(work, lang, signal) {
  if (!work) return null;
  const key = `${lang}|work#${work.id}`;
  if (answers.has(key)) return answers.get(key);
  try {
    const found = await lookupWork(work.id, lang, { signal });
    const value = found ? { ...found, kind: work.kind, name: found.name || work.name } : null;
    answers.set(key, value);
    return value;
  } catch (err) {
    if (!signal.aborted) console.warn("work lookup failed", err);
    return { id: work.id, kind: work.kind, name: work.name, synopsis: [] };
  }
}

// The band by id or name, from the answers already had or from Wikidata.
async function bandAnswer(query, id, lang, signal) {
  const key = id ? `${lang}|#${id}` : `${lang}|${query.toLowerCase()}`;
  if (answers.has(key)) return answers.get(key);
  const artist = await lookupArtist(query, lang, { id, signal });
  if (signal.aborted) return null;
  answers.set(key, artist);
  // Also by id, so a band found by name and then saved opens from the cache.
  if (artist) answers.set(`${lang}|#${artist.id}`, artist);
  return artist;
}

// The Discogs profile of the open track's band, from the server, or null:
// no token set (404 at once, and nothing is sent to Discogs), no band it was
// sure of, or no connection. Only an answer is kept, so setting a token in
// Settings takes effect the next time the box opens.
async function loadExtra(trackId, signal) {
  if (!trackId) return null;
  const key = `extra#${trackId}`;
  if (answers.has(key)) return answers.get(key);
  try {
    const res = await fetch(`/api/jobs/${encodeURIComponent(trackId)}/artist-extra`, { signal, headers: { Accept: "application/json" } });
    if (!res.ok) return null;
    const extra = discogsExtraFromJson(await res.json());
    if (extra) answers.set(key, extra);
    return extra;
  } catch (err) {
    if (!signal.aborted) console.warn("artist details from Discogs failed", err);
    return null;
  }
}

// `typed` is a name searched for in the box. Found nothing, it says so, where
// the name the box opened with instead gives way to the track's work.
//
// The box's own band (not a typed one) is asked of Discogs only when
// Wikipedia leaves a gap (artistDiscogs.js needsDiscogs): Wikipedia's answer
// is drawn as soon as it is in, and redrawn with the gaps filled when
// Discogs' arrives. With no Wikidata band at all, the box waits for Discogs
// before it says nothing was found.
async function search(name, { id = "", typed = false } = {}) {
  const query = String(name || "").trim();
  lastQuery = query;
  lastId = id;
  current?.abort();
  current = null;
  const gen = ++searchGen;
  const trackWork = getCurrentTrackWork();
  const trackId = getCurrentTrackInfo()?.id || "";
  const extraController = new AbortController();
  let extraAsked = null;
  const extraFor = () => (extraAsked ??= typed ? Promise.resolve(null) : loadExtra(trackId, extraController.signal));
  const stillShown = () => gen === searchGen && isOpen();
  if (!query && !id && !trackWork) {
    // A song known by no band: the song, with the search a click away, and
    // the band from Discogs when it knows the track's.
    if (!typed && getCurrentTrackSong()) {
      render(null);
      const extra = await extraFor();
      if (extra && stillShown()) render(withDiscogs(null, extra));
    } else {
      extraController.abort();
      // Nothing asked yet: the field's placeholder already says what to do.
      body.replaceChildren(...songParts());
    }
    return;
  }

  const lang = getLanguage();
  // Nothing to show means the field is the next step, filled with the name
  // that found nothing so it can be corrected or tried again.
  const notFound = () => {
    showSearch(true);
    showStatus(t("artist.notFound", { name: query }), "muted");
  };

  const controller = new AbortController();
  current = controller;
  controller.signal.addEventListener("abort", () => extraController.abort(), { once: true });
  const bandKey = id ? `${lang}|#${id}` : `${lang}|${query.toLowerCase()}`;
  const waitsForBand = (query || id) && !answers.has(bandKey);
  const waitsForWork = trackWork && !answers.has(`${lang}|work#${trackWork.id}`);
  if (waitsForBand || waitsForWork) {
    showStatus(t("artist.loading", { name: query || trackWork.name }), "loading");
  }
  try {
    // A band that cannot be reached must not take the work down with it: the
    // work is shown on its own and the band is asked for again next time.
    let bandError = null;
    const [artist, work] = await Promise.all([
      query || id
        ? bandAnswer(query, id, lang, controller.signal).catch((err) => {
            bandError = err;
            return null;
          })
        : null,
      loadWork(trackWork, lang, controller.signal),
    ]);
    // Only a newer search or closing the box aborts this controller; a
    // request that times out aborts its own inner signal (artistLookup.js
    // withTimeout) and arrives here as bandError, or in the catch below.
    if (controller.signal.aborted) return;
    if (!artist && !work && !typed) {
      // No band on Wikidata, or none reachable: Discogs may know it.
      const extra = await extraFor();
      if (controller.signal.aborted) return;
      if (extra) {
        showSearch(false);
        render(withDiscogs(null, extra));
        return;
      }
    }
    if (bandError && !(work && !typed)) throw bandError;
    if (artist) render(artist, work);
    else if (work && !typed) render(null, work);
    else notFound();
    if (typed || !(artist || work) || !needsDiscogs(artist)) return;
    // Drawn already; filled from Discogs when its answer is in, if it has
    // what Wikipedia lacks.
    extraFor().then((extra) => {
      if (!extra || !stillShown() || controller.signal.aborted) return;
      const band = withDiscogs(artist, extra);
      if (band !== artist) render(band, work);
    });
  } catch (err) {
    // A newer search or closing the box aborts this one; that is not a fault.
    if (controller.signal.aborted) return;
    console.warn("artist lookup failed", err);
    showSearch(true);
    showStatus(t("artist.offline"), "error");
  } finally {
    if (current === controller) current = null;
  }
}

// Opens on the band saved on the track if there is one, looked up by its id;
// then on the artist the file was tagged with, which is the file's own word
// rather than a guess; otherwise on an empty field. It used to guess from the
// title, the part before " - ", but titles name the song as often as the band
// ("Metropolis - Part I" guessed "Metropolis"), and a wrong answer shown with
// confidence is worse than a question.
//
// While the track's tags are still being read, or the band is being found
// from them, it says so and waits: an empty field there would ask for what is
// about to arrive. showFromTrack() runs again when the answer lands.
function showFromTrack({ focus = true } = {}) {
  const info = getCurrentTrackInfo();
  const saved = getCurrentTrackArtist();
  waiting = !saved && Boolean(isCurrentTrackTagsPending() || (autoController && autoTrackId === info?.id));
  if (waiting) {
    current?.abort();
    current = null;
    lastQuery = "";
    lastId = "";
    input.value = "";
    showSearch(false);
    showStatus(t("artist.checkingTrack"), "loading");
    // Reading a video's tags can take the server most of a minute, so the
    // field stays one click away rather than out of reach until then.
    const other = el("p", "artist-other");
    const link = el("button", "artist-other-btn", t("artist.searchInstead"));
    link.type = "button";
    link.addEventListener("click", () => {
      waiting = false;
      body.replaceChildren(...songParts());
      showSearch(true, { focus: true });
    });
    other.append(link);
    body.append(other);
    if (focus) closeButton?.focus();
    return;
  }
  const name = saved?.name || taggedArtistName(info?.audioTags?.artist);
  // A work or a song to show is an answer too, so the field stays out of its
  // way.
  const answer = Boolean(name || getCurrentTrackWork() || getCurrentTrackSong());
  input.value = name;
  showSearch(!answer);
  // With a name, the answer is what they came for, so focus stays off the
  // field and Escape closes. Without one, the field is the next step.
  if (focus) (answer ? closeButton : input)?.focus();
  search(name, { id: saved?.id || "" });
}

function open() {
  returnFocus = document.activeElement;
  dialog.classList.remove("hidden");
  showFromTrack();
}

// The wait is over. Focus moves only if it is still where the wait put it.
function settle() {
  if (!isOpen() || !waiting) return;
  const active = document.activeElement;
  showFromTrack({ focus: active === closeButton || !dialog.contains(active) });
}

function close() {
  current?.abort();
  current = null;
  searchGen += 1;
  waiting = false;
  dialog.classList.add("hidden");
  returnFocus?.focus?.();
  returnFocus = null;
}

// ─── The band from the file's tags ───
//
// A track whose file was tagged with an artist gets its band saved without a
// click: looked up once when the track is opened, and kept only when the answer
// carries that same name. The box, the Lyrics tab and the now-playing card then
// all have it.
//
// Once per track per page load. Nothing found, a different name or no
// connection all leave the track as it was, and the next session asks again.
// The wait first means clicking down the library asks for nothing until a
// track is settled on, and the request never competes with that track's load.
//
// A track from before tags were read gets them from catalog.js a moment after
// it opens, announced as "tracktags", and the lookup runs then instead.
const AUTO_DELAY_MS = 800;
const autoTried = new Set();
let autoTimer = 0;
let autoController = null;
let autoTrackId = null; // the track autoController is finding the band for

function findBandFromTags(trackId) {
  clearTimeout(autoTimer);
  autoController?.abort();
  autoController = null;
  autoTimer = setTimeout(() => runFindBandFromTags(trackId), AUTO_DELAY_MS);
}

async function runFindBandFromTags(trackId) {
  const info = getCurrentTrackInfo();
  // The box open means the user is choosing; what they pick is the answer.
  // Unless it is open waiting for exactly this.
  if (info?.id !== trackId || autoTried.has(trackId) || getCurrentTrackArtist() || (isOpen() && !waiting)) return;
  const name = taggedArtistName(info.audioTags?.artist);
  if (!name) return;
  autoTried.add(trackId);
  const controller = new AbortController();
  autoController = controller;
  autoTrackId = trackId;
  const lang = getLanguage();
  const key = `${lang}|${name.toLowerCase()}`;
  try {
    // The box may have asked for this name already.
    const artist = answers.has(key) ? answers.get(key) : await lookupArtist(name, lang, { signal: controller.signal });
    // Kept for the box either way, under the keys search() looks for.
    answers.set(key, artist);
    if (artist) answers.set(`${lang}|#${artist.id}`, artist);
    if (!artist || !artistMatchesName(artist, name)) return;
    // Still the open track, and nobody saved a band on it in the meantime.
    if (getCurrentTrackInfo()?.id !== trackId || getCurrentTrackArtist()) return;
    setCurrentTrackArtist({ id: artist.id, name: artist.name, englishName: artist.englishName });
  } catch (err) {
    // Aborted because another track was opened: not an attempt, so opening
    // this one again tries again.
    if (controller.signal.aborted) {
      autoTried.delete(trackId);
      return;
    }
    console.warn("artist lookup from tags failed", err);
  } finally {
    if (autoController === controller) autoController = null;
    // The box, if it was waiting on this, now shows what was found.
    settle();
  }
}

// An older track's tags have been read (or found missing). For the open
// track, the band is looked up from them straight away: it was settled on long
// enough ago for the server to answer, so there is nothing left to wait for.
function onTrackTags(trackId) {
  if (!trackId || getCurrentTrackInfo()?.id !== trackId) return;
  clearTimeout(autoTimer);
  runFindBandFromTags(trackId);
  settle();
}

export function initArtistInfo() {
  dialog = document.getElementById("artistDialog");
  body = document.getElementById("artistBody");
  input = document.getElementById("artistQuery");
  form = document.getElementById("artistSearch");
  closeButton = document.getElementById("artistClose");
  const button = document.getElementById("np-details-btn");
  if (!dialog || !body || !input || !button) return;

  button.addEventListener("click", () => {
    // Nothing to look up before a track is open: the card then says "Ready
    // to import a track", which is not a band. CSS mutes the button too.
    if (document.querySelector(".app")?.classList.contains("no-track")) return;
    open();
  });

  document.getElementById("artistSearch")?.addEventListener("submit", (e) => {
    e.preventDefault();
    // What was typed, by name: the saved band's id only applies to the name
    // the box opened with.
    search(input.value, { typed: true });
  });
  document.getElementById("artistClose")?.addEventListener("click", close);
  dialog.addEventListener("mousedown", (e) => { if (e.target === dialog) close(); });
  dialog.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });

  // Section titles and messages are written by this module, not by
  // applyTranslations, so a language change while the box is open asks again
  // in the new language, which also brings the article in that language.
  onLanguageChange(() => {
    if (!isOpen()) return;
    if (waiting) showFromTrack({ focus: false });
    else search(lastQuery, { id: lastId });
  });

  // catalog.js says when a track has been opened, after its load is under way,
  // and when an older track's tags have been read.
  document.addEventListener("trackopen", (e) => {
    findBandFromTags(e.detail?.id ?? null);
    settle();
  });
  document.addEventListener("tracktags", (e) => onTrackTags(e.detail?.id ?? null));
}
