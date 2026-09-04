import { fmtTime, fmtTickLabel, fmtTimeMs, parseTimecode, storeGet, storeSet } from "./utils.js";
import { MIN_LOOP_SEC, loopDragResult } from "./loopRegion.js";
import {
  playBtn, playMiniBtn, stopBtn, loopBtn, timeEl, masterFader,
  speedBtns,
  pitchDownBtn,
  pitchUpBtn,
  pitchValueEl,
  pitchResetBtn,
  pitchWrap,
  speedWrap,
  rulerTime, wavesGrid, loopRegionEl, playheadMarker,
  multitrack, audioEngine, totalDuration, loopEnabled, loopStart, loopEnd, masterVolume,
  waveScroll, waveCanvas, multitrackContainer,
  presenceRulerEl, presencePlayheadEl,
  footerTimeElapsed, footerTimeTotal, footerWaveTicks, npScrubFill, footerWaveDrawFn,
  loopStartInput, loopEndInput,
  metroBtn, metroPanel, metroVolEl, metroVolLabel, metroBarEl, metroNoteEl,
  metroHalfBtn, metroOneBtn, metroDoubleBtn, metroCountInEl,
  metronome, metronomeEnabled, metronomeVolume, metronomeBeatsPerBar, metronomeHasBars,
  metronomeCountIn, setMetronomeCountIn,
  setMetronomeHasBars,
  setMetronomeEnabled, setMetronomeVolume, setMetronomeBeatsPerBar,
  setLoopEnabled, setLoopStart, setLoopEnd, setMasterVolume, setPlaybackSpeed,
  waveZoom, setWaveZoom, overviewRerenderFn,
} from "./state.js";
import { applyMix, nudgeAllLanePitches, resetAllLanePitches } from "./mixer.js";
import { isDownbeatIndex, getBeats as getGridBeats, getBars as getGridBars } from "./beatgrid.js";
import { computeCountIn } from "./metronome.js";
import { t } from "./i18n.js";
import { pitchBlockedKey } from "./pitchBus.js";

// Zoom range. 1 is the whole track fitted to the panel; there is nothing below
// it to show, so it is the floor rather than a soft default.
//
// The ceiling is set by how many points back the picture, not by taste. A bar
// occupies OVERVIEW_BAR_SLOT_PX, so a panel of W pixels draws W/5 bars at 1x
// and Z times that at Zx. Ask for more bars than there are points and the
// extra ones repeat their neighbours: a fatter picture, not a closer one,
// which is the exact failure this feature exists to avoid.
//
// 10x needs roughly 2400 points for a full-width panel, so _PEAK_POINTS in
// app/pipeline/collect.py carries 3000. Raise them together or not at all.
//
// Tracks separated before that change kept 1500-point peaks.json and repeat
// bars past 5x on the streaming path. The Web Audio path is unaffected either
// way: it scans the decoded buffer itself, at whatever this ceiling asks for.
const WAVE_ZOOM_MIN = 1;
export const WAVE_ZOOM_MAX = 10;
// One wheel notch. Multiplicative, so a notch covers the same proportion of the
// range at 1x as at 4x; linear steps feel fast at the bottom and stuck at the top.
const WAVE_ZOOM_STEP = 1.18;

// The handles that drag audio out to the OS. Both run an HTML5 drag, and any
// pointer handler that calls preventDefault on their pointerdown stops that
// drag before it starts. Excluded from both loop gestures for that reason.
const DRAG_OUT_SELECTOR = "[data-loop-drag-out], [data-lane-drag-out]";
// Below this visible width the waveform stops compressing to fit and instead
// keeps a minimum size, overflowing horizontally so .wave-scroll can scroll.
const WAVE_MIN_WIDTH = 720;
// rulerTime is the canonical timeline reference for both click->time
// and time->pixel mapping. The wave-editor lays the ruler and the
// waveform body out so they should be horizontally aligned (both gutter
// 48 px on the left in studio mode), but using one element for both
// halves of the round-trip eliminates any subtle CSS drift -- clicking
// "1:00" on the ruler always lands a marker exactly under that tick,
// regardless of how the waves layer below happens to size itself.
function rulerRect() {
  return rulerTime?.getBoundingClientRect() || { left: 0, width: 1 };
}

function loopOverlayParent() {
  return document.querySelector(".waves-column") || rulerTime;
}

function ensureLoopRegionParent() {
  const parent = loopOverlayParent();
  if (parent && loopRegionEl.parentElement !== parent) {
    parent.appendChild(loopRegionEl);
  }
}

function timeFromClientX(clientX) {
  if (!totalDuration) return null;
  const rect = rulerRect();
  const x = clientX - rect.left;
  const frac = Math.max(0, Math.min(1, x / Math.max(1, rect.width)));
  return frac * totalDuration;
}

/// The clock that actually owns playback.
///
/// engineMode() defaults to "chunked", where audioEngine drives audio and the
/// multitrack is mounted with url: null for visuals only -- so operating on
/// `multitrack` directly moves nothing and reads 0. Everything in this module
/// already went through `audioEngine ?? multitrack`; exporting it stops other
/// modules re-deriving it and drifting (#515).
export function transport() {
  return audioEngine ?? multitrack;
}

export function setPlayheadTime(sec) {
  const tx = audioEngine ?? multitrack;
  if (!tx || !totalDuration) return;
  const next = Math.max(0, Math.min(totalDuration, sec));
  tx.setTime(next);
  updatePlayheadMarker(next);
  updateFooterTimes(next);
  updatePresencePlayhead(next);
}

// Spacing of the timeline's labelled ticks. Shared by the ruler above the
// lanes and the one on the footer waveform: the two strips are the same width
// and start at the same x, so a time has to land at the same place in both.
// Label spacing the ruler will not go below, comfortably wider than a "10:00"
// label so neighbours never crowd each other.
const MIN_TICK_PX = 110;
const TICK_LADDER = [1, 2, 5, 10, 15, 30, 60, 120, 300];

// `contentWidthPx` is the width the ticks will actually occupy. Omitted (the
// footer strip, which always shows the whole track) the step is the plain
// duration-based one, which is also what 1x has always used.
// The step the ruler was last built with. buildRuler mutates elements inside
// .wave-scroll, which is the element the resize observer watches, so rebuilding
// unconditionally from that callback can re-trigger it. Comparing against this
// makes the rebuild idempotent: once the ruler matches the width, it settles.
let _rulerStep = 0;

function tickStep(durationSec, contentWidthPx = 0) {
  const base = durationSec < 90 ? 15 : durationSec < 300 ? 30 : 60;
  // Zoom is the only thing that subdivides it. Spreading the same handful of
  // ticks across five screen widths would make the ruler less useful the
  // further in you went, which is backwards.
  if (waveZoom <= 1 || !contentWidthPx || !durationSec) return base;
  const pxPerSec = contentWidthPx / durationSec;
  for (const step of TICK_LADDER) {
    if (step > base) break;
    if (step * pxPerSec >= MIN_TICK_PX) return step;
  }
  return base;
}

