// Beat grid editor.
//
// Detection gets steady 4/4 pop right and gets harder material wrong in ways
// no heuristic reliably catches (half-time grids score ~100% on every quality
// check and are still musically wrong). This module is the escape hatch: it
// draws the grid over the waveform and lets the user correct it.
//
// Two editing models, because they solve different problems:
//
//   Ripple (default) -- dragging a beat re-interpolates every beat between it
//   and the surrounding anchors. Correcting a tempo region takes one drag, not
//   one drag per beat. This is the only model that scales: a track with 108
//   time signature changes needs ~20 anchor placements, not 800 beat drags.
//
//   Single -- moves exactly one beat. For the last few milliseconds of
//   cleanup, where rippling would disturb neighbours that are already right.
//
// Rendering is canvas, not DOM. A 6-minute track carries 800+ beats plus
// ~2000 onset ticks; that many absolutely-positioned elements janks every
// scroll and resize.

import { storeGet, storeSet } from "./utils.js";

const MIN_GAP = 0.01;          // seconds; keeps the grid strictly increasing
const HIT_PX = 6;              // pointer distance that counts as grabbing a beat
const SNAP_PX = 14;            // max on-screen distance to snap to an onset
// ...but bounded in time as well. At full-track zoom a 6-minute song puts
// ~0.27 s behind every pixel, so a purely pixel-based window is over three
// seconds wide and would teleport a dragged beat to an unrelated transient.
// 60 ms is magnetic without ever reaching a neighbouring subdivision.
const SNAP_MAX_SEC = 0.06;
// Below this per-beat pixel spacing, individual beats are hidden and only bar
// lines and anchors draw. 4 px is roughly where adjacent lines stop being
// separable, and drawing past it hides the waveform the user is editing against.
const DENSE_MIN_PX = 4;
const UNDO_DEPTH = 100;
const SAVE_DEBOUNCE_MS = 700;

const COLORS = {
  beat: "rgba(148,163,184,0.30)",
  bar: "rgba(74,140,255,0.85)",
  anchor: "#e8c840",
  hover: "#ffffff",
  onset: "rgba(0,200,160,0.35)",
};

let _jobId = null;
let _duration = 0;
let _canvas = null;
let _ctx = null;
let _host = null;          // element defining the timeline coordinate space
let _onChange = null;      // (beats, bars) -> void, drives the metronome
let _resizeObs = null;

let _beats = [];
let _bars = [];            // [{ beat: index, beats_per_bar: n }]
let _onsets = [];
let _anchors = new Set();  // beat indices the user has explicitly positioned

let _editing = false;
let _tool = "move";        // move | insert | delete | bar
let _ripple = true;
let _snap = true;

let _hoverIdx = -1;
let _dragIdx = -1;
let _dragSingle = false;
let _undo = [];
let _redo = [];
let _saveTimer = null;
let _dirty = false;

// ─── geometry ───────────────────────────────────────────────

function _rect() {
  return _host?.getBoundingClientRect() || { left: 0, width: 1 };
}
const _timeToFrac = (t) => (_duration > 0 ? Math.max(0, Math.min(1, t / _duration)) : 0);
function _xToTime(clientX) {
  const r = _rect();
  return Math.max(0, Math.min(_duration, ((clientX - r.left) / Math.max(1, r.width)) * _duration));
}
const _secondsPerPixel = () => _duration / Math.max(1, _rect().width);

// ─── state snapshots (undo) ─────────────────────────────────

function _snapshot() {
  return {
    beats: _beats.slice(),
    bars: _bars.map((b) => ({ ...b })),
    anchors: [..._anchors],
  };
}

function _restore(s) {
  _beats = s.beats.slice();
  _bars = s.bars.map((b) => ({ ...b }));
  _anchors = new Set(s.anchors);
}

function _pushUndo() {
  _undo.push(_snapshot());
  if (_undo.length > UNDO_DEPTH) _undo.shift();
  _redo.length = 0;
}

// ─── mutation helpers ───────────────────────────────────────

/** Shift bar-mark indices when beats are inserted or removed at `at`. */
function _shiftBars(at, delta) {
  _bars = _bars
    .map((b) => (b.beat >= at ? { ...b, beat: b.beat + delta } : b))
    .filter((b) => b.beat >= 0 && b.beat < _beats.length);
}

function _shiftAnchors(at, delta) {
  const next = new Set();
  for (const i of _anchors) {
    if (i < at) next.add(i);
    else if (i + delta >= 0) next.add(i + delta);
  }
  _anchors = next;
}

