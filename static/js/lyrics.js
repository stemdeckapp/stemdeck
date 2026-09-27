// The Lyrics tab: a view of the sidebar where the open track's lyrics are
// found, kept, and shown in time with playback, karaoke style (#699).
//
// Where they come from, in order:
//   0. what the user chose for the track, kept here; else what the server
//      found while the track was separated (GET /api/jobs/{id}/lyrics: the
//      file's own, LRCLIB's, or a transcription), shown but not kept here, so
//      a better answer from the server later still shows. Failing both:
//   1. lyrics the file itself carried (an upload's embedded lyrics tag);
//   2. LRCLIB (lyricsLookup.js), looked up by itself when the tab opens, by
//      the artist and song the file was tagged with, or failing that the band
//      saved from the artist box and the song's name cleaned out of the
//      title. Kept straight away only when a version is the same length as
//      the track; otherwise the versions are offered to pick from.
// There is no search box: what is looked up is what the track says it is.
// What was kept belongs to the track and comes back whenever it is open.
//
// Synced lyrics follow playback: the line being sung fills with colour word by
// word, the next line is raised a step, the view keeps the line in the middle,
// and clicking a line moves the playhead there. Plain lyrics are shown as text.
//
// Everything that came from the network or a file's tags goes in with
// textContent, never as HTML: anyone can edit LRCLIB or a tag, and this page
// can reach the desktop app's native commands.

import { t } from "./i18n.js";
import { storeGet, storeSet } from "./utils.js";
import { getCurrentTrackInfo, getCurrentTrackArtist } from "./catalog.js";
import { transport, setPlayheadTime } from "./transport.js";
import {
  searchLyrics,
  rankVersions,
  otherNames,
  parseLrc,
  currentLineIndex,
  wordTimings,
  songFromTitle,
  fromServerLyrics,
  belongsTo,
} from "./lyricsLookup.js";

// One store entry per track rather than a field in the library store: a song's
// lyrics run to a few kilobytes, and the library is rewritten whole on every
// change to any track.
const storeKey = (trackId) => `stemdeck.lyrics.${trackId}`;

// Within this many seconds of the track's length, an automatic search keeps a
// version without asking. LRCLIB lengths come from real releases, so the same
// recording lands within a second or two; a live cut or a radio edit does not.
const SAME_RECORDING_SEC = 3;

const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

let statusEl = null;
let versionsEl = null;
let bodyEl = null;

let visible = false;
let shownTrackId = null; // the track whose lyrics are on screen
let lines = []; // parsed synced lines of what is shown, [] for plain text
let lineEls = [];
let lineWords = []; // per line: [{ el, start, end }] for the wipe
let currentIndex = -1;
let frame = 0;
let searchController = null;
let loadToken = 0; // guards against a slow store read landing after a switch
// The track the tab said "nothing known" for, so tags read for it after it
// was opened (catalog.js, "tracktags") can be looked up without a reopen.
let nothingKnownFor = null;
// Per track, a lookup this session that kept nothing: { song, matches }, the
// versions offered or none. Opening the tab again shows it rather than asking
// LRCLIB the same question.
const answered = new Map();
// The vocals stem's level over time for one track, { hop, db }, which times
// the wipe to the singing (lyricsLookup.js wordTimings). Only the open
// track's is kept: it is asked for again, cheaply, when a track comes back.
let envelope = null;
let envelopeFor = null;
let envelopeAsked = null; // the track whose envelope is being fetched

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function setStatus(message = "", kind = "") {
  statusEl.textContent = message;
  statusEl.className = `lyrics-status${kind ? ` ${kind}` : ""}`;
}