export function buildRuler(durationSec) {
  rulerTime.innerHTML = "";
  wavesGrid.innerHTML = "";
  const marker = document.createElement("div");
  marker.className = "playhead-marker";
  marker.setAttribute("aria-hidden", "true");
  marker.innerHTML =
    '<svg viewBox="0 0 10 10" width="10" height="10"><polygon points="0,0 10,0 5,8" fill="#e54e4e"></polygon></svg>';
  rulerTime.appendChild(marker);

  if (!durationSec || durationSec <= 0) return;
  // The ruler is width: calc(100% * var(--zoom)), so its own box already is the
  // zoomed width; no need to recompute it here.
  const step = tickStep(durationSec, rulerTime.getBoundingClientRect().width);
  _rulerStep = step;
  for (let t = 0; t <= durationSec; t += step) {
    const leftPct = (t / durationSec) * 100;
    const tick = document.createElement("div");
    tick.className = "tick";
    tick.style.left = `${leftPct}%`;
    tick.innerHTML = `<span class="tick-label">${fmtTickLabel(t)}</span>`;
    rulerTime.appendChild(tick);

    const grid = document.createElement("div");
    grid.className = "grid-line";
    grid.style.left = `${leftPct}%`;
    wavesGrid.appendChild(grid);
  }
}

export function updatePlayheadMarker(currentSec) {
  if (!playheadMarker || !totalDuration) return;
  const m = rulerTime.querySelector(".playhead-marker");
  if (m) {
    // Position relative to the ruler itself (the marker is a ruler
    // child) so the playhead always sits exactly under the tick at
    // the matching time. Use percent instead of px: app-level CSS
    // zoom scales getBoundingClientRect() values, while left/width
    // styles are interpreted in unzoomed layout pixels.
    const pct = Math.max(0, Math.min(100, (currentSec / totalDuration) * 100));
    m.style.left = `${pct}%`;
  }
}

// Mirror the elapsed/total time into the transport-footer's two side
// labels (which used to show hardcoded "00:00.000" / "03:38.000") and
// drive the small scrub bar in the now-playing card. Driven from the
// same wavesurfer "timeupdate" event that already updates #t-time, so
// every label stays in sync without extra event plumbing.
export function updateFooterTimes(currentSec) {
  if (!totalDuration) return;
  if (footerTimeElapsed) footerTimeElapsed.textContent = fmtTime(currentSec);
  if (footerTimeTotal) footerTimeTotal.textContent = fmtTime(totalDuration);
  const pct = Math.max(0, Math.min(100, (currentSec / totalDuration) * 100));
  if (npScrubFill) npScrubFill.style.width = `${pct}%`;
  footerWaveDrawFn?.(pct / 100);
}

// Time labels above the footer waveform. Same ticks as the ruler over the
// lanes, positioned the same way (percent of duration), because the footer
// strip is now indented to share that ruler's left edge and width.
export function buildFooterWaveTicks(durationSec) {
  if (!footerWaveTicks) return;
  footerWaveTicks.innerHTML = "";
  if (!durationSec || durationSec <= 0) return;
  const step = tickStep(durationSec);
  for (let t = 0; t <= durationSec; t += step) {
    const tick = document.createElement("div");
    tick.className = "tick";
    tick.style.left = `${(t / durationSec) * 100}%`;
    tick.innerHTML = `<span class="tick-label">${fmtTickLabel(t)}</span>`;
    footerWaveTicks.appendChild(tick);
  }
}

// Build the presence-panel ruler labels from the actual track duration.
// The HTML ships 8 placeholder <b> tags ("0:00 ... 3:38"); we replace
// each label's text with a tick at evenly-spaced fractions of the song.
export function buildPresenceRuler(durationSec) {
  if (!presenceRulerEl) return;
  const ticks = presenceRulerEl.querySelectorAll("b");
  if (!ticks.length) return;
  if (!durationSec || durationSec <= 0) {
    for (const t of ticks) t.textContent = "0:00";
    return;
  }
  // 8 ticks -- evenly distribute from 0 to duration.
  const n = ticks.length;
  for (let i = 0; i < n; i++) {
    const frac = i / (n - 1);
    ticks[i].textContent = fmtTickLabel(frac * durationSec);
  }
}

// Move the gold playhead line that overlays the presence-bars panel.
// Uses left% within the .presence-bars container, which spans the full
// duration -- matches the ruler ticks above it.
export function updatePresencePlayhead(currentSec) {
  if (!presencePlayheadEl) return;
  if (!totalDuration || totalDuration <= 0) {
    presencePlayheadEl.classList.add("hidden");
    return;
  }
  const pct = Math.max(0, Math.min(100, (currentSec / totalDuration) * 100));
  presencePlayheadEl.style.left = `${pct}%`;
  presencePlayheadEl.classList.remove("hidden");
}

export function updateLoopRegionVisual() {
  const regionItem = document.getElementById("t-export-region");
  const hasRegion = loopEnabled && totalDuration > 0 && loopEnd > loopStart;
  if (regionItem) regionItem.setAttribute("aria-disabled", String(!hasRegion));
  // Keep the engine's loop bounds in sync with every loop change (toggle/drag);
  // the engine wraps playback itself off these values. No-op on streaming path.
  audioEngine?.setLoop(loopEnabled, loopStart, loopEnd);
  // Mirror the bounds into the exact-loop text fields (skips fields being edited).
  syncLoopInputs();
  if (!loopEnabled || !totalDuration) {
    loopRegionEl.classList.add("hidden");
    document.querySelector(".waves-column")?.classList.remove("loop-armed");
    return;
  }
  ensureLoopRegionParent();
  // Keep the loop overlay in the same normalized timeline coordinate
  // system as the ruler ticks. Percentages avoid CSS zoom mismatch:
  // pointer coordinates and getBoundingClientRect() are visual pixels,
  // but style.left/style.width in px are unzoomed layout pixels.
  const startPct = Math.max(0, Math.min(100, (loopStart / totalDuration) * 100));
  const endPct = Math.max(0, Math.min(100, (loopEnd / totalDuration) * 100));
  loopRegionEl.style.left = `${startPct}%`;
  loopRegionEl.style.width = `${Math.max(0, endPct - startPct)}%`;
  loopRegionEl.classList.remove("hidden");
  positionLaneNuggets(endPct);
}

