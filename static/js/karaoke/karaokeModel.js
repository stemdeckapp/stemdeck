// Karaoke Mode's arithmetic, kept apart from the page so it can be tested in
// Node: speeds and their presets, key names after a transpose, the section
// under the playhead, lyric lines grouped into the two-line phrases the stage
// shows, and which stems get a card. Nothing here holds state; karaoke.js
// computes all of it from the player's own state every frame.

import { currentLineIndex, wordTimings } from "../lyricsLookup.js";

// ── Speed ──

// The range transport.setSpeed clamps to (transport.js SPEED_MIN/MAX): kept
// here as numbers because this module runs in Node, where transport.js,
// which reaches the page at import, cannot be loaded.
export const SPEED_MIN = 0.5;
export const SPEED_MAX = 1.5;
export const SPEED_STEP = 0.05;
export const SPEED_PRESETS = Object.freeze([0.5, 0.75, 1, 1.25, 1.5]);

const hundredths = (x) => Math.round(x * 100) / 100;

/** `rate` held to the range, in hundredths. */
export function clampSpeed(rate) {
  const r = Number(rate);
  return hundredths(Math.min(SPEED_MAX, Math.max(SPEED_MIN, Number.isFinite(r) ? r : 1)));
}

/** One 5% step slower (dir -1) or faster (dir 1) than `rate`. */
export function stepSpeed(rate, dir) {
  return clampSpeed(clampSpeed(rate) + (dir < 0 ? -SPEED_STEP : SPEED_STEP));
}

/** The preset `rate` is exactly, or null for any other speed. */
export function activePreset(rate) {
  const r = clampSpeed(rate);
  return SPEED_PRESETS.find((p) => Math.abs(p - r) < 1e-6) ?? null;
}

/** "100%", "75%", "125%". */
export function speedPercent(rate) {
  return `${Math.round(clampSpeed(rate) * 100)}%`;
}

// ── Key ──

// From A, the names the design uses: flats for the black keys except C# and F#.
const ROOTS = ["A", "B♭", "B", "C", "C♯", "D", "E♭", "E", "F", "F♯", "G", "A♭"];
const LETTER = { A: 0, B: 2, C: 3, D: 5, E: 7, F: 8, G: 10 };

/**
 * A detected key as StemDeck writes it ("E min", "F# maj", "B♭ minor",
 * "C major"), as { root: 0..11 from A, minor }, or null when there is none.
 */
