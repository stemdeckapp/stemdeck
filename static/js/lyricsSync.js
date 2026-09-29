// Timing synced lyrics line by line, for the Lyrics tab's Sync lines mode.
//
// A track's lyrics can be timed three ways, and the one in effect is, first
// found:
//   1. the user's own, set line by line here ("user_synced", or userSynced
//      in the store entry of lyrics kept in the browser);
//   2. the server's, fitted line by line to the vocals stem ("aligned");
//   3. the lyrics' own timing, moved as a whole by the Align panel's offset.
// timingOf() picks it. Everything that times a line or a word (the list, the
// karaoke wipe, a line click, the lane over the waveform) reads its lines.
//
// SyncSession is the editor: one line in hand (the cursor), taps that set it
// to the playhead and move on, drags and nudges that move one line, and an
// undo history over all of them. Lines never cross: a line dragged is held
// between its neighbours, a line tapped pushes the lines it would pass.
//
// No DOM here, so node runs it (tests/js/lyrics-sync.test.mjs).

import { parseLrc, shiftLines, clampOffset, voicedPhrases } from "./lyricsLookup.js";

// Two lines are never closer than this, so a line dragged onto its neighbour
// still starts after it and both stay reachable. More than the 50ms the
// Lyrics tab looks ahead to mark the line being sung (lyrics.js tick), or a
// click on the line before would mark this one.
export const MIN_GAP_SEC = 0.1;
// A line's last word keeps at least this long before the next line starts
// when the line's words are squeezed to fit.
const MIN_LAST_WORD_SEC = 0.15;
// How far a tap or a drop looks for the singing's start to snap to.
export const SNAP_REACH_SEC = 0.3;
// Undo keeps this many steps.
const HISTORY_MAX = 300;

/**
 * The timing in effect for a kept entry: { kind, lines, offset }. `kind` is
 * "user" (set here by hand), "aligned" (fitted to the vocals by the server) or
 * "offset" (the lyrics' own, moved by `offset` seconds). `lines` is [] when
 * there is no timing at all (plain lyrics nothing has timed).
 */
export function timingOf(entry) {
  const user = typeof entry?.userSynced === "string" ? parseLrc(entry.userSynced) : [];
  if (user.length) return { kind: "user", lines: user, offset: 0 };
  const aligned = typeof entry?.aligned?.synced === "string" ? parseLrc(entry.aligned.synced) : [];
  if (aligned.length) return { kind: "aligned", lines: aligned, offset: 0 };
  const offset = clampOffset(entry?.offsetSec);
  return { kind: "offset", lines: shiftLines(parseLrc(entry?.synced || ""), offset), offset };
}