// The per-lane drag nuggets live in the waveform overlay, not in the loop
// region, because each one has to sit on its own lane. They follow the loop
// through a custom property on the column both subtrees share: one write per
// loop change rather than one per lane per frame.
//
// No minimum size. A width threshold in percent is a threshold on the fraction
// of the song selected, so a four-bar loop in a four-minute track never meets
// it and the handles simply never appear -- which is what shipped first and is
// the whole reason this note exists. Measuring pixels instead would go stale,
// since zooming changes the rendered width without going through here.
//
// On a selection narrower than the nugget it does cover the resize handles,
// but those extend 7px outside the region on each side (.loop-handle in
// waves.css), so both stay grabbable from the outer edge.
function positionLaneNuggets(endPct) {
  // The column itself, not whatever the region is parented to:
  // loopOverlayParent falls back to the ruler when the column does not exist
  // yet, and the class would then be added to one element and removed from
  // another, leaving the handles showing with no loop behind them.
  const column = document.querySelector(".waves-column");
  if (!column) return;
  column.style.setProperty("--loop-right", `${endPct}%`);
  column.classList.add("loop-armed");
}

// Keep the exact-loop text fields in sync with loopStart/loopEnd after any
// programmatic change (drag, toggle). Never overwrite a field the user is
// actively editing, and disable both when no track is loaded.
function syncLoopInputs() {
  const enabled = totalDuration > 0;
  for (const [input, value] of [
    [loopStartInput, loopStart],
    [loopEndInput, loopEnd],
  ]) {
    if (!input) continue;
    input.disabled = !enabled;
    if (document.activeElement !== input) input.value = fmtTimeMs(value);
  }
}

// Commit a typed loop time. Invalid/out-of-range input reverts the field to the
// current stored value (self-evident rejection) rather than raising an error;
// showError lives in the import form and would surface in the wrong place.
function commitLoopInput(which) {
  const input = which === "start" ? loopStartInput : loopEndInput;
  if (!input) return;
  const revert = () => {
    input.value = fmtTimeMs(which === "start" ? loopStart : loopEnd);
  };
  const parsed = parseTimecode(input.value);
  if (parsed === null || totalDuration <= 0) {
    revert();
    return;
  }
  const v = Math.max(0, Math.min(totalDuration, parsed));
  const start = which === "start" ? v : loopStart;
  const end = which === "end" ? v : loopEnd;
  if (end - start < MIN_LOOP_SEC) {
    revert();
    return;
  }
  setLoopStart(start);
  setLoopEnd(end);
  setLoopEnabled(true);
  loopBtn.classList.add("active");
  updateLoopRegionVisual();
}

// Wheel over a loop field nudges it, in seconds on the left of the decimal
// point and in milliseconds on the right. Two units in one field is the whole
// point: a loop boundary is chosen coarsely first and then trimmed, and doing
// the trim by retyping nine characters is why the fields were barely used.
//
// A tenth of a second per notch on the seconds half, ten milliseconds on the
// other. One millisecond per notch would need a hundred notches to cover what
// the ear can hear.
const LOOP_WHEEL_SEC = 0.1;
const LOOP_WHEEL_MS = 0.01;

// Which half of the field the pointer is over. Character-level hit-testing
// inside an <input> is not reliable across browsers (caretRangeFromPoint
// returns the element, not an offset inside it), but only one boundary matters
// here, so measure the text up to the decimal point and compare. The field is
// centre-aligned and its padding is symmetric, so the border box and the
// content box share a centre and the string starts halfway through the
// leftover space.
let _loopWheelCtx = null;
function loopWheelUnit(input, clientX) {
  const value = input.value || "";
  const dot = value.indexOf(".");
  if (dot < 0) return "sec";
  const style = getComputedStyle(input);
  _loopWheelCtx ||= document.createElement("canvas").getContext("2d");
  _loopWheelCtx.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
  const full = _loopWheelCtx.measureText(value).width;
  const throughDot = _loopWheelCtx.measureText(value.slice(0, dot + 1)).width;
  const rect = input.getBoundingClientRect();
  const textStart = rect.left + (rect.width - full) / 2;
  return clientX >= textStart + throughDot ? "ms" : "sec";
}

// Same rules as a typed commit: clamp to the track, never let the two bounds
// cross inside MIN_LOOP_SEC. A nudge that would break either is dropped rather
// than clamped to the limit, so holding the wheel against the end of a track
// does not silently drag the other bound along.
function nudgeLoopInput(which, direction, unit) {
  if (totalDuration <= 0) return;
  const step = unit === "ms" ? LOOP_WHEEL_MS : LOOP_WHEEL_SEC;
  const current = which === "start" ? loopStart : loopEnd;
  const next = Math.round((current + direction * step) * 1000) / 1000;
  if (next < 0 || next > totalDuration) return;
  const start = which === "start" ? next : loopStart;
  const end = which === "end" ? next : loopEnd;
  if (end - start < MIN_LOOP_SEC) return;
  setLoopStart(start);
  setLoopEnd(end);
  setLoopEnabled(true);
  loopBtn.classList.add("active");
  updateLoopRegionVisual();
}

function wireLoopInputs() {
  for (const [input, which] of [
    [loopStartInput, "start"],
    [loopEndInput, "end"],
  ]) {
    if (!input) continue;
    input.addEventListener("blur", () => commitLoopInput(which));
    input.addEventListener(
      "wheel",
      (e) => {
        if (input.disabled || totalDuration <= 0) return;
        // The lanes zoom on wheel (#493) and the page scrolls; neither is what
        // a wheel over a numeric field is asking for.
        e.preventDefault();
        e.stopPropagation();
        nudgeLoopInput(which, e.deltaY < 0 ? 1 : -1, loopWheelUnit(input, e.clientX));
        // syncLoopInputs leaves a focused field alone so it cannot overwrite
        // what is being typed. A wheel is not typing, so write it back here.
        input.value = fmtTimeMs(which === "start" ? loopStart : loopEnd);
      },
      { passive: false }
    );
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        input.blur();
      } else if (e.key === "Escape") {
        e.preventDefault();
        input.value = fmtTimeMs(which === "start" ? loopStart : loopEnd);
        input.blur();
      }
    });
  }
  syncLoopInputs();
}

