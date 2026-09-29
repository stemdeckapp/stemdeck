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
// Align moves synced lyrics timed to another cut of the song (a video's
// intro) onto this track: the first sung line, or the one last clicked, to
// the playhead; nudges earlier or later; the server's estimate from the
// vocals stem; or back to their own timing. What moved is one offset kept
// beside the synced text, never the text: on the server for lyrics it keeps
// (POST .../lyrics/offset), in the track's store entry for lyrics kept here.
//
// Sync lines times the lines one by one: tap along with playback, drag a
// line's marker in the lane over the waveform (lyricsLane.js), nudge the line
// in hand, or re-time every line from the vocals. The user's timing takes
// precedence over the server's fitted to the vocals, which takes precedence
// over the lyrics' own at the Align offset (lyricsSync.js timingOf); the
// offset applies to the last only, and the Align panel says so.
//
// Everything that came from the network or a file's tags goes in with
// textContent, never as HTML: anyone can edit LRCLIB or a tag, and this page
// can reach the desktop app's native commands.

import { t, getLanguage } from "./i18n.js";
import { storeGet, storeSet } from "./utils.js";
import { getCurrentTrackInfo, getCurrentTrackArtist } from "./catalog.js";
import { transport, setPlayheadTime } from "./transport.js";
import { totalDuration } from "./state.js";
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
  sameSong,
  clampOffset,
  shiftLines,
  firstSungIndex,
  offsetToStart,
  alignedFrom,
} from "./lyricsLookup.js";
import {
  timingOf,
  serializeLrc,
  snapToVoice,
  applyTime,
  SyncSession,
} from "./lyricsSync.js";
import {
  showLane,
  hideLane,
  updateLane,
  setLaneSelected,
  setLaneCurrent,
  setLanePlayhead,
  revealLaneSelected,
  formatClock,
} from "./lyricsLane.js";

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
let lines = []; // timed lines of what is shown, in the timing in effect, [] for plain text
let baseLines = []; // the lyrics' own lines at their own timing
let offset = 0; // seconds the lines are moved later (earlier when negative)
// Which timing is in effect (lyricsSync.js timingOf): "user" (Sync lines),
// "aligned" (fitted to the vocals by the server) or "offset" (the lyrics' own,
// moved by the Align panel's offset, which applies to this one only).
let timingKind = "offset";
// What is on screen and where it is kept: "server" for lyrics.json, "store"
// for the track's entry here. The Align panel saves to the same place.
let shownEntry = null;
let shownOthers = [];
let shownFrom = "store";
let pickedIndex = -1; // the line last clicked, which Start lyrics here moves
// Sync lines (lyricsSync.js, lyricsLane.js): the mode, kept on while the
// track is; its panel, session and elements while it is on screen.
let syncOpen = false;
let sync = null;
let syncToggleEl = null; // the Sync lines link in the tools row
let userSave = null; // the user's timing waiting to be saved, or its removal
let userSaveTimer = 0;
let userSaveChain = Promise.resolve();
let retimeToken = 0; // guards a re-time's polling against a switch or a close
let leadIn = false; // a line taken in hand puts the playhead a little before it
let snapOn = true; // taps and drops snap to where the singing starts
let alignOpen = false; // the Align panel, kept open while the track is
let align = null; // the Align panel's elements while it is on screen
let alignToggleEl = null; // the Align link in the tools row
let saveTimer = 0;
let pendingSave = null;
let alignToken = 0; // guards against an estimate landing after a switch
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
  closeSync(false, { keepOpen: true });
  flushSave();
  lines = [];
  timingKind = "offset";
  baseLines = [];
  offset = 0;
  shownEntry = null;
  shownOthers = [];
  pickedIndex = -1;
  align = null;
  alignToggleEl = null;
  alignToken++;
  lineEls = [];
  lineWords = [];
  currentIndex = -1;
  bodyEl.replaceChildren();
  bodyEl.classList.remove("synced", "aligning", "syncing");
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
function lineButton(line, nextTime, index) {
  const row = tagLang(el("button", "lyrics-line"), line.text);
  row.type = "button";
  row.title = t("lyrics.seekTitle");
  row.addEventListener("click", (e) => {
    if (sync) {
      // In Sync lines a line is taken in hand, and the playhead goes to it
      // (a little before, with the lead-in, to hear into it).
      selectLine(index, { seek: true });
      if (e.detail > 0) row.blur();
      return;
    }
    // Its time in the timing in effect now, not when the button was made.
    setPlayheadTime(Math.max(0, lines[index]?.time ?? line.time));
    pickedIndex = index;
    showAlignTarget();
    // A mouse click lets go of the line, so Space goes back to play and pause
    // rather than pressing the line again, and no focus box stays on it. A
    // key press (detail 0) keeps focus there for whoever is using the keys.
    if (e.detail > 0) row.blur();
  });
  lineWords.push(fillLine(row, line, nextTime));
  return row;
}

/** A line's words as spans in `row`, timed for the wipe: [{ el, start, end }]. */
function fillLine(row, line, nextTime) {
  row.replaceChildren();
  const words = [];
  for (const word of wordTimings(line, nextTime, envelopeFor === shownTrackId ? envelope : null)) {
    const span = el("span", "lw", word.text);
    row.append(span);
    words.push({ el: span, start: word.start, end: word.end });
  }
  // An empty stamp marks an instrumental gap in LRC; a note keeps its place.
  if (!words.length) row.textContent = "♪";
  return words;
}

/**
 * Draw what is kept for the track: `entry` as saved, `others` to offer.
 * `from` is where it is kept, "server" or "store", which is where the Align
 * panel saves an offset.
 */