/** Nearest onset to `t`, or null when none is within the snap window. */
function _snapTime(t) {
  if (!_snap || !_onsets.length) return t;
  const tol = Math.min(SNAP_PX * _secondsPerPixel(), SNAP_MAX_SEC);
  let lo = 0;
  let hi = _onsets.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (_onsets[mid] < t) lo = mid + 1;
    else hi = mid;
  }
  let best = null;
  for (const i of [lo - 1, lo, lo + 1]) {
    if (i < 0 || i >= _onsets.length) continue;
    const d = Math.abs(_onsets[i] - t);
    if (d <= tol && (best === null || d < Math.abs(_onsets[best] - t))) best = i;
  }
  return best === null ? t : _onsets[best];
}

/** Previous/next anchor bounding `idx`, falling back to the grid ends. */
function _anchorSpan(idx) {
  let lo = 0;
  let hi = _beats.length - 1;
  for (const a of _anchors) {
    if (a < idx && a > lo) lo = a;
    if (a > idx && a < hi) hi = a;
  }
  return [lo, hi];
}

/**
 * Move beat `idx` to `t`.
 * Ripple mode re-interpolates everything between the surrounding anchors so a
 * whole tempo region follows one drag. Single mode moves just this beat.
 */
function _moveBeat(idx, t, single) {
  if (idx < 0 || idx >= _beats.length) return;

  if (single) {
    const lo = idx > 0 ? _beats[idx - 1] + MIN_GAP : 0;
    const hi = idx < _beats.length - 1 ? _beats[idx + 1] - MIN_GAP : _duration;
    _beats[idx] = Math.max(lo, Math.min(hi, t));
    _anchors.add(idx);
    return;
  }

  const [a, b] = _anchorSpan(idx);
  // Leave room for every beat between the anchors to stay strictly ordered.
  const lo = _beats[a] + MIN_GAP * (idx - a);
  const hi = _beats[b] - MIN_GAP * (b - idx);
  const target = Math.max(lo, Math.min(hi, t));

  const leftSpan = idx - a;
  const rightSpan = b - idx;
  if (leftSpan > 0) {
    const step = (target - _beats[a]) / leftSpan;
    for (let k = a + 1; k < idx; k++) _beats[k] = _beats[a] + step * (k - a);
  }
  if (rightSpan > 0) {
    const step = (_beats[b] - target) / rightSpan;
    for (let k = idx + 1; k < b; k++) _beats[k] = target + step * (k - idx);
  }
  _beats[idx] = target;
  _anchors.add(idx);
}

function _insertBeat(t) {
  const time = _snapTime(t);
  let at = 0;
  while (at < _beats.length && _beats[at] < time) at++;
  if (at > 0 && Math.abs(_beats[at - 1] - time) < MIN_GAP) return;
  if (at < _beats.length && Math.abs(_beats[at] - time) < MIN_GAP) return;
  _pushUndo();
  _beats.splice(at, 0, time);
  _shiftBars(at, 1);
  _shiftAnchors(at, 1);
  _anchors.add(at);
  _commit();
}

function _deleteBeat(idx) {
  if (idx < 0 || idx >= _beats.length || _beats.length <= 2) return;
  _pushUndo();
  _beats.splice(idx, 1);
  _shiftBars(idx, -1);
  _shiftAnchors(idx, -1);
  _anchors.delete(idx);
  _commit();
}

/** Toggle a downbeat at `idx`, inheriting the bar length already in force. */
function _toggleBar(idx) {
  if (idx < 0 || idx >= _beats.length) return;
  _pushUndo();
  const existing = _bars.findIndex((b) => b.beat === idx);
  if (existing >= 0) {
    _bars.splice(existing, 1);
  } else {
    _bars.push({ beat: idx, beats_per_bar: barLengthAt(idx) });
    _bars.sort((x, y) => x.beat - y.beat);
  }
  _commit();
}

/** Bar length in force at beat `idx` (from the preceding mark, else 4). */
export function barLengthAt(idx) {
  let n = 4;
  for (const b of _bars) {
    if (b.beat <= idx) n = b.beats_per_bar;
    else break;
  }
  return n;
}