// Standard DAW transport state machine:
//   [stopped]  (paused at start)  ─Play→  [playing]
//        ↑                                  ↓ Play
//      Stop                                [paused]  (paused mid-track)
//                       Stop ↓
//                          [stopped]
//
// Play button is a Play/Pause toggle. Stop both pauses and returns the
// playhead to 0 (or loopStart if loop is on). Visual state is driven
// from the multitrack lifecycle events in player.js (mt.on play/pause/
// timeupdate) — click handlers only mutate the transport, never the
// button's CSS class. That way manual seeks (e.g. clicking the ruler)
// keep the button states in sync without extra plumbing.
// WKWebView (Tauri desktop) has small audio buffers. After a seek, all
// audio elements drop their buffers and issue new range requests simultaneously.
// Calling play() before they reach HAVE_FUTURE_DATA (readyState >= 3) causes
// choppiness. Wait for all elements to be ready, with a hard 1.5 s fallback so
// the user is never stuck. Desktop browsers buffer aggressively enough that
// this wait is skipped entirely (readyState is already >= 3 by the time play
// is pressed after a seek).
function _playWhenReady() {
  if (!multitrack) return;
  const inTauri = Boolean(window.__TAURI__?.core?.invoke);
  if (!inTauri) { multitrack.play(); return; }

  const audios = (multitrack.audios ?? [])
    .filter((a) => a instanceof HTMLMediaElement && a.src);
  const notReady = audios.filter((a) => a.readyState < 3);
  if (!notReady.length) { multitrack.play(); return; }

  let fired = false;
  const fire = () => { if (!fired && multitrack && !multitrack.isPlaying()) { fired = true; multitrack.play(); } };
  const waits = notReady.map((a) => new Promise((res) => {
    if (a.readyState >= 3) { res(); return; }
    const onReady = () => { a.removeEventListener("canplay", onReady); res(); };
    a.addEventListener("canplay", onReady);
  }));
  Promise.all(waits).then(fire);
  window.setTimeout(fire, 1500);
}

// The live beat grid to count against: the editor's copy when it holds one
// (reflects unsaved drags), else the grid last handed to the metronome UI.
function _currentGrid() {
  const edited = getGridBeats?.() ?? [];
  if (edited.length) return { beats: edited, bars: getGridBars?.() ?? [] };
  if (_lastGrid?.beats?.length) return { beats: _lastGrid.beats, bars: _lastGrid.bars ?? [] };
  return null;
}

// Arm a count-in when it is enabled and the engine + grid can support one.
// Starts the audio late (engine.play(leadIn)) and schedules the count clicks in
// the gap, whether or not the running click is on. Returns true when it took
// over starting playback, so the caller does not also start it immediately.
function _armCountIn(eng, startPos) {
  if (!metronomeCountIn || !eng?.supportsCountIn || !metronome) return false;
  const grid = _currentGrid();
  if (!grid) return false;
  const { leadIn, clicks } = computeCountIn(grid.beats, grid.bars, {
    countBars: 1,
    multiplier: metronome.getMultiplier?.() ?? 1,
    accentMode: metronomeBeatsPerBar,
    start: startPos,
  });
  if (leadIn <= 0 || !clicks.length) return false;
  // Clicks sit in source time, leading into the start position: the last lands
  // one beat before the audio, so the song enters on the next downbeat.
  const sourceClicks = clicks.map((c) => ({ time: startPos - leadIn + c.offset, accent: c.accent }));
  eng.play(leadIn); // sets the (future) clock the clicks are scheduled against
  metronome.playCountIn(sourceClicks);
  return true;
}

export function togglePlayPause() {
  const eng = audioEngine;
  const tx = eng ?? multitrack;
  if (!tx) return;
  if (tx.isPlaying()) {
    tx.pause();
    metronome?.cancelCountIn?.(); // drop a count-in if paused before the audio enters
    // The engine emits no play/pause events (the multitrack stays silent), so
    // the play-button visual that the ws "pause" handler normally toggles must
    // be driven here directly.
    if (eng) playBtn.classList.remove("playing");
    return;
  }
  const ctx = tx.audioContext;
  // Safari requires play() to be called synchronously within the user-gesture
  // handler. Resume the AudioContext fire-and-forget so the context becomes
  // live, then call play() immediately on the same tick.
  if (ctx && ctx.state === "suspended") {
    ctx.resume().catch(() => {});
  }
  // Snap playhead to loopStart on play (DAW convention).
  if (loopEnabled && totalDuration > 0) {
    tx.setTime(loopStart);
  }
  if (eng) {
    // Match the engine's own end-of-track reset so the count-in leads into the
    // same position playback will actually start from.
    let startPos = eng.getCurrentTime?.() ?? 0;
    if (totalDuration > 0 && startPos >= totalDuration) startPos = 0;
    if (!_armCountIn(eng, startPos)) eng.play();
    playBtn.classList.add("playing");
    stopBtn.classList.remove("stopped");
  } else {
    _playWhenReady();
  }
}

export function stopTransport() {
  const eng = audioEngine;
  const tx = eng ?? multitrack;
  if (!tx) return;
  tx.pause();
  metronome?.cancelCountIn?.(); // a count-in in progress must not outlive Stop
  tx.setTime(loopEnabled ? loopStart : 0); // engine: setTime → onTime → stop visual
  if (eng) playBtn.classList.remove("playing");
}

export function toggleLoop() {
  setLoopEnabled(!loopEnabled);
  loopBtn.classList.toggle("active", loopEnabled);
  updateLoopRegionVisual();
}

// Click-drag on the timeline ruler or waveform body to define the loop
// region. Drag direction doesn't matter -- start and end get sorted.
// Adjust an existing loop region rather than redrawing it (#538, discussion
// #507).
//
// Three gestures on one element:
//   - a handle at either edge moves only that edge, so a loop can be tightened
//     one side at a time instead of being re-measured from scratch;
//   - the body moves both edges together, preserving length, so a loop found by
//     ear can be slid;
//   - a press that does not move is still a seek, which is what the region did
//     before it became interactive, and losing that would be a regression for
//     anyone who just wants to click inside their selection.
//
// Pointer events throughout, so this works with touch and pen as well as a
// mouse.
function wireLoopRegionAdjust() {
  if (!loopRegionEl) return;

  let mode = null; // "start" | "end" | "move"
  let pointerId = null;
  let grabTime = 0; // where in the track the pointer went down
  let fromStart = 0;
  let fromEnd = 0;
  let moved = false;

  const apply = (t) => {
    const next = loopDragResult({
      mode,
      pointerTime: t,
      grabTime,
      fromStart,
      fromEnd,
      duration: totalDuration,
    });
    setLoopStart(next.start);
    setLoopEnd(next.end);
    updateLoopRegionVisual();
  };

  loopRegionEl.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || !totalDuration) return;
    // Starting a pointer-drag from the same gesture would move the region
    // while it is being dragged out.
    if (e.target.closest(DRAG_OUT_SELECTOR)) return;
    const t = timeFromClientX(e.clientX);
    if (t === null) return;
    mode = e.target.closest("[data-loop-handle]")?.dataset.loopHandle ?? "move";
    pointerId = e.pointerId;
    grabTime = t;
    fromStart = loopStart;
    fromEnd = loopEnd;
    moved = false;
    loopRegionEl.classList.add("dragging");
    loopRegionEl.setPointerCapture(e.pointerId);
    // Stops wireLoopDrag's surface handler starting a fresh selection
    // underneath this one.
    e.stopPropagation();
    e.preventDefault();
  });

  loopRegionEl.addEventListener("pointermove", (e) => {
    if (mode === null || e.pointerId !== pointerId) return;
    const t = timeFromClientX(e.clientX);
    if (t === null) return;
    // Same threshold the create-drag uses to tell a click from a drag.
    if (Math.abs(t - grabTime) >= MIN_LOOP_SEC) moved = true;
    apply(t);
    e.preventDefault();
  });

  const finish = (e) => {
    if (mode === null || e.pointerId !== pointerId) return;
    const wasMove = mode === "move";
    mode = null;
    pointerId = null;
    loopRegionEl.classList.remove("dragging");
    if (!moved && wasMove) {
      // A press with no travel: seek, exactly as clicking here did before the
      // region took pointer events.
      setPlayheadTime(grabTime);
    }
    syncLoopInputs();
  };

  loopRegionEl.addEventListener("pointerup", finish);
  loopRegionEl.addEventListener("pointercancel", finish);
}