function fmtLength(seconds) {
  const s = Math.round(Number(seconds) || 0);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function clearLyrics() {
  lines = [];
  lineEls = [];
  lineWords = [];
  currentIndex = -1;
  bodyEl.replaceChildren();
  bodyEl.classList.remove("synced");
  versionsEl.replaceChildren();
}

// The language a line's script says it is in, for its lang attribute: Korean
// then wraps between words rather than inside one (word-break: keep-all in
// daw.css), and the browser draws Han characters in the Japanese or Chinese
// forms the line is written in, rather than whichever its font prefers.
const KANA = /[\p{Script=Hiragana}\p{Script=Katakana}]/u;
const HANGUL = /\p{Script=Hangul}/u;
const HAN = /\p{Script=Han}/u;

function scriptLang(text) {
  if (KANA.test(text)) return "ja";
  if (HANGUL.test(text)) return "ko";
  if (HAN.test(text)) return "zh";
  return "";
}

function tagLang(node, text) {
  const lang = scriptLang(text);
  if (lang) node.lang = lang;
  return node;
}

/** A synced line as a button of word spans, which the wipe fills in turn. */
function lineButton(line, nextTime) {
  const row = tagLang(el("button", "lyrics-line"), line.text);
  row.type = "button";
  row.title = t("lyrics.seekTitle");
  row.addEventListener("click", (e) => {
    setPlayheadTime(line.time);
    // A mouse click lets go of the line, so Space goes back to play and pause
    // rather than pressing the line again, and no focus box stays on it. A
    // key press (detail 0) keeps focus there for whoever is using the keys.
    if (e.detail > 0) row.blur();
  });
  const words = [];
  for (const word of wordTimings(line, nextTime, envelopeFor === shownTrackId ? envelope : null)) {
    const span = el("span", "lw", word.text);
    row.append(span);
    words.push({ el: span, start: word.start, end: word.end });
  }
  // An empty stamp marks an instrumental gap in LRC; a note keeps its place.
  if (!words.length) row.textContent = "♪";
  lineWords.push(words);
  return row;
}

/** Draw what is kept for the track: `entry` as saved, `others` to offer. */
function show(entry, others = []) {
  clearLyrics();
  if (!entry) return;

  if (entry.instrumental && !entry.synced && !entry.plain) {
    setStatus(t("lyrics.instrumental"), "muted");
  } else {
    setStatus("");
  }

  const head = el("div", "lyrics-match");
  head.append(
    el("span", "lyrics-match-title", entry.track),
    el("span", "lyrics-match-meta", [
      entry.artist,
      entry.album,
      entry.source === "file" ? t("lyrics.fromFile") : "",
      entry.source === "whisper" ? t("lyrics.transcribed") : "",
    ].filter(Boolean).join(" · ")),
  );
  const tools = el("div", "lyrics-tools");
  versionsEl.append(head, tools);
  if (others.length) {
    const toggle = el("button", "lyrics-link", t("lyrics.otherVersions", { count: others.length }));
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", "false");
    const list = versionList(others, [entry, ...others]);
    list.hidden = true;
    toggle.addEventListener("click", () => {
      list.hidden = !list.hidden;
      toggle.setAttribute("aria-expanded", String(!list.hidden));
      toggle.textContent = t(list.hidden ? "lyrics.otherVersions" : "lyrics.hideVersions", { count: others.length });
    });
    tools.append(toggle);
    versionsEl.append(list);
  }
  const remove = el("button", "lyrics-link", t("lyrics.remove"));
  remove.type = "button";
  remove.addEventListener("click", () => removeLyrics());
  tools.append(remove);

  if (entry.synced) {
    lines = parseLrc(entry.synced);
    lineEls = lines.map((line, i) => lineButton(line, lines[i + 1]?.time ?? null));
    bodyEl.append(...lineEls);
    bodyEl.classList.add("synced");
    if (lines.length) loadEnvelope(shownTrackId);
  } else if (entry.plain) {
    // Composed, as parseLrc does for synced lines.
    for (const text of entry.plain.normalize("NFC").split(/\r?\n/)) bodyEl.append(tagLang(el("p", "lyrics-text", text || " "), text));
  }
  bodyEl.scrollTop = 0;
}

/**
 * Fetch the vocals stem's level for `trackId`, once, and re-time the lines on
 * screen by it. Until it arrives, and for a track with no vocals stem (a 404),
 * the wipe keeps the estimate from the time between lines.
 */
async function loadEnvelope(trackId) {
  if (!trackId || envelopeFor === trackId || envelopeAsked === trackId) return;
  envelopeAsked = trackId;
  try {
    const res = await fetch(`/api/jobs/${encodeURIComponent(trackId)}/vocal-envelope`);
    if (!res.ok) return;
    const data = await res.json();
    if (!(Number(data?.hop) > 0) || !Array.isArray(data?.db)) return;
    envelope = { hop: Number(data.hop), db: Int8Array.from(data.db) };
    envelopeFor = trackId;
    if (trackId === shownTrackId) retime();
  } catch (err) {
    console.warn("vocal envelope fetch failed", err);
  } finally {
    if (envelopeAsked === trackId) envelopeAsked = null;
  }
}

/** Give the words on screen the envelope's timings: once, not per frame. */
function retime() {
  lines.forEach((line, i) => {
    const words = lineWords[i];
    const timed = wordTimings(line, lines[i + 1]?.time ?? null, envelope);
    if (!words || timed.length !== words.length) return;
    timed.forEach((word, k) => {
      words[k].start = word.start;
      words[k].end = word.end;
    });
  });
}

function versionList(matches, all) {
  const list = el("ul", "lyrics-version-list");
  for (const match of matches) {
    const item = el("li");
    const pick = el("button", "lyrics-version");
    pick.type = "button";
    pick.append(
      el("span", "lyrics-version-title", match.track),
      el("span", "lyrics-version-meta", [
        match.artist,
        match.album,
        match.duration ? fmtLength(match.duration) : "",
        t(match.synced ? "lyrics.synced" : "lyrics.plain"),
      ].filter(Boolean).join(" · ")),
    );
    pick.addEventListener("click", () => keep(match, all.filter((m) => m.id !== match.id)));
    item.append(pick);
    list.append(item);
  }
  return list;
}

/** Offer `matches` to choose from without keeping any: LRCLIB has this song,
 * but not a version the length of this track. */
function offer(matches) {
  clearLyrics();
  setStatus(t("lyrics.pickVersion"), "muted");
  versionsEl.append(versionList(matches, matches));
}

/** Save `match` on the open track and show it, with `others` still offered. */
async function keep(match, others) {
  const info = getCurrentTrackInfo();
  if (!info) return;
  const entry = { v: 1, source: "lrclib", ...match, savedAt: Date.now() };
  await storeSet(storeKey(info.id), { entry, others });
  answered.delete(info.id);
  if (getCurrentTrackInfo()?.id !== info.id) return;
  show(entry, others);
}

// Removing is remembered, not just a cleared entry: the lookup runs by itself,
// and would otherwise bring the same lyrics straight back the next time the
// tab opened. "Look up again" is the way back.
async function removeLyrics() {
  const info = getCurrentTrackInfo();
  if (!info) return;
  await storeSet(storeKey(info.id), { dismissed: true });
  answered.delete(info.id);
  showRemoved();
}

function showRemoved() {
  clearLyrics();
  setStatus(t("lyrics.removed"), "muted");
  versionsEl.append(lookAgainButton());
}

/** "Look up again": forget what is kept for the open track and find it anew. */
function lookAgainButton() {
  const again = el("button", "lyrics-link", t("lyrics.lookAgain"));
  again.type = "button";
  again.addEventListener("click", async () => {
    const info = getCurrentTrackInfo();
    if (!info) return;
    await storeSet(storeKey(info.id), null);
    loadForCurrentTrack();
  });
  return again;
}

// How many of the artist's other names are searched by when its own finds no
// version the track's length.
const OTHER_NAME_SEARCHES = 2;

const sameLength = (match, info) => Boolean(match) && info.duration > 0 && match.duration > 0
  && Math.abs(match.duration - info.duration) <= SAME_RECORDING_SEC;

/**
 * Look the open track up on LRCLIB by what it is known to be. Only versions of
 * this song by this artist count (belongsTo): LRCLIB's search also answers with
 * other artists' songs, and no lyrics beat another song's. `names` are the
 * other names the artist goes by (the band's, in its own script and in
 * English), which count as its own, and which are searched by too when the
 * artist's name finds no version the track's length: LRCLIB files a song under
 * whichever name its uploader wrote, 周杰倫 or Jay Chou. One the same length as
 * the track is kept straight away; otherwise they are offered to pick from;
 * with none, it says so. What it found is remembered for the session, so
 * opening the tab again does not ask again. A failed connection is not, so the
 * next opening tries again.
 */
async function lookUp(info, { artist, song, names = [] }) {
  searchController?.abort();
  const controller = new AbortController();
  searchController = controller;
  setStatus(t("lyrics.loading", { song }), "loading");
  try {
    const known = { artist, song, names };
    const search = async (by) => (await searchLyrics({ artist: by, song, duration: info.duration }, { signal: controller.signal }))
      .filter((m) => belongsTo(m, known));
    let matches = await search(artist);
    for (const other of otherNames(artist, names).slice(0, OTHER_NAME_SEARCHES)) {
      if (sameLength(matches[0], info) || controller.signal.aborted) break;
      // A failure here keeps what the artist's own name found.
      const more = await search(other).catch((err) => {
        console.warn("lyrics lookup by another name failed", err);
        return [];
      });
      matches = rankVersions([...matches, ...more], info.duration);
    }
    if (controller.signal.aborted || getCurrentTrackInfo()?.id !== info.id) return;
    const best = matches[0];
    if (sameLength(best, info)) {
      await keep(best, matches.slice(1, 12));
      return;
    }
    answered.set(info.id, { song, matches: matches.slice(0, 12) });
    showAnswer(answered.get(info.id));
  } catch (err) {
    // Aborted only by another track being opened; a timeout aborts the
    // lookup's own inner signal and lands below, as a failure.
    if (controller.signal.aborted) return;
    console.warn("lyrics lookup failed", err);
    setStatus(t("lyrics.offline"), "error");
    // There is no search box to press again, so the retry is offered here.
    versionsEl.replaceChildren(lookAgainButton());
  } finally {
    if (searchController === controller) searchController = null;
  }
}

/** A lookup's answer that kept nothing: versions to pick from, or none. */
function showAnswer({ song, matches }) {
  if (matches.length) offer(matches);
  else setStatus(t("lyrics.notFound", { song }), "muted");
}

/**
 * The lyrics the server found for a track (lyrics.json), as { entry, others }
 * in the shape this tab keeps; entry null when it kept none but has LRCLIB's
 * versions to offer, which its 404 carries. Null when it has neither or cannot
 * be reached, and the tab then looks for them itself.
 */
async function serverLyrics(trackId) {
  try {
    const res = await fetch(`/api/jobs/${encodeURIComponent(trackId)}/lyrics`);
    if (!res.ok && res.status !== 404) return null;
    return fromServerLyrics(await res.json().catch(() => null));
  } catch (err) {
    console.warn("server lyrics fetch failed", err);
    return null;
  }
}

/** The lyrics a track's file carried, from the server, or "" if unavailable. */
async function embeddedLyrics(trackId) {
  try {
    const res = await fetch(`/api/jobs/${encodeURIComponent(trackId)}`);
    if (!res.ok) return "";
    const state = await res.json();
    return typeof state?.audio_tags?.lyrics === "string" ? state.audio_tags.lyrics : "";
  } catch (err) {
    console.warn("embedded lyrics fetch failed", err);
    return "";
  }
}

/**
 * The artist and song the track is known to be, best source first: the
 * file's tags, then the artist (tagged, or the band saved from the artist box)
 * with the song's name cleaned out of the title. An empty song means nothing
 * is known well enough to look up.
 */
function knownSong(info) {
  const tags = info.audioTags || {};
  const band = getCurrentTrackArtist();
  const artist = tags.artist || band?.englishName || band?.name || "";
  // The band's names, its own and its English one: the same artist, which
  // LRCLIB may file the song under.
  const names = [band?.name, band?.englishName].filter(Boolean);
  if (tags.title) return { artist, names, song: songFromTitle(tags.title, artist) || tags.title };
  if (!artist) return { artist, names, song: "" };
  return { artist, names, song: songFromTitle(info.title, artist) };
}

/** Show what is kept for the open track, or find it. */
async function loadForCurrentTrack() {
  const info = getCurrentTrackInfo();
  shownTrackId = info?.id ?? null;
  nothingKnownFor = null;
  searchController?.abort();
  searchController = null;
  clearLyrics();
  const token = ++loadToken;
  if (!info) {
    setStatus(t("lyrics.noTrack"), "muted");
    return;
  }
  setStatus("");
  let saved = null;
  try {
    saved = await storeGet(storeKey(info.id), null);
  } catch (err) {
    console.warn("lyrics store read failed", err);
  }
  if (token !== loadToken) return;
  if (saved?.dismissed) {
    showRemoved();
    return;
  }
  if (saved?.entry) {
    show(saved.entry, Array.isArray(saved.others) ? saved.others : []);
    return;
  }

  // What the server found while the track was separated. Shown, not saved:
  // picking another version is what saves one, as the user's own choice.
  const found = await serverLyrics(info.id);
  if (token !== loadToken) return;
  if (found?.entry) {
    show(found.entry, found.others);
    return;
  }
  // None was the length of the track: its versions, to pick from.
  if (found?.others.length) {
    offer(found.others);
    return;
  }

  // Lyrics the file came with are this recording's own: nothing to look up.
  // The library keeps only that there are some (libraryAudioTags); the text is
  // asked of the server once, then kept in this track's own entry below.
  const { artist, song, names } = knownSong(info);
  const embedded = info.audioTags?.lyrics || (info.audioTags?.hasLyrics ? await embeddedLyrics(info.id) : "");
  if (token !== loadToken) return;
  if (embedded) {
    const synced = parseLrc(embedded).length ? embedded : "";
    await keep({
      id: 0,
      source: "file",
      track: info.audioTags.title || song || info.title,
      artist,
      album: info.audioTags.album || "",
      duration: info.duration,
      instrumental: false,
      synced,
      plain: synced ? "" : embedded,
    }, []);
    return;
  }
  if (!song) {
    setStatus(t("lyrics.nothingKnown"), "muted");
    nothingKnownFor = info.id;
    return;
  }
  if (answered.has(info.id)) {
    showAnswer(answered.get(info.id));
    return;
  }
  lookUp(info, { artist, song, names });
}

// While the tab is on screen: notice a different track being opened, mark the
// line being sung and fill its words. One frame loop rather than events,
// because playback time has no event of its own here and the loop stops the
// moment the tab is hidden.
function tick() {
  frame = 0;
  if (!visible) return;
  const id = getCurrentTrackInfo()?.id ?? null;
  if (id !== shownTrackId) loadForCurrentTrack();
  if (lines.length) {
    // 50ms ahead: a click on a line seeks to its exact stamp, and the time read
    // back can land a hair before it, which would mark the line above.
    const now = (transport()?.getCurrentTime?.() ?? 0) + 0.05;
    const index = currentLineIndex(lines, now);
    if (index !== currentIndex) moveTo(index);
    fillWords(index, now);
  }
  frame = requestAnimationFrame(tick);
}

/** Make `index` the line being sung: marks, the next line, the scroll. */
function moveTo(index) {
  const before = lineEls[currentIndex];
  if (before) {
    before.classList.remove("current");
    for (const word of lineWords[currentIndex] || []) {
      word.el.classList.remove("sung", "singing");
      word.el.style.removeProperty("--fill");
    }
  }
  lineEls[currentIndex + 1]?.classList.remove("next");
  currentIndex = index;
  lineEls[index + 1]?.classList.add("next");
  const line = lineEls[index];
  if (!line) return;
  line.classList.add("current");
  // Only the lyrics box scrolls: scrollIntoView would also move the sidebar
  // and the page if they had anything to give.
  bodyEl.scrollTo({
    top: line.offsetTop - bodyEl.clientHeight / 2 + line.offsetHeight / 2,
    behavior: reducedMotion.matches ? "auto" : "smooth",
  });
}

/** The wipe: words already sung in colour, the one being sung part-filled. */
function fillWords(index, now) {
  for (const word of lineWords[index] || []) {
    const sung = now >= word.end;
    const singing = !sung && now > word.start;
    word.el.classList.toggle("sung", sung);
    word.el.classList.toggle("singing", singing);
    if (singing) {
      const share = (now - word.start) / (word.end - word.start);
      word.el.style.setProperty("--fill", `${Math.round(share * 100)}%`);
    }
  }
}

function setVisible(on) {
  visible = on;
  if (on) {
    loadForCurrentTrack();
    if (!frame) frame = requestAnimationFrame(tick);
  } else if (frame) {
    cancelAnimationFrame(frame);
    frame = 0;
  }
}

export function initLyrics() {
  const panel = document.getElementById("lyricsPanel");
  statusEl = document.getElementById("lyricsStatus");
  versionsEl = document.getElementById("lyricsVersions");
  bodyEl = document.getElementById("lyricsBody");
  if (!panel || !statusEl || !versionsEl || !bodyEl) return;

  document.addEventListener("catalogviewchange", (e) => setVisible(e.detail?.view === "lyrics"));
  document.addEventListener("tracktags", (e) => {
    if (visible && nothingKnownFor && e.detail?.id === nothingKnownFor) loadForCurrentTrack();
  });
}