/** Seconds as an LRC stamp's inside, "mm:ss.xx"; never negative. */
export function formatStamp(seconds) {
  const cs = Math.max(0, Math.round((Number(seconds) || 0) * 100));
  const m = Math.floor(cs / 6000);
  const s = Math.floor((cs % 6000) / 100);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}.${String(cs % 100).padStart(2, "0")}`;
}

/**
 * Lines as LRC, one stamp per line, earliest first, and enhanced LRC's word
 * stamps for a line that has them: what PUT .../lyrics/user-synced keeps, and
 * what parseLrc reads back to the same lines.
 */
export function serializeLrc(lines) {
  return lines
    .map((line) => {
      const head = `[${formatStamp(line.time)}]`;
      if (line.words?.length) return head + line.words.map((w) => `<${formatStamp(w.time ?? line.time)}>${w.text}`).join("").trimEnd();
      return line.text ? `${head} ${line.text}` : head;
    })
    .join("\n");
}

/**
 * A line's word stamps squeezed, in proportion, to end before `limit` (the
 * next line's start): a line moved closer to the next one keeps its words in
 * the same shape, only faster. Lines without word stamps, or with room, are
 * returned as they are.
 */
export function fitWords(line, limit) {
  if (!line.words?.length || limit == null) return line;
  const rel = line.words.map((w) => Math.max(0, (w.time ?? line.time) - line.time));
  const reach = Math.max(...rel);
  const room = Math.max(0, limit - line.time - MIN_LAST_WORD_SEC);
  if (reach <= room || reach <= 0) return line;
  const k = room / reach;
  return { ...line, words: line.words.map((w, i) => ({ ...w, time: line.time + rel[i] * k })) };
}

/** `line` at `time`, its words moved with it. */
function atTime(line, time) {
  const delta = time - line.time;
  if (!delta) return line;
  return {
    ...line,
    time,
    ...(line.words ? { words: line.words.map((w) => ({ ...w, time: (w.time ?? line.time) + delta })) } : {}),
  };
}

/**
 * `lines` with line `index` at `time`, as a new array; `lines` is left alone.
 *
 * `mode` says what the other lines do:
 *   "clamp"  - nothing; the line is held between its neighbours (a drag, a
 *              nudge);
 *   "push"   - the lines it would pass are pushed ahead of it, later ones
 *              later and earlier ones earlier (a tap);
 *   "ripple" - the lines after it move by as much, up to the first of
 *              `fixed` (indexes already set this session, which stay), then
 *              as "push".
 * `max` is the track's length, past which no line goes. The words of every
 * line that moved move with it, and are squeezed to fit where a line's next
 * one came closer.
 */
export function applyTime(lines, index, time, { mode = "clamp", fixed = null, max = Infinity } = {}) {
  const line = lines[index];
  if (!line || !Number.isFinite(time)) return lines;
  const out = lines.slice();
  const top = Number.isFinite(max) && max > 0 ? max : Infinity;
  let t = Math.min(top, Math.max(0, time));
  if (mode === "clamp") {
    const lo = index > 0 ? lines[index - 1].time + MIN_GAP_SEC : 0;
    const hi = index < lines.length - 1 ? lines[index + 1].time - MIN_GAP_SEC : top;
    t = Math.max(lo, Math.min(hi, t));
    // Neighbours closer than the gap already: stay where it was.
    if (lo > hi) t = line.time;
    out[index] = atTime(line, t);
  } else {
    const delta = t - line.time;
    out[index] = atTime(line, t);
    if (mode === "ripple" && delta) {
      for (let j = index + 1; j < lines.length; j++) {
        if (fixed?.has?.(j)) break;
        out[j] = atTime(lines[j], Math.min(top, Math.max(0, lines[j].time + delta)));
      }
    }
    // Nothing crosses the line set: later lines pushed, earlier ones pulled.
    for (let j = index + 1; j < out.length; j++) {
      const floor = out[j - 1].time + MIN_GAP_SEC;
      // No early way out: lines carried by a ripple can cross a line past them.
      if (out[j].time >= floor) continue;
      // Order wins over the track's end: a stamp past it is simply never reached.
      out[j] = atTime(out[j], floor);
    }
    for (let j = index - 1; j >= 0; j--) {
      const ceiling = out[j + 1].time - MIN_GAP_SEC;
      if (out[j].time <= ceiling) break;
      out[j] = atTime(out[j], Math.max(0, ceiling));
    }
  }
  // Words of a line whose next line came closer are squeezed to fit.
  for (let j = 0; j < out.length; j++) {
    const moved = out[j] !== lines[j] || out[j + 1]?.time !== lines[j + 1]?.time;
    if (moved && out[j].words) out[j] = fitWords(out[j], out[j + 1]?.time ?? null);
  }
  return out;
}

/**
 * Where the singing nearest `time` starts, within `reach` seconds, from the
 * vocals stem's level ({ hop, db }); `time` itself when there is none that
 * close, or no envelope.
 */
export function snapToVoice(envelope, time, reach = SNAP_REACH_SEC) {
  if (!envelope) return time;
  // A window wider than the reach, so a phrase that starts at its edge is a
  // real start and not where the window cut it.
  const from = Math.max(0, time - reach - 1.5);
  const phrases = voicedPhrases(envelope, from, time + reach + 1.5);
  let best = time;
  let bestOff = Infinity;
  for (const { start } of phrases) {
    if (start <= from + envelope.hop) continue;
    const off = Math.abs(start - time);
    if (off <= reach && off < bestOff) {
      best = start;
      bestOff = off;
    }
  }
  return best;
}

/** The first line after `index` with words, or lines.length when none. */
export function nextSungIndex(lines, index) {
  for (let j = index + 1; j < lines.length; j++) if (lines[j].text.trim()) return j;
  return lines.length;
}

/**
 * The editor's state: the lines being timed, the line in hand (`cursor`,
 * which a tap sets and the arrows move; lines.length once every line is
 * tapped), the lines set this session, and undo and redo over all of it.
 * Every change goes through here, so every change can be undone.
 */
export class SyncSession {
  constructor(lines, { ripple = false, max = Infinity, meta = {} } = {}) {
    this.lines = lines.slice();
    // The caller's own notes on each state (lyrics.js keeps which timing it
    // is), undone and redone with it.
    this.meta = { ...meta };
    this.ripple = ripple;
    this.max = max;
    this.touched = new Set();
    this.cursor = Math.max(0, nextSungIndex(this.lines, -1));
    if (this.cursor >= this.lines.length) this.cursor = 0;
    this.undoStack = [];
    this.redoStack = [];
  }

  get canUndo() { return this.undoStack.length > 0; }
  get canRedo() { return this.redoStack.length > 0; }
  get done() { return this.cursor >= this.lines.length; }

  snapshot() {
    return { lines: this.lines, cursor: this.cursor, touched: new Set(this.touched), meta: { ...this.meta } };
  }

  restore(snap) {
    this.lines = snap.lines;
    this.cursor = snap.cursor;
    this.touched = snap.touched;
    this.meta = snap.meta;
  }

  record() {
    this.undoStack.push(this.snapshot());
    if (this.undoStack.length > HISTORY_MAX) this.undoStack.shift();
    this.redoStack = [];
  }

  /** Put line `index` in hand. Not a change: nothing to undo. */
  select(index) {
    if (index >= 0 && index < this.lines.length) this.cursor = index;
  }

  /** Set line `index` to `time`, the lines around it as `mode` says (see
   * applyTime). False when nothing changed. */
  setTime(index, time, mode) {
    const next = applyTime(this.lines, index, time, { mode, fixed: this.touched, max: this.max });
    if (next === this.lines || next.every((line, j) => line.time === this.lines[j].time)) {
      // Still the line in hand, and still a step for undo when it was tapped.
      return false;
    }
    this.record();
    this.lines = next;
    this.touched.add(index);
    return true;
  }

  /** A tap: the line in hand starts at `time`, and the next sung line is in
   * hand. Taps push later lines ahead of them, or carry them along with
   * `ripple`, so a whole song running late follows the first tap. */
  tap(time) {
    if (this.done) return false;
    const index = this.cursor;
    const before = this.snapshot();
    const changed = this.setTime(index, time, this.ripple ? "ripple" : "push");
    // A tap on the time a line already has still moves on, and undo takes
    // the cursor back to it.
    if (!changed) {
      this.undoStack.push(before);
      this.redoStack = [];
      this.touched.add(index);
    }
    this.cursor = nextSungIndex(this.lines, index);
    return true;
  }

  /** Move line `index` to `time` without passing its neighbours (a drag), or
   * with the lines after it when `ripple` is on. */
  move(index, time) {
    return this.setTime(index, time, this.ripple ? "ripple" : "clamp");
  }

  nudge(index, delta) {
    const line = this.lines[index];
    return line ? this.move(index, line.time + delta) : false;
  }

  /** Replace every line at once (a reset, or new timing from the vocals). */
  replace(lines) {
    this.record();
    this.lines = lines.slice();
    this.touched = new Set();
    if (this.cursor > this.lines.length) this.cursor = this.lines.length;
  }

  undo() {
    const snap = this.undoStack.pop();
    if (!snap) return false;
    this.redoStack.push(this.snapshot());
    this.restore(snap);
    return true;
  }

  redo() {
    const snap = this.redoStack.pop();
    if (!snap) return false;
    this.undoStack.push(this.snapshot());
    this.restore(snap);
    return true;
  }
}