// Tiny drags are treated as clicks and seek the playhead instead.
function wireLoopDrag() {
  let dragging = false;
  let dragStartTime = 0;
  let activePointerId = null;
  let moved = false;

  const startDrag = (e, surface) => {
    // .loop-region covers the mix grip, which lives inside it. The lane
    // nuggets sit in the waveform overlay instead, so they need naming: without
    // this, grabbing one starts a new selection and preventDefault below kills
    // the drag-out before dragstart ever fires.
    if (e.button !== 0 || e.target.closest(".loop-region")) return;
    if (e.target.closest(DRAG_OUT_SELECTOR)) return;
    const t = timeFromClientX(e.clientX);
    if (t === null) return;
    dragging = true;
    activePointerId = e.pointerId;
    moved = false;
    dragStartTime = t;
    setLoopStart(t);
    setLoopEnd(t);
    setLoopEnabled(true);
    loopBtn.classList.add("active");
    updateLoopRegionVisual();
    surface.setPointerCapture(e.pointerId);
    e.preventDefault();
  };

  const moveDrag = (e) => {
    if (!dragging || e.pointerId !== activePointerId) return;
    const t = timeFromClientX(e.clientX);
    if (t === null) return;
    if (Math.abs(t - dragStartTime) >= MIN_LOOP_SEC) moved = true;
    if (t < dragStartTime) {
      setLoopStart(t);
      setLoopEnd(dragStartTime);
    } else {
      setLoopStart(dragStartTime);
      setLoopEnd(t);
    }
    updateLoopRegionVisual();
  };

  const finishDrag = (e) => {
    if (!dragging || e.pointerId !== activePointerId) return;
    dragging = false;
    activePointerId = null;
    const clicked = !moved || loopEnd - loopStart < MIN_LOOP_SEC;
    if (clicked) {
      setLoopEnabled(false);
      loopBtn.classList.remove("active");
      updateLoopRegionVisual();
      setPlayheadTime(dragStartTime);
    }
  };

  const wavesColumn = document.querySelector(".waves-column");
  const surfaces = [rulerTime, wavesColumn].filter(Boolean);
  for (const surface of surfaces) {
    surface.addEventListener("pointerdown", (e) => {
      if (surface === rulerTime && e.target !== rulerTime) return;
      startDrag(e, surface);
    });
    surface.addEventListener("pointermove", moveDrag);
    surface.addEventListener("pointerup", finishDrag);
    surface.addEventListener("pointercancel", finishDrag);
  }
}

// ─── Zoom ───
//
// Single CSS variable `--zoom` on .wave-canvas drives the visual width
// (canvas = 100% * zoom). Multitrack's pxPerSec is set to match so its
// internal canvases stay the exact same pixel width as the canvas; that
// way the bundle never adds its own internal horizontal scroll, which
// historically broke alignment with our ruler/loop overlay.
//
// All percentage-positioned children (ruler ticks, playhead, grid lines,
// loop region) automatically stretch with the canvas, so the loop drag
// math stays correct without any per-element width logic.

// Keep the header ruler horizontally aligned with the (possibly wider, scrolled)
// waveform body. The ruler lives outside .wave-scroll, so translate it by the
// same scrollLeft; .daw-ruler-area uses overflow-x: clip to hide the spill while
// leaving the vertical playhead line (overflow-y: visible) intact.
export function syncRulerScroll() {
  if (rulerTime && waveScroll) {
    rulerTime.style.transform = `translateX(${-waveScroll.scrollLeft}px)`;
  }
}

export function applyWaveZoom() {
  const lanes = document.getElementById("lanes") || waveCanvas;
  const wavesColumn = document.querySelector(".waves-column");
  if (wavesColumn) {
    lanes?.style.setProperty("--wave-playhead-h", `${wavesColumn.clientHeight}px`);
  }
  if (multitrack && totalDuration > 0 && waveScroll) {
    const baseWidth = waveScroll.clientWidth;
    if (baseWidth > 0) {
      // Two separate reasons the content can be wider than the viewport, and
      // they multiply rather than compete: the user's zoom, and the floor that
      // stops the whole track compressing into a sliver on a narrow window.
      const contentWidth = Math.max(baseWidth * waveZoom, WAVE_MIN_WIDTH);
      const zoom = contentWidth / baseWidth;
      // Widen the container via --zoom FIRST. Then, after the browser has
      // reflowed it, zoom WaveSurfer to fit the container's *actual* width.
      // Measuring post-reflow avoids the resize race where WaveSurfer renders
      // wider than its container and exposes its own (unstyled, light) internal
      // horizontal scrollbar — the only horizontal scroll must come from the
      // outer .wave-scroll, which also keeps the ruler/playhead aligned.
      lanes?.style.setProperty("--zoom", String(zoom));
      requestAnimationFrame(() => {
        if (!multitrack || totalDuration <= 0) return;
        const w = multitrackContainer?.clientWidth || contentWidth;
        try { multitrack.zoom(w / totalDuration); } catch { /* ignore -- pre-canplay */ }
        // Redraw the SVG bars against the width the reflow actually produced.
        // The bars are 1 viewBox unit each, so leaving the old count in place
        // would stretch every bar by the zoom factor: same waveform, fatter
        // strokes. Redrawing keeps them 3px wide and spends the extra width on
        // detail instead, which is the whole point of zooming in.
        overviewRerenderFn?.();
        syncRulerScroll();
      });
    }
  }
}

/**
 * Set the zoom, keeping the time under `anchorClientX` where it is.
 *
 * Without the anchor the view jumps to wherever scrollLeft happened to be, and
 * zooming toward a specific bar becomes a game of chase-the-scrollbar.
 */