function show(entry, others = [], from = "store") {
  clearLyrics();
  if (!entry) return;
  shownEntry = entry;
  shownOthers = others;
  shownFrom = from;

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
  baseLines = entry.synced ? parseLrc(entry.synced) : [];
  offset = clampOffset(entry.offsetSec);
  applyTiming();
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
  // Words to align, or to say why they cannot be: not for an instrumental.
  // The panel goes under the tools row, above the versions list.
  if (entry.synced || entry.plain) tools.append(syncToggle(), alignToggle());
  if (baseLines.length && alignOpen && !syncOpen) tools.after(alignPanel());
  const remove = el("button", "lyrics-link", t("lyrics.remove"));
  remove.type = "button";
  remove.addEventListener("click", () => removeLyrics());
  tools.append(remove);

  if (lines.length) {
    renderLines();
    loadEnvelope(shownTrackId);
  } else if (entry.plain) {
    // Composed, as parseLrc does for synced lines.
    for (const text of entry.plain.normalize("NFC").split(/\r?\n/)) bodyEl.append(tagLang(el("p", "lyrics-text", text || " "), text));
  }
  bodyEl.scrollTop = 0;
  if (syncOpen && lines.length) openSync();
}

/** The timing in effect for what is shown, into `lines`: the user's own, the
 * server's fitted to the vocals, or the lyrics' own at the offset. */
function applyTiming() {
  const timing = shownEntry ? timingOf({ ...shownEntry, offsetSec: offset }) : { kind: "offset", lines: [] };
  timingKind = timing.kind;
  lines = timing.lines;
}

/** Draw `lines` as the synced list, afresh. */
function renderLines() {
  lineWords = [];
  currentIndex = -1;
  lineEls = lines.map((line, i) => lineButton(line, lines[i + 1]?.time ?? null, i));
  bodyEl.replaceChildren(...lineEls);
  bodyEl.classList.add("synced");
  showLineTimes();
}

/** Show `next` as the lines' timing at once: the list, the wipe, a line
 * click and the lane all follow. The list is drawn again only when its lines
 * are not the same ones (a reset to a timing with other lines). */
function setDisplayLines(next) {
  const same = next.length === lines.length && next.every((line, i) => line.text === lines[i].text);
  lines = next;
  if (!same) {
    renderLines();
    if (sync) placeLineTools();
    return;
  }
  retime();
  showLineTimes();
}

/** In Sync lines, each line says when it starts (drawn by CSS from
 * data-time, so it is not part of the line's text). */
function showLineTimes() {
  if (!sync) return;
  lineEls.forEach((row, i) => {
    const time = formatClock(lines[i]?.time ?? 0);
    if (row.dataset.time !== time) row.dataset.time = time;
  });
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

/** Give the words on screen the envelope's timings, or the estimate without
 * one, at the lines' times now: once, not per frame. */
function retime() {
  const levels = envelopeFor === shownTrackId ? envelope : null;
  lines.forEach((line, i) => {
    const words = lineWords[i];
    const timed = wordTimings(line, lines[i + 1]?.time ?? null, levels);
    if (!words) return;
    if (timed.length !== words.length) {
      // Timing with word stamps where there were none, or the other way.
      if (lineEls[i]) lineWords[i] = fillLine(lineEls[i], line, lines[i + 1]?.time ?? null);
      return;
    }
    timed.forEach((word, k) => {
      words[k].start = word.start;
      words[k].end = word.end;
    });
  });
}

// ── Align ──

// The nudges, in seconds: a coarse and a fine step each way.
const NUDGES = [-0.5, -0.1, 0.1, 0.5];
// An offset is saved this long after its last change, so a run of nudges is
// one request rather than one each.
const SAVE_DELAY_MS = 400;

/** Seconds to a tenth, in the app's language: "+15.9", "-0.5", "0.0". */
function formatSeconds(seconds, signDisplay = "exceptZero", digits = 1) {
  try {
    return new Intl.NumberFormat(getLanguage(), {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
      signDisplay,
    }).format(seconds);
  } catch (err) {
    console.warn("number format failed", err);
    return (signDisplay !== "never" && seconds > 0 ? "+" : "") + seconds.toFixed(digits);
  }
}

/** The Align link in the tools row. Disabled, and saying why, for lyrics
 * without timing: aria-disabled rather than disabled, so it keeps its
 * tooltip and can still be reached with the keys to read it. */
function alignToggle() {
  const toggle = el("button", "lyrics-link lyrics-align-toggle", t("lyrics.align.button"));
  toggle.type = "button";
  alignToggleEl = toggle;
  if (!baseLines.length) {
    toggle.setAttribute("aria-disabled", "true");
    toggle.title = t("lyrics.align.syncedOnly");
    return toggle;
  }
  toggle.title = t("lyrics.align.title");
  toggle.setAttribute("aria-controls", "lyricsAlign");
  toggle.setAttribute("aria-expanded", String(alignOpen));
  toggle.addEventListener("click", () => {
    if (align) closeAlign(false);
    else openAlign();
  });
  return toggle;
}

function openAlign() {
  if (!baseLines.length || align) return;
  if (sync) closeSync(false);
  alignOpen = true;
  alignToggleEl?.setAttribute("aria-expanded", "true");
  alignToggleEl?.closest(".lyrics-tools")?.after(alignPanel());
}

function closeAlign(focusToggle) {
  alignOpen = false;
  align?.panel.remove();
  align = null;
  bodyEl.classList.remove("aligning");
  alignToggleEl?.setAttribute("aria-expanded", "false");
  if (focusToggle) alignToggleEl?.focus();
}

/** One of the panel's buttons. A mouse click lets go of it, as a line does,
 * so Space goes back to play and pause mid-practice. */
function alignButton(text, onClick, className = "") {
  const btn = el("button", `lyrics-align-btn${className ? ` ${className}` : ""}`, text);
  btn.type = "button";
  btn.addEventListener("click", (e) => {
    onClick();
    if (e.detail > 0) btn.blur();
  });
  return btn;
}

function alignPanel() {
  const panel = el("div", "lyrics-align");
  panel.id = "lyricsAlign";
  panel.setAttribute("role", "group");
  panel.setAttribute("aria-label", t("lyrics.align.title"));

  const start = alignButton(t("lyrics.align.start"), startHere, "lyrics-align-start");
  start.title = t("lyrics.align.startTitle");
  const target = el("p", "lyrics-align-target");

  const nudges = el("div", "lyrics-align-nudges");
  const readout = el("output", "lyrics-align-offset");
  readout.setAttribute("aria-live", "polite");
  readout.title = t("lyrics.align.offsetTitle");
  const steps = NUDGES.map((step) => {
    const btn = alignButton(formatSeconds(step, "always"), () => applyOffset(offset + step), "lyrics-align-nudge");
    const label = t(step < 0 ? "lyrics.align.earlier" : "lyrics.align.later", { seconds: formatSeconds(Math.abs(step), "never") });
    btn.setAttribute("aria-label", label);
    btn.title = label;
    return btn;
  });
  nudges.append(steps[0], steps[1], readout, steps[2], steps[3]);

  const actions = el("div", "lyrics-align-actions");
  const detect = alignButton(t("lyrics.align.detect"), autoDetect);
  detect.title = t("lyrics.align.detectTitle");
  const reset = alignButton(t("lyrics.align.reset"), () => applyOffset(0));
  reset.title = t("lyrics.align.resetTitle");
  // Closes the panel. Nothing to save here: every change is saved as it is
  // made (a burst of nudges once, after the last).
  const done = alignButton(t("lyrics.align.done"), () => closeAlign(true), "lyrics-align-done");
  done.title = t("lyrics.align.doneTitle");
  actions.append(detect, reset, done);

  const message = el("p", "lyrics-align-message");
  message.setAttribute("aria-live", "polite");

  // The offset moves the lyrics' own timing only. Lines timed one by one (by
  // hand, or fitted to the vocals) take precedence, and the panel says so
  // rather than moving nothing.
  const note = el("p", "lyrics-align-note");
  note.setAttribute("aria-live", "polite");
  const syncBtn = alignButton(t("lyrics.sync.open"), () => openSync({ focus: true }));
  syncBtn.title = t("lyrics.sync.openTitle");
  const offsetControls = [start, ...steps, detect, reset];

  panel.append(el("p", "lyrics-align-hint", t("lyrics.align.hint")), note, start, target, nudges, actions, syncBtn, message);
  panel.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    e.stopPropagation();
    closeAlign(true);
  });
  align = { panel, target, readout, detect, message, note, offsetControls };
  bodyEl.classList.add("aligning");
  showAlignApplies();
  showOffset();
  showAlignTarget();
  return panel;
}

