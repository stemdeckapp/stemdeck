// The lyrics lane: a strip over the waveform lanes, shown while the Lyrics
// tab's Sync lines mode is on, with one marker per line at the moment it
// starts, labelled with its first words. A marker is dragged to move its line,
// clicked to take it in hand; the line being sung is lit as playback goes.
//
// Built like the Sections ribbon (sections.js): the markers sit at percentages
// of an inner track that is the zoomed waveform's width, and transport.js
// slides that track by the waveform's scroll (syncRulerScroll), so zooming and
// scrolling need nothing from here. The markers are only redrawn when a line
// moves, never per frame; the playhead is one transform.
//
// It holds no lyrics state of its own: lyrics.js hands it the lines and gets
// back what the pointer did.

import { t } from "./i18n.js";

let laneEl = null;
let trackEl = null;
let playheadEl = null;
let marks = [];
let duration = 0;
let lines = [];
let selected = -1;
let current = -1;
let handlers = {};
let lastPlayhead = -1;

/** A pixel of pointer travel that still counts as a click, not a drag. */
const CLICK_SLOP_PX = 3;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

/** What a marker says: the line's first words, or a note for a gap. */
function labelOf(line) {
  const text = String(line?.text || "").trim();
  return text || "♪";
}

/**
 * Show the lane for `nextLines` over a track `trackDuration` seconds long.
 * `on` holds the callbacks: select(index), drag(index, seconds, { free })
 * while a marker moves (returns the lines to draw meanwhile), drop(index,
 * seconds, { free }) when it is let go, and done() for the lane's Done.
 */
export function showLane(nextLines, trackDuration, on = {}) {
  laneEl = document.getElementById("lyricsLane");
  trackEl = document.getElementById("daw-lyrics-track");
  if (!laneEl || !trackEl) return false;
  handlers = on;
  duration = Math.max(0.001, Number(trackDuration) || 0);
  laneEl.classList.remove("hidden");
  // Where the waveform is scrolled to now; transport.js keeps it in step
  // from here on.
  const wave = document.getElementById("wave-scroll");
  trackEl.style.transform = `translateX(${-(wave?.scrollLeft || 0)}px)`;
  const done = document.getElementById("lyricsLaneDone");
  if (done && !done.dataset.wired) {
    done.dataset.wired = "1";
    done.addEventListener("click", (e) => {
      handlers.done?.();
      if (e.detail > 0) done.blur();
    });
  }
  marks = [];
  trackEl.replaceChildren();
  playheadEl = el("div", "lyr-lane-playhead");
  playheadEl.setAttribute("aria-hidden", "true");
  playheadEl.append(el("i"));
  trackEl.append(playheadEl);
  lastPlayhead = -1;
  current = -1;
  updateLane(nextLines, selected);
  return true;
}

export function hideLane() {
  laneEl?.classList.add("hidden");
  trackEl?.replaceChildren();
  marks = [];
  lines = [];
  selected = -1;
  current = -1;
  handlers = {};
}

/**
 * Draw `nextLines` with line `nextSelected` in hand. Markers are made once per
 * line and after that only moved, so a drag rewrites a few styles.
 */
export function updateLane(nextLines, nextSelected = selected) {
  if (!trackEl) return;
  lines = nextLines;
  if (marks.length !== lines.length) {
    for (const mark of marks) mark.remove();
    marks = lines.map((line, i) => makeMark(i));
    trackEl.append(...marks);
  }
  lines.forEach((line, i) => {
    const mark = marks[i];
    const start = Math.max(0, Math.min(duration, line.time));
    const end = Math.max(start, Math.min(duration, lines[i + 1]?.time ?? duration));
    const label = labelOf(line);
    // Only what moved is written: a drag touches two markers, not all of them.
    const drawn = mark._drawn;
    if (drawn && drawn.start === start && drawn.end === end && drawn.label === label) return;
    mark._drawn = { start, end, label };
    mark.style.left = `${((start / duration) * 100).toFixed(4)}%`;
    mark.style.width = `${(((end - start) / duration) * 100).toFixed(4)}%`;
    const text = mark.firstChild;
    if (text.textContent !== label) text.textContent = label;
    mark.setAttribute("aria-label", t("lyrics.sync.markAria", { line: label, time: formatClock(line.time) }));
  });
  setLaneSelected(nextSelected);
}