export function setWaveZoomLevel(next, anchorClientX = null) {
  const clamped = Math.min(WAVE_ZOOM_MAX, Math.max(WAVE_ZOOM_MIN, next));
  if (Math.abs(clamped - waveZoom) < 1e-4) return false;
  // Which content pixel the anchor is on, before anything moves.
  const rect = waveScroll?.getBoundingClientRect();
  const offsetX = anchorClientX !== null && rect
    ? Math.min(rect.width, Math.max(0, anchorClientX - rect.left))
    : (waveScroll ? waveScroll.clientWidth / 2 : 0);
  const contentX = (waveScroll?.scrollLeft ?? 0) + offsetX;
  // Measured, not derived from the zoom ratio. WAVE_MIN_WIDTH floors the
  // content width, so on a window narrower than 720px a zoom step can widen the
  // content by less than its own factor -- or not at all -- and scaling by the
  // ratio would slide the anchor out from under the pointer.
  const beforeWidth = waveCanvas?.getBoundingClientRect().width || 0;

  setWaveZoom(clamped);
  applyWaveZoom();
  const afterWidth = waveCanvas?.getBoundingClientRect().width || beforeWidth;
  const growth = beforeWidth > 0 ? afterWidth / beforeWidth : 1;
  // Everything positioned against the timeline is laid out again at the new
  // width: the ruler because its ticks are now the wrong distance apart for the
  // detail on screen, the playhead and the loop region because buildRuler
  // rebuilds the elements they live in.
  buildRuler(totalDuration);
  // buildRuler re-creates the marker element, so it comes back at 0. Put it
  // back where the transport actually is -- but only if something can say;
  // defaulting to 0 would yank the playhead to the start of the track.
  const now = (audioEngine ?? multitrack)?.getCurrentTime?.();
  if (typeof now === "number") updatePlayheadMarker(now);
  updateLoopRegionVisual();

  if (waveScroll) {
    // The same content pixel after the widening, minus where it sits in the
    // viewport, is the scroll offset that leaves it under the pointer.
    const target = contentX * growth - offsetX;
    waveScroll.scrollLeft = Math.max(0, target);
    syncRulerScroll();
  }
  return true;
}

export function resetWaveZoom() {
  return setWaveZoomLevel(WAVE_ZOOM_MIN);
}

function wireZoomButtons() {
  if (waveScroll) {
    let rafId = null;
    const ro = new ResizeObserver(() => {
      if (!multitrack || totalDuration <= 0) return;
      if (rafId) cancelAnimationFrame(rafId);
      rafId = requestAnimationFrame(() => {
        rafId = null;
        applyWaveZoom();
        // Tick spacing is chosen from the content width, so a resize can leave
        // the ruler at the density the old width called for. Rebuild only when
        // the step it would pick has actually changed: buildRuler writes inside
        // the observed element, so rebuilding every time would feed the
        // observer its own output.
        const next = tickStep(totalDuration, rulerTime?.getBoundingClientRect().width || 0);
        if (next !== _rulerStep) {
          buildRuler(totalDuration);
          updateLoopRegionVisual();
        }
      });
    });
    ro.observe(waveScroll);
  }
  if (waveScroll) {
    waveScroll.addEventListener("wheel", (e) => {
      if (totalDuration <= 0) return;
      // Shift is the pan gesture, and a trackpad's horizontal axis reports as
      // deltaX with no modifier. Both mean "move along the track", so neither
      // should change the zoom.
      if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
        if (waveScroll.scrollWidth <= waveScroll.clientWidth) return;
        e.preventDefault();
        // Whichever axis the gesture actually carried. Shift-wheel puts it on
        // deltaY, a trackpad swipe on deltaX, and shift plus a swipe on deltaX
        // with deltaY at zero.
        waveScroll.scrollLeft += Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
        syncRulerScroll();
        return;
      }
      if (!e.deltaY) return;
      // deltaMode 1 is lines and 2 is pages; both deliver far smaller numbers
      // than pixels, so normalise to notches rather than scaling by deltaY.
      const notches = Math.max(1, Math.min(3, Math.round(Math.abs(e.deltaY) / 100) || 1));
      const factor = WAVE_ZOOM_STEP ** (e.deltaY < 0 ? notches : -notches);
      const changed = setWaveZoomLevel(waveZoom * factor, e.clientX);
      // Only swallow the event when it did something. At either end of the
      // range the page should still get its scroll rather than feel dead.
      if (changed) e.preventDefault();
    }, { passive: false });
    waveScroll.addEventListener("scroll", syncRulerScroll, { passive: true });
  }
  applyWaveZoom();
}

// Keep the mixer column and the waveform area scrolled in lockstep so stem
// controls stay aligned with their lanes when the stack overflows (#159).
function wireLaneScrollSync() {
  const mixer = document.getElementById("mixer");
  if (!mixer || !waveScroll) return;
  // Mirror scrollTop between the two panes by assigning only when the values
  // differ. The echo stops on its own (once equal, the partner's handler is a
  // no-op), so no reentrancy guard / rAF is needed — that frame-delayed guard
  // was what made inertial scrolling stutter (#163).
  const link = (src, dst) =>
    src.addEventListener("scroll", () => {
      if (dst.scrollTop !== src.scrollTop) dst.scrollTop = src.scrollTop;
    }, { passive: true });
  link(mixer, waveScroll);
  link(waveScroll, mixer);
}

// ─── Wire transport buttons ───

export function wireTransportButtons() {
  playBtn.addEventListener("click", togglePlayPause);
  playMiniBtn?.addEventListener("click", togglePlayPause);
  stopBtn.addEventListener("click", stopTransport);
  loopBtn.addEventListener("click", toggleLoop);
  wireLoopDrag();
  wireLoopRegionAdjust();
  wireLoopInputs();
  wireZoomButtons();
  wireLaneScrollSync();
  masterFader?.addEventListener("input", () => {
    setMasterVolume(parseFloat(masterFader.value));
    applyMix();
  });
  masterFader?.addEventListener("dblclick", () => {
    masterFader.value = "0.5";
    setMasterVolume(0.5);
    applyMix();
  });
  wireSpeedControl();
  wireMetronomeControl();
  wirePitchControl();
}

// Fixed presets, not a continuous dial -- practice speeds for slowing a part
// down, not a general-purpose tempo control (issue #269 follow-up).
// 0.75x rather than 0.5x/0.25x (#433): below ~0.7x the time-stretch artefacts
// dominate and the part gets harder to follow, which is the opposite of what
// a practice speed is for.
const SPEED_PRESETS = [0.75, 1];

function applySpeed(rate) {
  // Snap to the nearest preset rather than clamping continuously: every
  // caller (button click, resetSpeed on track load) already passes one of
  // SPEED_PRESETS, but snapping keeps this correct even if that changes.
  const clamped = SPEED_PRESETS.reduce((best, p) =>
    Math.abs(p - rate) < Math.abs(best - rate) ? p : best
  );
  setPlaybackSpeed(clamped);
  for (const btn of speedBtns) {
    if (!btn) continue;
    const on = parseFloat(btn.dataset.speed) === clamped;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-checked", on ? "true" : "false");
  }
  audioEngine?.setPlaybackRate?.(clamped);
  if (multitrack) {
    for (const a of (multitrack.audios ?? [])) {
      try { a.playbackRate = clamped; } catch { /* noop */ }
    }
  }
}