/** Set the bar length of the region containing `idx`. */
export function setBarLengthAt(idx, n) {
  const len = Math.max(1, Math.min(32, Math.round(n)));
  _pushUndo();
  let mark = null;
  for (const b of _bars) if (b.beat <= idx && (!mark || b.beat > mark.beat)) mark = b;
  if (mark) mark.beats_per_bar = len;
  else {
    _bars.push({ beat: 0, beats_per_bar: len });
    _bars.sort((x, y) => x.beat - y.beat);
  }
  _commit();
}

// ─── persistence ────────────────────────────────────────────

function _commit() {
  _dirty = true;
  _onChange?.(_beats.slice(), _bars.map((b) => ({ ...b })));
  _render();
  if (_saveTimer !== null) clearTimeout(_saveTimer);
  _saveTimer = setTimeout(_save, SAVE_DEBOUNCE_MS);
}

async function _save() {
  _saveTimer = null;
  if (!_jobId || !_dirty) return;
  const payload = { beats: _beats.map((t) => +t.toFixed(6)), bars: _bars };
  try {
    const r = await fetch(`/api/jobs/${_jobId}/beats`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!r.ok) throw new Error(`PATCH ${r.status}`);
    _dirty = false;
  } catch (e) {
    console.warn("[beatgrid] failed to save edits:", e);
  }
}

/** Flush a pending save. Called before teardown so edits are never dropped. */
export function flushBeatGrid() {
  if (_saveTimer !== null) {
    clearTimeout(_saveTimer);
    _saveTimer = null;
    _save();
  }
}

export async function resetBeatGrid() {
  if (!_jobId) return null;
  try {
    await fetch(`/api/jobs/${_jobId}/beats`, { method: "DELETE" });
    const r = await fetch(`/api/jobs/${_jobId}/beats`);
    if (!r.ok) return null;
    const grid = await r.json();
    _pushUndo();
    _beats = (grid.beats || []).slice();
    _bars = (grid.bars || []).slice();
    _anchors = new Set();
    _dirty = false;
    _onChange?.(_beats.slice(), _bars.slice());
    _render();
    return grid;
  } catch (e) {
    console.warn("[beatgrid] reset failed:", e);
    return null;
  }
}

export function undoBeatGrid() {
  if (!_undo.length) return;
  _redo.push(_snapshot());
  _restore(_undo.pop());
  _commit();
}

export function redoBeatGrid() {
  if (!_redo.length) return;
  _undo.push(_snapshot());
  _restore(_redo.pop());
  _commit();
}

export const canUndo = () => _undo.length > 0;
export const canRedo = () => _redo.length > 0;

// ─── rendering ──────────────────────────────────────────────

function _resize() {
  if (!_canvas || !_host) return;
  const r = _rect();
  const dpr = window.devicePixelRatio || 1;
  const h = _host.clientHeight || 1;
  _canvas.width = Math.max(1, Math.round(r.width * dpr));
  _canvas.height = Math.max(1, Math.round(h * dpr));
  _canvas.style.width = "100%";
  _canvas.style.height = "100%";
  _ctx = _canvas.getContext("2d");
  _ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  _render();
}

function _render() {
  if (!_ctx || !_canvas) return;
  const w = _canvas.width / (window.devicePixelRatio || 1);
  const h = _canvas.height / (window.devicePixelRatio || 1);
  _ctx.clearRect(0, 0, w, h);
  if (!_beats.length || _duration <= 0) return;

  // Onset ticks along the bottom: the snap targets, so the user can see where
  // a dragged beat will land before letting go.
  if (_editing && _snap) {
    _ctx.fillStyle = COLORS.onset;
    for (const t of _onsets) {
      const x = Math.round(_timeToFrac(t) * w) + 0.5;
      _ctx.fillRect(x, h - 6, 1, 6);
    }
  }

  // Density fallback. A 6-minute track at full-track zoom puts 800 beats
  // behind ~900 px: drawing every one produces a solid wall that hides the
  // waveform and cannot be edited anyway. Below the threshold only bar lines
  // and anchors are drawn, so the overview stays readable and the individual
  // beats appear as the user zooms in (the timeline widens, this re-renders
  // via the ResizeObserver).
  // Note: this only affects drawing. Hit-testing still finds every beat, so a
  // zoomed-out click can still grab one.
  const spacingPx = w / Math.max(1, _beats.length);
  const beatsVisible = spacingPx >= DENSE_MIN_PX;

  const minPx = 2;
  let lastX = -Infinity;
  for (let i = 0; i < _beats.length; i++) {
    const x = Math.round(_timeToFrac(_beats[i]) * w) + 0.5;
    const isBar = _bars.some((b) => b.beat === i)
      || (_bars.length > 0 && _isDownbeat(i));
    const isAnchor = _anchors.has(i);
    if (!isBar && !isAnchor && !beatsVisible) continue;
    if (!isBar && !isAnchor && x - lastX < minPx) continue;
    lastX = x;

    _ctx.beginPath();
    _ctx.moveTo(x, 0);
    _ctx.lineTo(x, h);
    if (i === _hoverIdx && _editing) {
      _ctx.strokeStyle = COLORS.hover;
      _ctx.lineWidth = 2;
    } else if (isBar) {
      _ctx.strokeStyle = COLORS.bar;
      _ctx.lineWidth = 1.5;
    } else {
      _ctx.strokeStyle = COLORS.beat;
      _ctx.lineWidth = 1;
    }
    _ctx.stroke();

    if (isAnchor && _editing) {
      _ctx.fillStyle = COLORS.anchor;
      _ctx.fillRect(x - 3, 0, 6, 5);
    }
  }
}