/** Whether the offset is what times the lines now, and the panel's controls
 * and note to match. */
function showAlignApplies() {
  if (!align) return;
  const applies = timingKind === "offset";
  for (const btn of align.offsetControls) btn.disabled = !applies;
  align.note.textContent = applies ? "" : t(timingKind === "user" ? "lyrics.align.perLineUser" : "lyrics.align.perLineAligned");
}

function showOffset() {
  if (align) align.readout.textContent = t("lyrics.align.offset", { offset: formatSeconds(offset) });
}

function setAlignMessage(text = "", kind = "") {
  if (!align) return;
  align.message.textContent = text;
  align.message.className = `lyrics-align-message${kind ? ` ${kind}` : ""}`;
}

/** The line Start lyrics here moves: the one last clicked, else the first
 * with words. */
function alignIndex() {
  return pickedIndex >= 0 && pickedIndex < baseLines.length ? pickedIndex : firstSungIndex(baseLines);
}

/** Say which line Start lyrics here moves, and mark it among the lines. */
function showAlignTarget() {
  lineEls.forEach((row, i) => row.classList.toggle("picked", i === pickedIndex));
  if (!align) return;
  const index = alignIndex();
  const text = baseLines[index]?.text || "";
  align.target.textContent = text
    ? t(pickedIndex >= 0 ? "lyrics.align.targetPicked" : "lyrics.align.targetFirst", { line: text })
    : "";
  align.target.removeAttribute("lang");
  tagLang(align.target, text);
}

/** Start lyrics here: the line alignIndex names moves to the playhead. */
function startHere() {
  const index = alignIndex();
  if (index < 0) return;
  const now = transport()?.getCurrentTime?.() ?? 0;
  applyOffset(offsetToStart(baseLines, index, now));
}

/**
 * Show the lines `seconds` later than their own timing, at once: the wipe,
 * the line marked and a line click all read `lines`, which the frame loop
 * picks up on its next frame. Saved shortly after unless `save` is false
 * (the server has saved it already).
 */
function applyOffset(seconds, { save = true } = {}) {
  if (!baseLines.length) return;
  offset = clampOffset(seconds);
  if (shownEntry) shownEntry.offsetSec = offset;
  // Only the lyrics' own timing moves with the offset; lines timed one by one
  // keep their times.
  if (timingKind === "offset") {
    lines = shiftLines(baseLines, offset);
    retime();
  }
  showOffset();
  setAlignMessage();
  if (save) scheduleSave();
}

function scheduleSave() {
  pendingSave = { trackId: shownTrackId, entry: shownEntry, others: shownOthers, from: shownFrom, value: offset };
  clearTimeout(saveTimer);
  saveTimer = setTimeout(flushSave, SAVE_DELAY_MS);
}