// ── Transpose (#245) ────────────────────────────────────────────────────────
//
// Semitones, not a continuous dial, for the same reason the speed control uses
// presets: this is for moving a backing track into a singer's range, and every
// useful destination is a whole semitone away.
//
// Capped at a fifth rather than an octave. The worklet stretches then resamples,
// and past roughly five semitones that starts to be audible on sustained
// material. A control whose extremes sound broken is worse than a narrower one
// that always sounds right.
const PITCH_MIN = -6;
const PITCH_MAX = 6;

let _pitchSemitones = 0;
let _pitchAvailable = false;

/** Redraw the global key readout and its buttons. Touches no lane. */
function renderPitch() {
  if (pitchValueEl) {
    // Signed, so "+2" and "-2" are distinguishable at a glance; bare "0" for
    // the default rather than a redundant "+0".
    pitchValueEl.textContent = _pitchSemitones > 0 ? `+${_pitchSemitones}` : String(_pitchSemitones);
    pitchValueEl.classList.toggle("active", _pitchSemitones !== 0);
  }
  // A stepper that silently stops responding reads as broken, so say which end
  // has been reached rather than only going inert.
  if (pitchDownBtn) pitchDownBtn.disabled = !_pitchAvailable || _pitchSemitones <= PITCH_MIN;
  if (pitchUpBtn) pitchUpBtn.disabled = !_pitchAvailable || _pitchSemitones >= PITCH_MAX;
  if (pitchResetBtn) pitchResetBtn.disabled = !_pitchAvailable;

  // Three dead buttons still wearing "Transpose up a semitone" explain nothing.
  // The reason lives on the group, because a disabled button does not reliably
  // fire the pointer events a tooltip needs.
  if (pitchWrap) {
    if (_pitchAvailable) pitchWrap.removeAttribute("title");
    else pitchWrap.title = t(pitchBlockedKey());
  }
  // Without the pitch stage the sources resample instead, so speed drags the
  // key with it. That is the one degradation here that is otherwise silent:
  // the control still works, it just quietly does something else (#552).
  if (speedWrap) {
    if (_pitchAvailable) speedWrap.removeAttribute("title");
    else speedWrap.title = t("speed.pitchFollows");
  }
}

/**
 * Move the global key, and every lane with it.
 *
 * The lanes carry absolute keys, so this applies the *change* rather than the
 * new value: a lane deliberately put a third above the rest stays a third above
 * the rest when the whole track moves. Overriding instead would flatten every
 * per-lane decision the moment the global control was touched.
 */
function applyPitch(semitones) {
  const clamped = Math.max(PITCH_MIN, Math.min(PITCH_MAX, Math.round(semitones)));
  const delta = clamped - _pitchSemitones;
  _pitchSemitones = clamped;
  renderPitch();
  if (delta !== 0) nudgeAllLanePitches(delta);
}

/**
 * Clear the readout when a track is torn down or swapped.
 *
 * Deliberately does not touch the lanes: their keys are saved per track and are
 * about to be reloaded from the store for whatever is being opened.
 */
export function resetPitch() {
  _pitchSemitones = 0;
  renderPitch();
}

/** The user's reset: the global key and every lane, back to the original. */
export function resetAllKeys() {
  _pitchSemitones = 0;
  resetAllLanePitches();
  renderPitch();
}

export function updatePitchAvailability(available) {
  _pitchAvailable = available === true;
  if (!_pitchAvailable && _pitchSemitones !== 0) _pitchSemitones = 0;
  renderPitch();
}

function wirePitchControl() {
  pitchDownBtn?.addEventListener("click", () => applyPitch(_pitchSemitones - 1));
  pitchUpBtn?.addEventListener("click", () => applyPitch(_pitchSemitones + 1));
  pitchResetBtn?.addEventListener("click", () => resetAllKeys());
}

export function resetSpeed() {
  applySpeed(1.0);
}

function wireSpeedControl() {
  for (const btn of speedBtns) {
    btn?.addEventListener("click", () => applySpeed(parseFloat(btn.dataset.speed)));
  }
}

// ─── Click track ────────────────────────────────────────────

const _METRO_PREFS_KEY = "stemdeck:metronome";

function _saveMetroPrefs() {
  storeSet(_METRO_PREFS_KEY, {
    enabled: metronomeEnabled,
    volume: metronomeVolume,
    beatsPerBar: metronomeBeatsPerBar,
    countIn: metronomeCountIn,
  }).catch((e) => console.warn("[transport] failed to save metronome prefs:", e));
}

/**
 * Push the current accent choice into the metronome.
 *
 * "Auto" defers to the bar marks the detector found, which is the only mode
 * that can be right on a track whose meter changes -- a fixed count from the
 * top of the track cannot. The explicit choices override it, and exist because
 * detection can be wrong and because some players want no accent at all.
 */
export function applyMetronomeAccent() {
  if (!metronome) return;
  if (metronomeBeatsPerBar < 0 && metronomeHasBars) {
    metronome.setDownbeatFn?.(isDownbeatIndex);
  } else {
    metronome.setDownbeatFn?.(null);
    metronome.setBeatsPerBar?.(Math.max(0, metronomeBeatsPerBar));
  }
}

function _renderMetroVolume() {
  const pct = `${Math.round(metronomeVolume * 100)}%`;
  if (metroVolEl) { metroVolEl.value = String(metronomeVolume); metroVolEl.title = t("click.volumeTitle", { pct }); }
  // Readout sits next to the slider (design 1b): a click level you can only
  // learn by hovering is one you cannot match between sessions.
  if (metroVolLabel) metroVolLabel.textContent = pct;
}

// Count-in is a press-to-arm toggle like the click on/off beside it, not a
// switch: both are "is this on for the next play?", and two different widgets
// for the same question read as two different kinds of setting.
function _renderCountIn() {
  if (!metroCountInEl) return;
  metroCountInEl.classList.toggle("active", metronomeCountIn);
  metroCountInEl.setAttribute("aria-pressed", metronomeCountIn ? "true" : "false");
}

export function toggleMetronome(force) {
  if (!metroBtn || metroBtn.disabled) return;
  const on = force === undefined ? !metronomeEnabled : !!force;
  setMetronomeEnabled(on);
  metroBtn.classList.toggle("active", on);
  metroBtn.setAttribute("aria-pressed", on ? "true" : "false");
  metronome?.setEnabled(on);
  _saveMetroPrefs();
}