function _isDownbeat(i) {
  if (!_bars.length) return false;
  let mark = null;
  for (const b of _bars) {
    if (b.beat <= i) mark = b;
    else break;
  }
  if (!mark) return false;
  return (i - mark.beat) % mark.beats_per_bar === 0;
}

/** Accent predicate handed to the metronome so bar marks drive the accent. */
export function isDownbeatIndex(i) {
  return _isDownbeat(i);
}

/**
 * Where a beat sits inside its bar, as `[offset, barLength]`, or null when no
 * bar mark applies. The metronome needs the position rather than a downbeat
 * yes/no so it can group an odd or compound bar (#595); `_bar_position` in
 * app/pipeline/click_render.py is the same lookup on the export side.
 */
export function barPositionIndex(i) {
  if (!_bars.length) return null;
  let mark = null;
  for (const b of _bars) {
    if (b.beat <= i) mark = b;
    else break;
  }
  if (!mark || !Number.isInteger(mark.beats_per_bar) || mark.beats_per_bar < 1) return null;
  return [(i - mark.beat) % mark.beats_per_bar, mark.beats_per_bar];
}

// ─── hit testing + pointer handling ─────────────────────────

function _nearestBeat(clientX) {
  if (!_beats.length) return -1;
  const t = _xToTime(clientX);
  let lo = 0;
  let hi = _beats.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (_beats[mid] < t) lo = mid + 1;
    else hi = mid;
  }
  let best = lo;
  if (lo > 0 && Math.abs(_beats[lo - 1] - t) < Math.abs(_beats[lo] - t)) best = lo - 1;
  const tolSec = HIT_PX * _secondsPerPixel();
  return Math.abs(_beats[best] - t) <= tolSec ? best : -1;
}

function _onPointerMove(e) {
  if (!_editing) return;
  if (_dragIdx >= 0) {
    e.stopPropagation();
    _moveBeat(_dragIdx, _snapTime(_xToTime(e.clientX)), _dragSingle);
    _onChange?.(_beats.slice(), _bars.map((b) => ({ ...b })));
    _render();
    return;
  }
  const idx = _nearestBeat(e.clientX);
  if (idx !== _hoverIdx) {
    _hoverIdx = idx;
    _canvas.style.cursor = idx >= 0 ? "ew-resize" : (_tool === "insert" ? "copy" : "default");
    _render();
  }
}

function _onPointerDown(e) {
  if (!_editing || e.button !== 0) return;
  // The canvas sits inside .waves-column, which also carries the loop-region
  // drag and click-to-seek handlers. Without this, grabbing a beat would
  // simultaneously start a loop selection and move the playhead.
  e.stopPropagation();
  const idx = _nearestBeat(e.clientX);

  if (_tool === "insert") { _insertBeat(_xToTime(e.clientX)); return; }
  if (_tool === "delete") { if (idx >= 0) _deleteBeat(idx); return; }
  if (_tool === "bar") { if (idx >= 0) _toggleBar(idx); return; }

  if (idx < 0) return;
  e.preventDefault();
  _pushUndo();
  _dragIdx = idx;
  // Alt inverts the ripple setting for a single drag, so the occasional
  // one-beat nudge does not need a trip to the toolbar.
  _dragSingle = e.altKey ? _ripple : !_ripple;
  try { _canvas.setPointerCapture?.(e.pointerId); } catch { /* capture is best-effort */ }
}