/** Save the offset waiting to be saved, now. */
function flushSave() {
  clearTimeout(saveTimer);
  saveTimer = 0;
  const save = pendingSave;
  pendingSave = null;
  return Promise.all([save ? saveOffset(save) : null, flushUserSave()]);
}

/** Forget an offset waiting to be saved in the store: the entry it belongs
 * to is being replaced or removed there, and saving it would bring it back.
 * One for the server is saved, since lyrics.json stays. */
function dropStoreSave() {
  if (pendingSave?.from === "store") {
    clearTimeout(saveTimer);
    saveTimer = 0;
    pendingSave = null;
  }
  if (userSave?.from === "store") {
    clearTimeout(userSaveTimer);
    userSaveTimer = 0;
    userSave = null;
  }
  return flushSave();
}

async function saveOffset({ trackId, entry, others, from, value }) {
  try {
    if (from === "server") {
      const res = await fetch(`/api/jobs/${encodeURIComponent(trackId)}/lyrics/offset`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ offset_sec: value }),
        keepalive: true,
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } else {
      await storeSet(storeKey(trackId), { entry: { ...entry, offsetSec: value }, others });
    }
  } catch (err) {
    console.warn("lyrics offset save failed", err);
    if (trackId === shownTrackId) setAlignMessage(t("lyrics.align.saveFailed"), "error");
  }
}

/**
 * Auto-detect: the server estimates from the vocals stem how much later the
 * lines are sung than their own timing says. Applied when it is sure; when
 * not, the lyrics stay as they are and the panel says so. The server keeps
 * what it applies to lyrics.json itself; lyrics kept here are sent along
 * and the answer saved here.
 */
async function autoDetect() {
  if (!align || !baseLines.length || align.detect.getAttribute("aria-busy") === "true") return;
  const trackId = shownTrackId;
  const from = shownFrom;
  const synced = shownEntry?.synced || "";
  // A nudge not yet saved is what "as they are" means if it cannot tell.
  await flushSave();
  const token = ++alignToken;
  if (!align) return;
  align.detect.setAttribute("aria-busy", "true");
  setAlignMessage(t("lyrics.align.detecting"), "loading");
  let answer = null;
  try {
    const res = await fetch(`/api/jobs/${encodeURIComponent(trackId)}/lyrics/align`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(from === "server" ? {} : { synced }),
    });
    if (res.ok) answer = await res.json();
    else console.warn("lyrics auto-detect refused", res.status);
  } catch (err) {
    console.warn("lyrics auto-detect failed", err);
  }
  if (token !== alignToken || trackId !== shownTrackId || !align) return;
  align.detect.removeAttribute("aria-busy");
  if (!answer) {
    setAlignMessage(t("lyrics.align.detectFailed"), "error");
  } else if (answer.confident === true && typeof answer.offset_sec === "number") {
    applyOffset(answer.offset_sec, { save: from !== "server" });
    setAlignMessage(t("lyrics.align.detected", { offset: formatSeconds(offset) }));
  } else {
    setAlignMessage(t("lyrics.align.unsure"), "muted");
  }
}

// ── Sync lines ──
//
// Timing the lines one by one, for lyrics whose own timing is off in places
// rather than as a whole. Tap along with playback (T or Enter at the start of
// each line), or drag a line's marker in the lane over the waveform, or nudge
// the line in hand. What is set is the user's timing, which takes precedence
// over the server's fitted to the vocals and over the Align offset
// (lyricsSync.js timingOf). It is kept where the lyrics are: PUT
// .../lyrics/user-synced for lyrics.json, the track's store entry for lyrics
// kept here.

// The arrows' steps, in seconds, and Shift's.
const NUDGE_FINE = 0.05;
const NUDGE_COARSE = 0.25;
// The lead-in: how far before a line the playhead goes when it is taken in hand.
const LEAD_IN_SEC = 2;
// How often a re-time from the vocals is asked how it is getting on, and how
// many answers of "idle" are taken for "not started yet" rather than lost.
const RETIME_POLL_MS = 1000;
const RETIME_IDLE_POLLS = 3;

const now = () => transport()?.getCurrentTime?.() ?? 0;
const levels = () => (envelopeFor === shownTrackId ? envelope : null);
const laneLength = () => totalDuration || (lines.at(-1)?.time ?? 0) + 10;

/** The Sync lines link in the tools row. Disabled, and saying why, for
 * lyrics with no timing at all. */
function syncToggle() {
  const toggle = el("button", "lyrics-link lyrics-sync-toggle", t("lyrics.sync.button"));
  toggle.type = "button";
  syncToggleEl = toggle;
  if (!lines.length) {
    toggle.setAttribute("aria-disabled", "true");
    toggle.title = t("lyrics.sync.syncedOnly");
    return toggle;
  }
  toggle.title = t("lyrics.sync.openTitle");
  toggle.setAttribute("aria-controls", "lyricsSync");
  toggle.setAttribute("aria-expanded", String(Boolean(sync)));
  toggle.addEventListener("click", (e) => {
    if (sync) closeSync(false);
    else openSync({ focus: e.detail === 0 });
    if (e.detail > 0) toggle.blur();
  });
  return toggle;
}

