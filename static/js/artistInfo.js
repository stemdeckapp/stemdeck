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
// Everything that came from the network goes in with textContent, never as
// HTML. Wikipedia text is written by anyone, and this page can reach the
// desktop app's native commands.

import { t, getLanguage, onLanguageChange } from "./i18n.js";
import { artistMatchesName, lookupArtist, taggedArtistName } from "./artistLookup.js";
import {
  getCurrentTrackArtist,
  getCurrentTrackInfo,
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

function showStatus(message, kind = "") {
  body.replaceChildren(el("p", `artist-status${kind ? ` ${kind}` : ""}`, message));
}

function section(titleKey, ...children) {
  const box = el("section", "artist-section");
  box.append(el("h3", "artist-section-title", t(titleKey)), ...children);
  return box;
}

function nameList(names) {
  const list = el("ul", "artist-names");
  for (const name of names) list.append(el("li", "", name));
  return list;
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

function render(artist) {
  const head = el("header", "artist-head");
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
  titles.append(el("h2", "artist-name", artist.name));
  if (artist.description) titles.append(el("p", "artist-desc", artist.description));
  head.append(titles, saveButton(artist));

  const parts = [head];

  // Under the name, where a wrong band is noticed: the way back to the field.
  if (!searchShown) {
    const other = el("p", "artist-other");
    const link = el("button", "artist-other-btn", t("artist.notThisBand"));
    link.type = "button";
    link.addEventListener("click", () => showSearch(true, { focus: true }));
    other.append(link);
    parts.push(other);
  }

  if (artist.history.length) {
    const prose = el("div", "artist-history");
    for (const paragraph of artist.history) prose.append(el("p", "", paragraph));
    parts.push(section("artist.history", prose));
  }

  const { current: now, former } = artist.members;
  if (now.length || former.length) {
    const children = [];
    if (now.length) children.push(nameList(now));
    if (former.length) {
      children.push(el("h4", "artist-subtitle", t("artist.formerMembers")), nameList(former));
    }
    parts.push(section("artist.members", ...children));
  }

  if (artist.albums.length) {
    const list = el("ol", "artist-albums");
    for (const album of artist.albums) {
      const row = el("li");
      row.append(el("span", "artist-album-year num", album.year), el("span", "artist-album-title", album.title));
      list.append(row);
    }
    parts.push(section("artist.albums", list));
  }

  const foot = el("footer", "artist-foot");
  if (artist.articleUrl) {
    // target="_blank" is all it takes: main.js routes these through the
    // desktop app's open_url, since the webview will not open one itself.
    const link = el("a", "artist-more", t("artist.readMore"));
    link.href = artist.articleUrl;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    foot.append(link);
  }
  foot.append(el("span", "artist-source", t("artist.source")));
  parts.push(foot);

  body.replaceChildren(...parts);
  body.scrollTop = 0;
}

async function search(name, { id = "" } = {}) {
  const query = String(name || "").trim();
  lastQuery = query;
  lastId = id;
  current?.abort();
  current = null;
  if (!query && !id) {
    // Nothing asked yet: the field's placeholder already says what to do.
    body.replaceChildren();
    return;
  }

  const lang = getLanguage();
  const key = id ? `${lang}|#${id}` : `${lang}|${query.toLowerCase()}`;
  // Nothing to show means the field is the next step, filled with the name
  // that found nothing so it can be corrected or tried again.
  const notFound = () => {
    showSearch(true);
    showStatus(t("artist.notFound", { name: query }), "muted");
  };
  if (answers.has(key)) {
    const cached = answers.get(key);
    if (cached) render(cached);
    else notFound();
    return;
  }

  const controller = new AbortController();
  current = controller;
  showStatus(t("artist.loading", { name: query }), "loading");
  try {
    const artist = await lookupArtist(query, lang, { id, signal: controller.signal });
    if (controller.signal.aborted) return;
    answers.set(key, artist);
    // Also by id, so a band found by name and then saved opens from the cache.
    if (artist) answers.set(`${lang}|#${artist.id}`, artist);
    if (artist) render(artist);
    else notFound();
  } catch (err) {
    // A newer search or closing the box aborts this one; that is not a fault.
    if (controller.signal.aborted && controller.signal.reason?.message !== "timeout") return;
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
      body.replaceChildren();
      showSearch(true, { focus: true });
    });
    other.append(link);
    body.append(other);
    if (focus) closeButton?.focus();
    return;
  }
  const name = saved?.name || taggedArtistName(info?.audioTags?.artist);
  input.value = name;
  showSearch(!name);
  // With a name, the answer is what they came for, so focus stays off the
  // field and Escape closes. Without one, the field is the next step.
  if (focus) (name ? closeButton : input)?.focus();
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
    search(input.value);
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