export function parseKey(text) {
  const m = /^\s*([A-Ga-g])\s*([#♯b♭]?)\s*(maj(?:or)?|min(?:or)?|m)?\b/i.exec(String(text || ""));
  if (!m) return null;
  let root = LETTER[m[1].toUpperCase()];
  if (m[2] === "#" || m[2] === "♯") root += 1;
  if (m[2] === "b" || m[2] === "♭") root -= 1;
  const q = (m[3] || "").toLowerCase();
  return { root: (root + 12) % 12, minor: q.startsWith("min") || q === "m" };
}

/** The key after moving `semitones`: { root: "B♭", minor } or null. */
export function transposedKey(text, semitones = 0) {
  const k = parseKey(text);
  if (!k) return null;
  const n = Math.round(Number(semitones) || 0);
  return { root: ROOTS[(((k.root + n) % 12) + 12) % 12], minor: k.minor };
}

/** "0", "+2", "−1" (a real minus sign, as the design shows it). */
export function semitoneLabel(n) {
  const v = Math.round(Number(n) || 0);
  return v === 0 ? "0" : v > 0 ? `+${v}` : `−${Math.abs(v)}`;
}

// ── Time ──

const pad = (n) => String(n).padStart(2, "0");

/** "0:48": a section's start, the countdown. */
export function clockShort(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  return `${Math.floor(s / 60)}:${pad(s % 60)}`;
}

/** "03:06": the progress bar's times. */
export function clockLong(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  return `${pad(Math.floor(s / 60))}:${pad(s % 60)}`;
}

// ── Lyrics ──

/** The sung lines: timed, with words (an LRC instrumental gap is an empty line). */
export function sungLines(lines) {
  return (lines || []).filter((l) => Number.isFinite(l?.time) && String(l.text || "").trim() && String(l.text).trim() !== "♪");
}

/**
 * What the stage shows at `t`, from the sung lines grouped two by two into
 * phrases (the design's current two lines, the next two under them, the line
 * before above):
 * - { state: "none" } with no lines,
 * - { state: "intro", firstAt, next: [a, b] } before the first line,
 * - { state: "lyric", phrase, current: [i, j?], prev, next: [a, b?] } after,
 * where current holds indices into `lines` and the rest is text.
 */
export function lyricFrame(lines, t) {
  if (!lines?.length) return { state: "none" };
  const text = (i) => (i >= 0 && i < lines.length ? lines[i].text : "");
  const pair = (p) => [2 * p, 2 * p + 1].filter((i) => i < lines.length);
  if (t < lines[0].time) {
    return { state: "intro", firstAt: lines[0].time, next: pair(0).map(text) };
  }
  const i = Math.max(0, currentLineIndex(lines, t));
  const phrase = Math.floor(i / 2);
  return {
    state: "lyric",
    phrase,
    current: pair(phrase),
    prev: text(2 * phrase - 1),
    next: pair(phrase + 1).map(text),
  };
}

/** Each sung line's words with their start times, by the Lyrics tab's own
 * timing (word stamps where the lyrics have them, else the estimate from the
 * line's time and the vocals' level). */
export function timedWords(lines, envelope = null) {
  return lines.map((line, i) => wordTimings(line, lines[i + 1]?.time ?? null, envelope));
}

/** How many of a line's words have started by `t`. */
export function sungCount(words, t) {
  let n = 0;
  for (const w of words || []) if (w.start <= t + 1e-6) n += 1;
  return n;
}

// ── Stems ──

const ORDER = ["original", "lead_vocals", "backing_vocals", "vocals"];

/** Stems that always play in Karaoke: Original and the extracted instruments.
 * They have no switch there; only the voice does. */
const ALWAYS_ON = ["original", "drums", "bass", "guitar", "piano", "other"];

export function karaokeAlwaysOn(name) {
  return ALWAYS_ON.includes(name);
}

/**
 * The stem cards for the lanes a song has, always in this order: Original on
 * the left, then the voice (Lead and Backing Vocals when the split made
 * both, else one Vocals card). There are no instrument cards: the only
 * switches are the voice and the backing vocals. Missing stems get no card;
 * recorded tracks get none.
 *
 * Original is a card because it is a stem: the sum of every stem that was
 * NOT extracted (pipeline/collect.py make_original_track). Extract only the
 * vocals and Original is the whole backing track, the one thing a karaoke
 * song needs to play. It is absent when every stem was extracted.
 */
export function karaokeStems(present) {
  const has = (n) => present.has(n);
  const split = has("lead_vocals") && has("backing_vocals");
  return ORDER.filter((n) => has(n) && (n === "vocals" ? !split : n === "lead_vocals" || n === "backing_vocals" ? split : true));
}

/** On entering Karaoke Mode: the sung part off (Lead Vocals, or the one Vocals
 * card); Original and the instruments always on, Backing Vocals on. Returns
 * whether `name` is muted by default. */
export function karaokeMutedByDefault(name) {
  return name === "lead_vocals" || name === "vocals";
}

// ── Queue ──
//
// A list of song ids, first to play first. A song is in it once: adding one
// that is there moves it. Pure functions; karaokeQueue.js keeps the list.

/** `id` placed at `index` (counted without it, clamped; the end when omitted):
 * an add, or a move when it is already queued. */
export function queueInsert(list, id, index) {
  const rest = (list || []).filter((x) => x !== id);
  const i = Number.isInteger(index) ? Math.max(0, Math.min(rest.length, index)) : rest.length;
  return [...rest.slice(0, i), id, ...rest.slice(i)];
}

export function queueRemove(list, id) {
  return (list || []).filter((x) => x !== id);
}

/** The list as stored or received: only strings, each once, only the songs
 * `exists` still knows (a song deleted from the library leaves the queue). */
export function queueClean(list, exists = () => true) {
  const seen = new Set();
  const out = [];
  for (const id of Array.isArray(list) ? list : []) {
    if (typeof id !== "string" || seen.has(id) || !exists(id)) continue;
    seen.add(id);
    out.push(id);
  }
  return out;
}

/** Where a drop at height `y` lands among rows ({ top, height } each, in
 * order): before the first row whose middle is below `y`, else at the end. */
export function dropIndex(rows, y) {
  for (let i = 0; i < rows.length; i++) {
    if (y < rows[i].top + rows[i].height / 2) return i;
  }
  return rows.length;
}