function openSync({ focus = false } = {}) {
  if (!lines.length || sync) return;
  if (align) closeAlign(false);
  syncOpen = true;
  const session = new SyncSession(lines, {
    // The lyrics' own timing is usually right line to line and off as a
    // whole, so a tap carries the lines after it along; timing already fitted
    // line by line is left where it is.
    ripple: timingKind === "offset",
    max: totalDuration || Infinity,
    meta: { kind: timingKind },
  });
  sync = buildSyncPanel(session);
  syncToggleEl?.closest(".lyrics-tools")?.after(sync.panel);
  syncToggleEl?.setAttribute("aria-expanded", "true");
  bodyEl.classList.add("syncing");
  showLineTimes();
  showLane(lines, laneLength(), {
    select: (i) => selectLine(i),
    click: (i) => selectLine(i, { seek: true }),
    drag: (i, time, { free }) => applyTime(sync.session.lines, i, snapped(time, free), {
      mode: sync.session.ripple ? "ripple" : "clamp",
      fixed: sync.session.touched,
      max: sync.session.max,
    }),
    drop: (i, time, { free, cancelled }) => {
      if (!cancelled && sync.session.move(i, snapped(time, free))) edited();
      else updateLane(sync.session.lines, sync.session.cursor);
    },
    done: () => closeSync(true),
  });
  onCursor();
  showSyncState();
  if (focus) sync.tap.focus();
}

/** Leave Sync lines: what is waiting to be saved is saved now. `keepOpen`
 * leaves the mode on for when the same track's lyrics are drawn again. */
function closeSync(focusToggle, { keepOpen = false } = {}) {
  if (!keepOpen) syncOpen = false;
  if (!sync) return;
  retimeToken++;
  flushUserSave();
  sync.panel.remove();
  sync.lineTools.remove();
  for (const row of lineEls) {
    row.classList.remove("selected");
    delete row.dataset.time;
  }
  bodyEl.classList.remove("syncing");
  hideLane();
  sync = null;
  syncToggleEl?.setAttribute("aria-expanded", "false");
  if (focusToggle) syncToggleEl?.focus();
}

/** One of the panel's options, a checkbox that lets go of focus once
 * changed, so Space goes back to play and pause. */
function syncOption(key, checked, onChange, titleKey) {
  const label = el("label", "lyrics-sync-opt");
  const box = el("input");
  box.type = "checkbox";
  box.checked = checked;
  box.addEventListener("change", () => {
    onChange(box.checked);
    box.blur();
  });
  label.title = t(titleKey);
  label.append(box, el("span", "", t(key)));
  return label;
}

function buildSyncPanel(session) {
  const panel = el("div", "lyrics-sync");
  panel.id = "lyricsSync";
  panel.setAttribute("role", "group");
  panel.setAttribute("aria-label", t("lyrics.sync.title"));

  const head = el("div", "lyrics-sync-head");
  const kind = el("span", "lyrics-sync-kind");
  const saveState = el("span", "lyrics-sync-kind lyrics-sync-save");
  saveState.setAttribute("aria-live", "polite");
  head.append(el("h3", "lyrics-sync-title", t("lyrics.sync.title")), kind, saveState);

  const next = el("p", "lyrics-sync-next");
  const nextLabel = el("span", "lyrics-sync-next-label");
  const nextText = el("span", "lyrics-sync-next-text");
  next.append(nextLabel, nextText);
  next.setAttribute("aria-live", "polite");

  const tap = alignButton(t("lyrics.sync.tap"), tapNow, "lyrics-sync-tap");
  tap.append(el("kbd", "", t("lyrics.sync.tapKey")));
  tap.title = t("lyrics.sync.tapTitle");

  const undo = alignButton(t("lyrics.sync.undo"), undoSync);
  undo.title = t("lyrics.sync.undoTitle");
  const edits = el("div", "lyrics-sync-row");
  edits.append(undo);

  const opts = el("div", "lyrics-sync-opts");
  opts.append(
    syncOption("lyrics.sync.leadIn", leadIn, (on) => { leadIn = on; }, "lyrics.sync.leadInTitle"),
    syncOption("lyrics.sync.snap", snapOn, (on) => { snapOn = on; }, "lyrics.sync.snapTitle"),
    syncOption("lyrics.sync.ripple", session.ripple, (on) => { session.ripple = on; }, "lyrics.sync.rippleTitle"),
  );

  const retimeBtn = alignButton(t("lyrics.sync.retime"), retimeFromVocals);
  retimeBtn.title = t("lyrics.sync.retimeTitle");
  const detected = alignButton(t("lyrics.sync.resetDetected"), resetToDetected);
  detected.title = t("lyrics.sync.resetDetectedTitle");
  const original = alignButton(t("lyrics.sync.resetOriginal"), resetToOriginal);
  original.title = t("lyrics.sync.resetOriginalTitle");
  // No timing of their own (plain lyrics fitted to the vocals): nothing to
  // go back to.
  original.disabled = !baseLines.length;
  const resets = el("div", "lyrics-sync-row");
  resets.append(retimeBtn, detected, original);

  const message = el("p", "lyrics-align-message");
  message.setAttribute("aria-live", "polite");
  const keys = el("p", "lyrics-sync-keys", t("lyrics.sync.keys"));
  const done = alignButton(t("lyrics.sync.done"), () => closeSync(true), "lyrics-align-done");
  done.title = t("lyrics.sync.doneTitle");

  panel.append(head, next, tap, edits, opts, resets, message, keys, done);

  // The line in hand's own controls, shown under it in the list.
  const lineTools = el("div", "lyrics-line-tools");
  lineTools.setAttribute("role", "group");
  lineTools.setAttribute("aria-label", t("lyrics.sync.lineToolsAria"));
  const lineStep = (step) => {
    // Hundredths: the steps are 0.05 and 0.25 s.
    const btn = alignButton(formatSeconds(step, "always", 2), () => nudgeLine(step), "lyrics-align-nudge");
    const label = t(step < 0 ? "lyrics.align.earlier" : "lyrics.align.later", { seconds: formatSeconds(Math.abs(step), "never", 2) });
    btn.setAttribute("aria-label", label);
    btn.title = label;
    return btn;
  };
  const here = alignButton(t("lyrics.sync.here"), lineToPlayhead, "lyrics-line-here");
  here.title = t("lyrics.sync.hereTitle");
  lineTools.append(lineStep(-NUDGE_COARSE), lineStep(-NUDGE_FINE), lineStep(NUDGE_FINE), lineStep(NUDGE_COARSE), here);

  return { panel, session, kind, saveState, nextLabel, nextText, tap, undo, retimeBtn, message, lineTools };
}