function _onPointerUp(e) {
  if (_dragIdx < 0) return;
  e.stopPropagation();
  try { _canvas.releasePointerCapture?.(e.pointerId); } catch { /* never captured */ }
  _dragIdx = -1;
  _commit();
}

function _onContextMenu(e) {
  if (!_editing) return;
  e.preventDefault();
  const idx = _nearestBeat(e.clientX);
  if (idx >= 0) _deleteBeat(idx);
}

// ─── lifecycle ──────────────────────────────────────────────

const PREFS_KEY = "stemdeck:beatgrid-prefs";

export function initBeatGrid({ jobId, grid, duration, onChange }) {
  destroyBeatGrid();
  _jobId = jobId;
  _duration = Math.max(0, duration || 0);
  _beats = Array.isArray(grid?.beats) ? grid.beats.slice() : [];
  _bars = Array.isArray(grid?.bars) ? grid.bars.slice() : [];
  _onsets = Array.isArray(grid?.onsets) ? grid.onsets.slice() : [];
  _anchors = new Set();
  _undo = [];
  _redo = [];
  _dirty = false;
  _onChange = onChange || null;

  _host = document.querySelector(".waves-column");
  _canvas = document.getElementById("beatgrid-canvas");
  if (!_host || !_canvas) return false;

  _canvas.addEventListener("pointermove", _onPointerMove);
  _canvas.addEventListener("pointerdown", _onPointerDown);
  _canvas.addEventListener("pointerup", _onPointerUp);
  _canvas.addEventListener("pointercancel", _onPointerUp);
  _canvas.addEventListener("contextmenu", _onContextMenu);

  _resizeObs = new ResizeObserver(() => _resize());
  _resizeObs.observe(_host);

  storeGet(PREFS_KEY, null).then((p) => {
    if (p && typeof p === "object") {
      if (typeof p.ripple === "boolean") _ripple = p.ripple;
      if (typeof p.snap === "boolean") _snap = p.snap;
    }
  }).catch((e) => console.warn("[beatgrid] prefs load failed:", e));

  _resize();
  return true;
}

export function destroyBeatGrid() {
  flushBeatGrid();
  if (_canvas) {
    _canvas.removeEventListener("pointermove", _onPointerMove);
    _canvas.removeEventListener("pointerdown", _onPointerDown);
    _canvas.removeEventListener("pointerup", _onPointerUp);
    _canvas.removeEventListener("pointercancel", _onPointerUp);
    _canvas.removeEventListener("contextmenu", _onContextMenu);
    _canvas.classList.add("hidden");
    const c = _canvas.getContext("2d");
    c?.clearRect(0, 0, _canvas.width, _canvas.height);
  }
  _resizeObs?.disconnect();
  _resizeObs = null;
  _jobId = null;
  _beats = [];
  _bars = [];
  _onsets = [];
  _anchors = new Set();
  _undo = [];
  _redo = [];
  _editing = false;
  _hoverIdx = -1;
  _dragIdx = -1;
  _canvas = null;
  _host = null;
  _onChange = null;
}

export function setBeatGridEditing(on) {
  _editing = !!on;
  if (!_canvas) return;
  _canvas.classList.toggle("hidden", !_editing);
  _canvas.style.pointerEvents = _editing ? "auto" : "none";
  if (!_editing) { _hoverIdx = -1; _dragIdx = -1; flushBeatGrid(); }
  _render();
}

export const isBeatGridEditing = () => _editing;

export function setBeatGridTool(tool) {
  _tool = ["move", "insert", "delete", "bar"].includes(tool) ? tool : "move";
  if (_canvas) _canvas.style.cursor = _tool === "insert" ? "copy" : "default";
}
export const getBeatGridTool = () => _tool;

export function setBeatGridRipple(on) {
  _ripple = !!on;
  storeSet(PREFS_KEY, { ripple: _ripple, snap: _snap })
    .catch((e) => console.warn("[beatgrid] prefs save failed:", e));
}
export const getBeatGridRipple = () => _ripple;

export function setBeatGridSnap(on) {
  _snap = !!on;
  _render();
  storeSet(PREFS_KEY, { ripple: _ripple, snap: _snap })
    .catch((e) => console.warn("[beatgrid] prefs save failed:", e));
}
export const getBeatGridSnap = () => _snap;

/** Indices the user has explicitly positioned; ripple interpolates between them. */
export const getAnchors = () => [..._anchors].sort((a, b) => a - b);
export const getBeats = () => _beats.slice();
export const getBars = () => _bars.map((b) => ({ ...b }));