/**
 * Reflect the current track's beat grid in the UI. `grid` is the parsed
 * beats.json, or null when the job has none (pre-existing jobs) or the
 * streaming path is active.
 * @param {object|null} grid
 * @param {string} reason  Shown to the user when there is no grid.
 */
let _lastGrid = null;

function _renderMetroMultiplier() {
  const mult = metronome?.getMultiplier?.() ?? 1;
  for (const [btn, v] of [[metroHalfBtn, 0.5], [metroOneBtn, 1], [metroDoubleBtn, 2]]) {
    if (!btn) continue;
    const on = mult === v;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-checked", on ? "true" : "false");
  }
}

function _renderMetroNote(grid) {
  if (!metroNoteEl) return;
  if (!grid) { metroNoteEl.textContent = ""; metroNoteEl.className = "metro-note"; return; }

  // Report the tempo actually being clicked, which is the detected tempo times
  // whatever level the user picked -- not the raw detected value.
  const eff = metronome?.getEffectiveBpm?.();
  const bpm = Number.isFinite(eff) ? eff : Number(grid.bpm);
  const conf = Number(grid.confidence);

  // Each branch is one complete, translated sentence (not clauses joined at
  // runtime) so word order can differ freely per language -- see i18n.js's
  // metro.note.* keys. Deliberately worded as "on a drum hit", not
  // "confidence the tempo is right": a half-time grid scores ~100% here and
  // is still musically wrong, so the note must not imply the metrical level
  // was verified -- that is what the halve/double control is for.
  let text = "";
  if (Number.isFinite(bpm) && Number.isFinite(conf)) {
    const bpmStr = bpm.toFixed(1);
    const nBars = Array.isArray(grid.bars) ? grid.bars.length : 0;
    if (metronomeBeatsPerBar < 0 && nBars === 1) {
      text = t("metro.note.detectedSingleBar", { bpm: bpmStr, conf, beats: grid.bars[0].beats_per_bar });
    } else if (metronomeBeatsPerBar < 0 && nBars > 1) {
      text = t("metro.note.detectedMultiBar", { bpm: bpmStr, conf, count: nBars });
    } else if (metronomeBeatsPerBar > 0) {
      text = t("metro.note.full", { bpm: bpmStr, conf, accent: metronomeBeatsPerBar });
    } else {
      text = t("metro.note.noAccent", { bpm: bpmStr, conf });
    }
  } else if (Number.isFinite(bpm)) {
    text = t("metro.note.fallback", { bpm: bpm.toFixed(1) });
  }
  metroNoteEl.textContent = text;
  metroNoteEl.title = text; // clipped to one line; the full text is a hover away
  metroNoteEl.className = Number.isFinite(conf) && conf < 60 ? "metro-note warn" : "metro-note";
}

export function updateMetronomeAvailability(grid, reason = "") {
  if (!metroBtn) return;
  _lastGrid = grid || null;
  const available = !!(grid && Array.isArray(grid.beats) && grid.beats.length);
  metroBtn.disabled = !available;
  metroBtn.title = available ? t("click.toggleTitle") : (reason || t("click.unavailableAria"));

  if (!available) {
    // Keep the stored preference so the click returns on the next track that
    // does have a grid; only the live toggle goes off.
    metroBtn.classList.remove("active");
    metroBtn.setAttribute("aria-pressed", "false");
    metroPanel?.classList.add("hidden");
    if (metroNoteEl) { metroNoteEl.textContent = reason || ""; metroNoteEl.className = "metro-note"; }
    return;
  }

  metroBtn.classList.toggle("active", metronomeEnabled);
  metroBtn.setAttribute("aria-pressed", metronomeEnabled ? "true" : "false");
  metroPanel?.classList.remove("hidden"); // undo a previous track's "unavailable" hide
  setMetronomeHasBars(Array.isArray(grid.bars) && grid.bars.length > 0);
  const autoOpt = metroBarEl?.querySelector('option[value="-1"]');
  if (autoOpt) {
    autoOpt.disabled = !metronomeHasBars;
    autoOpt.textContent = metronomeHasBars ? t("click.auto") : t("click.autoNone");
  }
  if (metroBarEl) metroBarEl.value = String(metronomeBeatsPerBar);
  _renderMetroMultiplier();
  _renderMetroNote(grid);
}

function wireMetronomeControl() {
  if (!metroBtn) return;

  // Restore preferences before the first track loads so the click comes back
  // on exactly as the user left it.
  storeGet(_METRO_PREFS_KEY, null).then((prefs) => {
    if (prefs && typeof prefs === "object") {
      if (typeof prefs.volume === "number") setMetronomeVolume(Math.max(0, Math.min(1, prefs.volume)));
      if (typeof prefs.beatsPerBar === "number") setMetronomeBeatsPerBar(prefs.beatsPerBar);
      if (typeof prefs.enabled === "boolean") setMetronomeEnabled(prefs.enabled);
      if (typeof prefs.countIn === "boolean") setMetronomeCountIn(prefs.countIn);
    }
    _renderMetroVolume();
    if (metroBarEl) metroBarEl.value = String(metronomeBeatsPerBar);
    _renderCountIn();
    if (metronomeEnabled && !metroBtn.disabled) {
      metroBtn.classList.add("active");
      metroBtn.setAttribute("aria-pressed", "true");
    }
  }).catch((e) => console.warn("[transport] failed to load metronome prefs:", e));

  // Toggles the click on/off; also bound to the K key elsewhere. Volume,
  // accent, rate and count-in sit inline next to it, always visible once a
  // track has a beat grid -- no click needed to reveal them (#269 follow-up).
  metroBtn.addEventListener("click", () => toggleMetronome());

  metroVolEl?.addEventListener("input", () => {
    const v = Math.max(0, Math.min(1, parseFloat(metroVolEl.value)));
    setMetronomeVolume(v);
    _renderMetroVolume();
    metronome?.setVolume(v);
    _saveMetroPrefs();
  });

  for (const [btn, mult] of [[metroHalfBtn, 0.5], [metroOneBtn, 1], [metroDoubleBtn, 2]]) {
    btn?.addEventListener("click", () => {
      metronome?.setMultiplier(mult);
      _renderMetroMultiplier();
      _renderMetroNote(_lastGrid);
    });
  }

  metroCountInEl?.addEventListener("click", () => {
    setMetronomeCountIn(!metronomeCountIn);
    _renderCountIn();
    _saveMetroPrefs();
  });

  metroBarEl?.addEventListener("change", () => {
    const raw = parseInt(metroBarEl.value, 10);
    setMetronomeBeatsPerBar(Number.isFinite(raw) ? raw : -1);
    applyMetronomeAccent();
    _renderMetroNote(_lastGrid);
    _saveMetroPrefs();
  });
}