/** Where the singing starts near `time`, with Snap on and Alt not held. */
function snapped(time, free = false) {
  return snapOn && !free ? snapToVoice(levels(), time) : time;
}

/** Take line `index` in hand; with `seek`, the playhead goes to it. */
function selectLine(index, { seek = false } = {}) {
  if (!sync || index < 0 || index >= lines.length) return;
  sync.session.select(index);
  onCursor({ reveal: true });
  if (seek) setPlayheadTime(Math.max(0, lines[index].time - (leadIn ? LEAD_IN_SEC : 0)));
}

/** Show which line is in hand: framed in the list with its controls under
 * it, lit in the lane, named as the next to tap. */
function onCursor({ reveal = false } = {}) {
  if (!sync) return;
  const index = sync.session.cursor;
  lineEls.forEach((row, i) => row.classList.toggle("selected", i === index));
  setLaneSelected(index);
  placeLineTools();
  const line = lines[index];
  sync.tap.disabled = !line;
  sync.nextText.removeAttribute("lang");
  if (line) {
    sync.nextLabel.textContent = t("lyrics.sync.next", { n: index + 1, count: lines.length });
    sync.nextText.textContent = line.text || "♪";
    tagLang(sync.nextText, line.text);
  } else {
    sync.nextLabel.textContent = t("lyrics.sync.allSetLabel");
    sync.nextText.textContent = t("lyrics.sync.allSet");
  }
  if (!reveal) return;
  const row = lineEls[index];
  if (row && (row.offsetTop < bodyEl.scrollTop || row.offsetTop + row.offsetHeight > bodyEl.scrollTop + bodyEl.clientHeight)) {
    bodyEl.scrollTo({ top: row.offsetTop - bodyEl.clientHeight / 3, behavior: "auto" });
  }
  revealLaneSelected();
}

function placeLineTools() {
  if (!sync) return;
  const row = lineEls[sync.session.cursor];
  if (row) row.after(sync.lineTools);
  else sync.lineTools.remove();
}

/** What Sync lines says about the timing in effect. */
function showSyncState() {
  if (!sync) return;
  sync.kind.textContent = t(`lyrics.sync.kind.${timingKind}`);
  sync.undo.disabled = !sync.session.canUndo;
}

function setSyncMessage(text = "", kind = "") {
  if (!sync) return;
  sync.message.textContent = text;
  sync.message.className = `lyrics-align-message${kind ? ` ${kind}` : ""}`;
}

/**
 * The session changed: show its lines as the timing in effect everywhere,
 * and keep them. Which timing they are is the session's (meta.kind): the
 * user's own after an edit, or what a reset or an undo went back to, in
 * which case the user's timing is dropped rather than saved.
 */
function syncChanged({ reveal = false } = {}) {
  if (!sync) return;
  const { session } = sync;
  setDisplayLines(session.lines);
  updateLane(lines, session.cursor);
  onCursor({ reveal });
  if (session.meta.kind === "user") {
    saveUserTiming(serializeLrc(lines));
  } else if (shownEntry?.userSynced) {
    saveUserTiming(null);
  }
  timingKind = session.meta.kind;
  showSyncState();
  showAlignApplies();
}

/** An edit by hand: the lines are now the user's own timing. */
function edited(options) {
  sync.session.meta.kind = "user";
  setSyncMessage();
  syncChanged(options);
}

function tapNow() {
  if (!sync || sync.session.done) return;
  sync.session.tap(snapped(now()));
  // The tap is seen as well as heard: the button flashes.
  sync.tap.classList.remove("tapped");
  void sync.tap.offsetWidth;
  sync.tap.classList.add("tapped");
  edited({ reveal: true });
}

function nudgeLine(delta) {
  if (!sync || sync.session.done) return;
  if (sync.session.nudge(sync.session.cursor, delta)) edited({ reveal: true });
}

/** The line in hand starts at the playhead, exactly; it stays in hand. */
function lineToPlayhead() {
  if (!sync || sync.session.done) return;
  if (sync.session.setTime(sync.session.cursor, now(), "push")) edited();
}

function undoSync() {
  if (sync?.session.undo()) {
    setSyncMessage();
    syncChanged({ reveal: true });
  }
}

function redoSync() {
  if (sync?.session.redo()) {
    setSyncMessage();
    syncChanged({ reveal: true });
  }
}

/** Reset to detected: the user's timing goes, and what was there before it
 * shows again, the server's fitted to the vocals or the lyrics' own at the
 * Align offset. Undo brings it back. */
function resetToDetected() {
  if (!sync || !shownEntry) return;
  const detected = timingOf({ ...shownEntry, userSynced: "", offsetSec: offset });
  if (!detected.lines.length) {
    setSyncMessage(t("lyrics.sync.nothingDetected"), "muted");
    return;
  }
  sync.session.replace(detected.lines);
  sync.session.meta.kind = detected.kind;
  syncChanged();
  setSyncMessage(t(`lyrics.sync.backTo.${detected.kind}`));
}

/** Reset to original: the lyrics' own timing, as they came, with no offset,
 * kept as the user's timing so it takes precedence over any other. */
function resetToOriginal() {
  if (!sync || !baseLines.length) return;
  sync.session.replace(baseLines);
  edited();
  setSyncMessage(t("lyrics.sync.backTo.original"));
}

/** Keep the user's timing (`lrc`), or drop it (null), a moment after the last
 * change: a run of taps or nudges is one request. */