export function setLaneSelected(index) {
  marks[selected]?.classList.remove("selected");
  selected = index;
  marks[selected]?.classList.add("selected");
}

export function setLaneCurrent(index) {
  if (index === current) return;
  marks[current]?.classList.remove("current");
  current = index;
  marks[current]?.classList.add("current");
}

/** Where playback is, as one transform: no layout. Skipped when unchanged. */
export function setLanePlayhead(seconds) {
  if (!playheadEl || seconds === lastPlayhead) return;
  lastPlayhead = seconds;
  const pct = Math.max(0, Math.min(100, (seconds / duration) * 100));
  playheadEl.style.transform = `translateX(${pct}%)`;
}

/** Scroll the waveform so the marker in hand is on screen, when it is not. */
export function revealLaneSelected() {
  const mark = marks[selected];
  const wave = document.getElementById("wave-scroll");
  if (!mark || !wave || wave.scrollWidth <= wave.clientWidth) return;
  const at = (lines[selected].time / duration) * wave.scrollWidth;
  if (at >= wave.scrollLeft + 24 && at <= wave.scrollLeft + wave.clientWidth - 24) return;
  wave.scrollLeft = Math.max(0, Math.min(at - wave.clientWidth / 3, wave.scrollWidth - wave.clientWidth));
}

/** "1:04.25": the lane's own readout, minutes, seconds, hundredths. */
export function formatClock(seconds) {
  const cs = Math.max(0, Math.round((Number(seconds) || 0) * 100));
  const m = Math.floor(cs / 6000);
  const s = Math.floor((cs % 6000) / 100);
  return `${m}:${String(s).padStart(2, "0")}.${String(cs % 100).padStart(2, "0")}`;
}

function makeMark(index) {
  const mark = el("button", "lyr-mark");
  mark.type = "button";
  // The keys reach the lines through the Lyrics tab; a Tab stop per line here
  // would be dozens of stops on the way to the mixer.
  mark.tabIndex = -1;
  mark.dataset.index = String(index);
  mark.append(el("span", "lyr-mark-label"));
  wireDrag(mark, index);
  return mark;
}

function wireDrag(mark, index) {
  let active = false;
  let startX = 0;
  let origin = 0;
  let moved = false;
  let at = 0;

  const timeAt = (clientX) => {
    const width = trackEl.getBoundingClientRect().width;
    if (!width) return origin;
    return origin + ((clientX - startX) / width) * duration;
  };

  mark.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    active = true;
    moved = false;
    startX = e.clientX;
    origin = lines[index]?.time ?? 0;
    at = origin;
    handlers.select?.(index);
    mark.setPointerCapture(e.pointerId);
    mark.classList.add("dragging");
    e.preventDefault();
  });

  mark.addEventListener("pointermove", (e) => {
    if (!active) return;
    if (!moved && Math.abs(e.clientX - startX) < CLICK_SLOP_PX) return;
    moved = true;
    at = timeAt(e.clientX);
    const preview = handlers.drag?.(index, at, { free: e.altKey });
    if (preview) updateLane(preview, index);
  });

  const finish = (e, cancelled) => {
    if (!active) return;
    active = false;
    mark.classList.remove("dragging");
    if (cancelled) {
      handlers.drop?.(index, origin, { free: true, cancelled: true });
      return;
    }
    if (moved) handlers.drop?.(index, timeAt(e.clientX), { free: e.altKey });
    else handlers.click?.(index);
  };
  mark.addEventListener("pointerup", (e) => finish(e, false));
  mark.addEventListener("pointercancel", (e) => finish(e, true));
}