function saveUserTiming(lrc) {
  if (!shownEntry) return;
  if (lrc == null) delete shownEntry.userSynced;
  else shownEntry.userSynced = lrc;
  userSave = { trackId: shownTrackId, entry: shownEntry, others: shownOthers, from: shownFrom, lrc };
  if (sync) sync.saveState.textContent = t("lyrics.sync.saving");
  clearTimeout(userSaveTimer);
  userSaveTimer = setTimeout(flushUserSave, SAVE_DELAY_MS);
}

function flushUserSave() {
  clearTimeout(userSaveTimer);
  userSaveTimer = 0;
  const save = userSave;
  userSave = null;
  if (!save) return userSaveChain;
  // In order: a removal sent after a save must land after it.
  userSaveChain = userSaveChain.then(() => sendUserTiming(save));
  return userSaveChain;
}

async function sendUserTiming({ trackId, entry, others, from, lrc }) {
  try {
    if (from === "server") {
      const url = `/api/jobs/${encodeURIComponent(trackId)}/lyrics/user-synced`;
      const res = lrc == null
        ? await fetch(url, { method: "DELETE", keepalive: true })
        : await fetch(url, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ synced: lrc }),
          keepalive: true,
        });
      // Nothing to remove is what a removal wanted.
      if (!res.ok && !(lrc == null && res.status === 404)) throw new Error(`HTTP ${res.status}`);
    } else {
      const kept = { ...entry };
      if (lrc == null) delete kept.userSynced;
      else kept.userSynced = lrc;
      await storeSet(storeKey(trackId), { entry: kept, others });
    }
    if (trackId === shownTrackId && sync && !userSave) sync.saveState.textContent = t("lyrics.sync.saved");
  } catch (err) {
    console.warn("lyrics timing save failed", err);
    if (trackId === shownTrackId && sync) {
      sync.saveState.textContent = "";
      setSyncMessage(t("lyrics.sync.saveFailed"), "error");
    }
  }
}

/**
 * Re-time from vocals: the server fits every line to the vocals stem (POST
 * .../lyrics/retime), which takes a while, so it is asked how it is getting
 * on until it is done. Done, its timing replaces what is shown and the user's
 * is dropped, which Undo brings back. Unsure or failed, nothing changes and
 * the panel says so.
 */
async function retimeFromVocals() {
  if (!sync || sync.retimeBtn.getAttribute("aria-busy") === "true") return;
  const trackId = shownTrackId;
  const from = shownFrom;
  const entry = shownEntry;
  const token = ++retimeToken;
  const base = `/api/jobs/${encodeURIComponent(trackId)}/lyrics/retime`;
  sync.retimeBtn.setAttribute("aria-busy", "true");
  setSyncMessage(t("lyrics.sync.retiming"), "loading");
  const live = () => token === retimeToken && trackId === shownTrackId && sync;
  const finish = (text, kind) => {
    if (!live()) return;
    sync.retimeBtn.removeAttribute("aria-busy");
    setSyncMessage(text, kind);
  };
  let started = false;
  try {
    const res = await fetch(base, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // Lyrics kept here are not in lyrics.json: they go along.
      body: JSON.stringify(from === "server" ? {} : { synced: entry?.synced || "", plain: entry?.plain || "" }),
    });
    started = res.ok;
    if (!started) console.warn("lyrics re-time refused", res.status);
  } catch (err) {
    console.warn("lyrics re-time failed", err);
  }
  if (!started) return finish(t("lyrics.sync.retimeFailed"), "error");

  let idle = 0;
  for (;;) {
    await new Promise((resolve) => setTimeout(resolve, RETIME_POLL_MS));
    if (!live()) return;
    let status = null;
    try {
      const res = await fetch(base);
      if (res.ok) status = await res.json();
    } catch (err) {
      console.warn("lyrics re-time status failed", err);
    }
    if (!live()) return;
    const state = status?.state;
    if (state === "running" || (state === "idle" && ++idle <= RETIME_IDLE_POLLS)) {
      const p = Number(status?.progress);
      const percent = Number.isFinite(p) && p > 0 ? Math.round(p <= 1 ? p * 100 : p) : 0;
      setSyncMessage(percent ? t("lyrics.sync.retimingProgress", { percent }) : t("lyrics.sync.retiming"), "loading");
      continue;
    }
    if (state === "unsure") return finish(t("lyrics.sync.retimeUnsure"), "muted");
    if (state !== "done") return finish(t("lyrics.sync.retimeFailed"), "error");
    // Lyrics.json holds the new timing; lyrics kept here get it in the answer.
    const aligned = from === "server"
      ? (await serverLyrics(trackId))?.entry?.aligned ?? null
      : alignedFrom(status.aligned);
    if (!live()) return;
    const next = aligned ? parseLrc(aligned.synced) : [];
    if (!next.length) return finish(t("lyrics.sync.retimeFailed"), "error");
    entry.aligned = aligned;
    sync.session.replace(next);
    sync.session.meta.kind = "aligned";
    syncChanged();
    // Kept here: the new timing is saved with the entry (the user's own
    // timing went with the change above).
    if (from === "store") {
      storeSet(storeKey(trackId), { entry: { ...entry }, others: shownOthers })
        .catch((err) => console.warn("lyrics timing save failed", err));
    }
    return finish(aligned.lines
      ? t("lyrics.sync.retimed", { matched: aligned.linesMatched, count: aligned.lines })
      : t("lyrics.sync.retimedPlain"));
  }
}

/** Whether a key press is typing into a field, which Sync lines leaves be. */
function isTyping(target) {
  if (target instanceof HTMLInputElement) return !["checkbox", "radio", "button"].includes(target.type);
  return target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement || Boolean(target?.isContentEditable);
}

/** Whether an editor, dialog or popover other than the Lyrics panel's own is
 * open, and so owns Escape and undo. */
function anotherEditorOpen() {
  if (document.querySelector('#t-metro-edit[aria-pressed="true"], dialog[open]')) return true;
  const panel = document.getElementById("lyricsPanel");
  // The app's dialogs are role="dialog" elements, hidden by a class or
  // removed when closed, not native <dialog>s: one showing owns the keys.
  const modals = document.querySelectorAll('[role="dialog"][aria-modal="true"]');
  if ([...modals].some((el) => el.getClientRects().length > 0 && !panel?.contains(el))) return true;
  // Popups only: a button that opens a menu or panel and has it open. The
  // sidebar's collapse button is aria-expanded whenever the sidebar shows.
  return [...document.querySelectorAll('[aria-haspopup][aria-expanded="true"]')].some((el) => !panel?.contains(el));
}

/** The Sync lines keys, while the mode is on. On the window, ahead of every
 * other handler: Backspace also moves the open track to the Trash, and the
 * arrows would scroll the list. */
function onSyncKey(e) {
  if (!sync || !visible || isTyping(e.target)) return;
  const mod = e.ctrlKey || e.metaKey;
  // The keyboard belongs to whatever else is open on top: the beat-grid
  // editor, a dialog, a menu or panel outside the Lyrics panel. Heard first
  // here (capture), Escape would close Sync lines and leave that stuck open,
  // undo would undo the lyrics instead of the grid, and the arrows would move
  // the lyrics instead of the menu.
  if (anotherEditorOpen()) return;
  let handled = true;
  if (mod && e.code === "KeyZ") {
    if (e.shiftKey) redoSync();
    else undoSync();
  } else if (mod && e.code === "KeyY") {
    redoSync();
  } else if (mod || e.altKey) {
    handled = false;
  } else if (e.code === "KeyT") {
    tapNow();
  } else if (e.key === "Enter") {
    // Enter on another button presses that button.
    const control = e.target?.closest?.("button, a, [role='button']");
    if (control && control !== sync.tap) handled = false;
    else tapNow();
  } else if (e.key === "Backspace") {
    undoSync();
  } else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
    nudgeLine((e.key === "ArrowLeft" ? -1 : 1) * (e.shiftKey ? NUDGE_COARSE : NUDGE_FINE));
  } else if (e.key === "ArrowUp" || e.key === "ArrowDown") {
    const at = Math.min(sync.session.cursor, lines.length);
    selectLine(Math.max(0, Math.min(lines.length - 1, at + (e.key === "ArrowUp" ? -1 : 1))));
  } else if (e.key === "Escape") {
    closeSync(true);
  } else {
    handled = false;
  }
  if (handled) {
    e.preventDefault();
    e.stopImmediatePropagation();
  }
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
  // Another version, at its own timing: the offset and the timing line by
  // line were the old one's.
  delete entry.offsetSec;
  delete entry.userSynced;
  delete entry.aligned;
  await dropStoreSave();
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
  await dropStoreSave();
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
/**
 * Whether lyrics this tab kept for a track are another song's. Before a lookup
 * held a version to the track's own song and artist, a fuzzy search could keep
 * another song of a similar name (NIHIL "Barro" got "Joao de Barro"). Only the
 * song is held to it here: the artist of a version the user picked on purpose,
 * a cover say, is their choice.
 */
function keptForAnotherSong(entry, info) {
  if (entry?.source !== "lrclib") return false;
  const { song } = knownSong(info);
  if (!song || !entry.track) return false;
  const titles = [song, info.identity?.title, ...(info.identity?.titleAliases || [])].filter(Boolean);
  return !titles.some((title) => sameSong(entry.track, title));
}

async function loadForCurrentTrack() {
  const info = getCurrentTrackInfo();
  // The Align panel stays open while the same track is shown again.
  if ((info?.id ?? null) !== shownTrackId) {
    alignOpen = false;
    syncOpen = false;
  }
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
  if (saved?.entry && !keptForAnotherSong(saved.entry, info)) {
    show(saved.entry, Array.isArray(saved.others) ? saved.others : []);
    return;
  }
  if (saved?.entry) {
    // Kept by the tab before a lookup was held to the track's own song:
    // another song's lyrics, dropped so the track is looked up again.
    await storeSet(storeKey(info.id), null);
    if (token !== loadToken) return;
  }

  // What the server found while the track was separated. Shown, not saved:
  // picking another version is what saves one, as the user's own choice.
  const found = await serverLyrics(info.id);
  if (token !== loadToken) return;
  if (found?.entry) {
    show(found.entry, found.others, "server");
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
    const time = transport()?.getCurrentTime?.() ?? 0;
    const now = time + 0.05;
    const index = currentLineIndex(lines, now);
    if (index !== currentIndex) moveTo(index);
    fillWords(index, now);
    // One transform, and only when playback has moved.
    if (sync) setLanePlayhead(time);
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
  if (sync) setLaneCurrent(index);
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
  // Sync lines works with the list in view; another view of the sidebar
  // leaves it, and what it set is saved.
  if (!on) closeSync(false);
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

  // An offset or a timing still waiting to be saved goes before the page does.
  window.addEventListener("pagehide", () => {
    flushSave();
  });
  // Capture, on the window: ahead of the app's own keys (see onSyncKey).
  window.addEventListener("keydown", onSyncKey, true);
  document.addEventListener("catalogviewchange", (e) => setVisible(e.detail?.view === "lyrics"));
  document.addEventListener("tracktags", (e) => {
    if (visible && nothingKnownFor && e.detail?.id === nothingKnownFor) loadForCurrentTrack();
  });
}
