// The waveform of a take while it is being recorded, drawn as the audio
// arrives, in the track's lane.
//
// Without it a take shows nothing until Stop: its waveform comes from the
// server's peaks.json, made when the take is finished. This draws the same
// bars from the recorder's own batches (takeRecorder.onTakeAudio, the arrays
// that are uploaded; no second capture path), placed in song time with the
// same formula the take is placed with (takeMath.songTimeAtCtx), so the live
// bars start where the take starts and grow behind the playhead.
//
// Lifetime, per track:
//   mountLiveWave(trackId, el)  right after startTake. `el` is the lane's
//       anchor (the .take-live-slot of the track's main lane); null falls
//       back to the overview row of the track.
//   recording   bars grow as batches arrive.
//   "stopping"  the bars stay, the last batch still lands.
//   the reload after Stop draws the server's bars for the take, and this
//       hands over on "stemdeck:overview-drawn": the live lane goes in the same
//       task the new bars are drawn, so there is no gap and no jump.
//   "discarded" or "failed": gone at once.
//   unmountLiveWave(trackId): gone at once (job change, track delete).
//
// The canvas is not inside the anchor: the reload after Stop rebuilds every
// lane, and a canvas inside one would vanish before the server bars arrive.
// It lives in its own element in .waves-column (which the reload keeps), laid
// over the anchor's box, re-measured on every overview draw and every resize.
//
// Cost: one requestAnimationFrame loop for all tracks, running only while a
// batch is waiting to be drawn. A frame redraws only the bars the new audio
// touched (a batch is about 85 ms, one or two bars); a full redraw happens on
// a resize or zoom, or when the take gets louder than every other lane (the
// shared normalisation moves). Nothing is allocated per frame.

import { onTakeAudio, onTakeEvent } from "./takeRecorder.js";
import { songTimeAtCtx } from "./takeMath.js";
import {
  createPeaks, addBlock, takeDirty, barCount, barsForPoints, barAmplitude, barRect, liveNorm,
  serverPointSec, parseWavFormat,
} from "./liveWaveMath.js";
import { totalDuration } from "../state.js";
import { TAKE_COLOR } from "../constants.js";
import { overviewPeakMax } from "../player.js";

const TRACK_ID = /^k[0-9a-f]{8}$/;
// Batches held while the engine's clock is not ready yet (the count-in).
// About 85 ms each: 240 is 20 seconds, longer than any count-in.
const MAX_PENDING = 240;
// After the reload is asked for, the live bars go at the latest after this,
// even if the server's bars never come (peaks.json could not be read).
const HANDOFF_TIMEOUT_MS = 30_000;
// A canvas wider than this fails to allocate in Chromium and WebKit.
const MAX_CANVAS_PX = 32_000;

const states = new Map();
const scratchRange = [0, 0];
const scratchBars = [0, 0];
const scratchRect = [0, 0, 0, 0];

const stats = {
  frames: 0,
  drawMs: 0,
  maxDrawMs: 0,
  fullRedraws: 0,
  batches: 0,
  batchMs: 0,
  maxBatchMs: 0,
};

let rafId = 0;
let hostObserver = null;

// ─── Public API ───

/**
 * Show the live waveform of the take about to be recorded on `trackId`.
 * Safe to call again for the same track (a rebuilt anchor): a take still
 * recording keeps its bars.
 */
export function mountLiveWave(trackId, containerEl = null) {
  if (!TRACK_ID.test(String(trackId))) return;
  let st = states.get(trackId);
  if (st && st.phase === "live") {
    if (containerEl) st.anchorEl = containerEl;
    place(st);
    return;
  }
  if (st) dropLane(st);
  st = {
    trackId,
    anchorEl: containerEl || null,
    phase: "live",
    takeId: null,
    peaks: null,
    mapping: null,
    pending: [],
    // Length of one peaks.json point: undefined while the stems' WAV header
    // is read, then the server's grid (0 = unknown, duration / points).
    pointSec: undefined,
    reloadSeen: false,
    drawsAfterReload: 0,
    timer: 0,
    lane: null,
    canvas: null,
    g: null,
    cssW: 0,
    cssH: 0,
    top: NaN,
    scaleX: 1,
    scaleY: 1,
    bars: 0,
    otherMax: 0,
    norm: 0,
    color: TAKE_COLOR,
    needsFull: true,
  };
  states.set(trackId, st);
  st.otherMax = safeOtherMax(trackId);
  stemGrid().then((pointSec) => {
    if (states.get(trackId) !== st) return;
    st.pointSec = pointSec;
    if (flushPending(st)) schedule();
  });
}

// ─── The server's point grid ───

let gridUrl = "";
let gridPromise = null;

/**
 * The server's peaks.json point length for this job, from the first 4 KB of
 * one stem WAV (every stem and every take share its format). Read once per
 * job; 0 when it cannot be read, and the grid falls back to duration / points.
 */
function stemGrid() {
  const link = document.querySelector('.mixer-column .lane-header[data-stem]:not([data-take]) .lane-dl[href*="/api/"]');
  const url = link?.getAttribute("href") || "";
  if (!url) return Promise.resolve(0);
  if (url === gridUrl && gridPromise) return gridPromise;
  gridUrl = url;
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), 3000);
  gridPromise = fetch(url, { headers: { Range: "bytes=0-4095" }, signal: ac.signal })
    .then((r) => (r.ok ? r.arrayBuffer() : null))
    .then((buf) => {
      const fmt = parseWavFormat(buf);
      return fmt ? serverPointSec(fmt.frames, fmt.sampleRate) : 0;
    })
    .catch((e) => {
      console.warn("[liveWave] reading the stems' format failed; using an approximate grid:", e);
      return 0;
    })
    .finally(() => clearTimeout(timer));
  return gridPromise;
}

/** Remove the live waveform of `trackId`, now. Idempotent. */
export function unmountLiveWave(trackId) {
  const st = states.get(trackId);
  if (st) dropLane(st);
}

/** Draw and batch costs since load, for the performance check (e2e). */
export function getLiveWaveStats() {
  const lanes = {};
  for (const [id, st] of states) {
    lanes[id] = {
      phase: st.phase,
      takeId: st.takeId,
      lastSongSec: st.peaks && Number.isFinite(st.peaks.lastSongSec) ? st.peaks.lastSongSec : null,
      bars: st.bars,
      width: st.cssW,
      height: st.cssH,
    };
  }
  return { ...stats, lanes };
}

// ─── Audio ───

onTakeAudio((ev) => {
  const st = states.get(ev.trackId);
  if (!st || (st.phase !== "live" && st.phase !== "saving")) return;
  if (ev.takeId) {
    if (st.takeId && st.takeId !== ev.takeId) return;
    st.takeId = ev.takeId;
  }
  const t0 = performance.now();
  if (ev.mapping) st.mapping = ev.mapping;
  // Held until the clock is ready (the count-in) and the grid is known.
  st.pending.push(ev);
  if (st.pending.length > MAX_PENDING) st.pending.shift();
  flushPending(st);
  const ms = performance.now() - t0;
  stats.batches++;
  stats.batchMs += ms;
  if (ms > stats.maxBatchMs) stats.maxBatchMs = ms;
  if (!st.lane) ensureLane(st);
  schedule();
});

/** Place every held batch once the clock and the grid allow. True if any. */
function flushPending(st) {
  if (!st.mapping || st.pointSec === undefined || !st.pending.length) return false;
  if (!st.peaks) {
    if (!(totalDuration > 0)) return false;
    st.peaks = createPeaks(totalDuration, st.pending[0].channels.length, undefined, st.pointSec);
  }
  for (const p of st.pending) placeBlock(st, p);
  st.pending.length = 0;
  return true;
}

function placeBlock(st, ev) {
  const start = songTimeAtCtx(ev.frame / ev.sampleRate, st.mapping, ev.roundTripSec);
  addBlock(st.peaks, ev.channels, start, ev.sampleRate);
  // The take got louder than every other lane: the finished lane will be
  // normalised to it, so every bar drawn so far changes height.
  const norm = liveNorm(st.otherMax, st.peaks.level);
  if (norm !== st.norm) {
    st.norm = norm;
    st.needsFull = true;
  }
}

// ─── Take events ───

onTakeEvent((ev) => {
  const st = ev.trackId ? states.get(ev.trackId) : null;
  if (!st) return;
  if (ev.takeId && st.takeId && ev.takeId !== st.takeId) return;
  if (ev.type === "stopping") {
    st.phase = "saving";
  } else if (ev.type === "discarded" || ev.type === "failed") {
    dropLane(st);
  } else if (ev.type === "finished") {
    // Kept. The reload after Stop has normally drawn the server's bars by now;
    // if not, the next overview draw hands over.
    st.phase = "saving";
    markReload(st);
  }
});

document.addEventListener("stemdeck:takes-changed", () => {
  for (const st of states.values()) if (st.phase === "saving") markReload(st);
});

document.addEventListener("stemdeck:overview-drawn", () => {
  for (const st of [...states.values()]) {
    if (st.phase === "saving" && st.reloadSeen) {
      st.drawsAfterReload++;
      if (rowHasBars(st.trackId) || st.drawsAfterReload >= 2) {
        handOver(st);
        continue;
      }
    }
    st.otherMax = safeOtherMax(st.trackId);
    const norm = st.peaks ? liveNorm(st.otherMax, st.peaks.level) : 0;
    if (norm !== st.norm) {
      st.norm = norm;
      st.needsFull = true;
    }
    place(st);
  }
  schedule();
});

function markReload(st) {
  if (st.reloadSeen) return;
  st.reloadSeen = true;
  clearTimeout(st.timer);
  st.timer = setTimeout(() => dropLane(st), HANDOFF_TIMEOUT_MS);
}

function rowHasBars(trackId) {
  return !!document.querySelector(`.stem-waveform-row[data-stem="${trackId}"] .stem-waveform-svg rect`);
}

/** The server's bars are drawn: say what the live bars were, then go. */
function handOver(st) {
  // Measured against the layout the server's bars were just drawn in (the
  // reload can change the zoom and the lane heights).
  place(st);
  if (st.lane && st.peaks && st.bars > 0) {
    // For the e2e check that the switch does not jump: the live bar heights
    // in CSS px at the moment the server's bars replaced them.
    const heights = new Float32Array(st.bars);
    for (let i = 0; i < st.bars; i++) {
      const amp = barAmplitude(st.peaks, i, st.bars, 0);
      heights[i] = amp < 0 ? -1 : barRect(i, st.bars, st.cssW, st.cssH, amp, st.norm, scratchRect)[3];
    }
    document.dispatchEvent(new CustomEvent("stemdeck:live-wave-handoff", {
      detail: { trackId: st.trackId, takeId: st.takeId, bars: st.bars, height: st.cssH, heights },
    }));
  }
  dropLane(st);
}

function dropLane(st) {
  clearTimeout(st.timer);
  st.lane?.remove();
  st.lane = null;
  st.canvas = null;
  st.g = null;
  st.phase = "gone";
  if (states.get(st.trackId) === st) states.delete(st.trackId);
  if (!states.size && hostObserver) {
    hostObserver.disconnect();
    hostObserver = null;
  }
}

function safeOtherMax(trackId) {
  try {
    return overviewPeakMax(trackId) || 0;
  } catch (e) {
    console.warn("[liveWave] reading the lanes' peak level failed:", e);
    return 0;
  }
}

// ─── Placement ───

/** The element whose box the live lane covers. */
function findAnchor(st) {
  if (st.anchorEl?.isConnected) return st.anchorEl;
  const slot = document.querySelector(`.take-live-slot[data-track-id="${st.trackId}"]`);
  if (slot) {
    st.anchorEl = slot;
    return slot;
  }
  return document.querySelector(`.stem-waveform-row[data-stem="${st.trackId}"]`);
}

function ensureLane(st) {
  const host = document.querySelector(".waves-column");
  if (!host) return;
  const lane = document.createElement("div");
  lane.className = "live-wave-lane";
  lane.dataset.trackId = st.trackId;
  lane.setAttribute("aria-hidden", "true");
  const canvas = document.createElement("canvas");
  canvas.className = "live-wave-canvas";
  lane.append(canvas);
  host.append(lane);
  st.lane = lane;
  st.canvas = canvas;
  st.g = canvas.getContext("2d");
  st.top = NaN;
  st.cssW = 0;
  st.cssH = 0;
  if (!hostObserver && typeof ResizeObserver === "function") {
    // A window resize changes every lane's height without an overview draw.
    hostObserver = new ResizeObserver(() => {
      for (const s of states.values()) place(s);
      schedule();
    });
  }
  hostObserver?.observe(host);
  place(st);
}

/**
 * Lay the live lane over its anchor and size the canvas. Writes styles only
 * when something moved. Offsets, not client rects: the app's CSS zoom scales
 * rects but not the lengths written back.
 */
function place(st) {
  const lane = st.lane;
  if (!lane) return;
  const host = lane.parentElement;
  const anchor = findAnchor(st);
  if (!host) return;
  let top = st.top;
  let height = st.cssH;
  if (anchor) {
    let y = anchor.clientTop || 0;
    let el = anchor;
    while (el && el !== host) {
      y += el.offsetTop;
      el = el.offsetParent;
    }
    if (el === host) {
      top = y;
      height = anchor.clientHeight;
    }
  }
  const width = host.clientWidth;
  if (!Number.isFinite(top) || !(height > 0) || !(width > 0)) return;
  if (top !== st.top) {
    lane.style.top = `${top}px`;
    st.top = top;
  }
  if (width !== st.cssW || height !== st.cssH) {
    lane.style.height = `${height}px`;
    st.cssW = width;
    st.cssH = height;
    const dpr = window.devicePixelRatio || 1;
    const bw = Math.max(1, Math.min(MAX_CANVAS_PX, Math.round(width * dpr)));
    const bh = Math.max(1, Math.round(height * dpr));
    st.canvas.width = bw;
    st.canvas.height = bh;
    st.scaleX = bw / width;
    st.scaleY = bh / height;
    st.bars = barCount(width);
    st.color = readTakeColor();
    st.needsFull = true;
  }
}

function readTakeColor() {
  const v = getComputedStyle(document.documentElement).getPropertyValue("--take").trim();
  return v || TAKE_COLOR;
}

// ─── Drawing ───

function schedule() {
  if (rafId) return;
  rafId = requestAnimationFrame(frame);
}

function frame() {
  rafId = 0;
  const t0 = performance.now();
  let drew = false;
  for (const st of states.values()) {
    if (!st.g || !st.peaks || !st.bars) continue;
    if (st.needsFull) {
      drawAll(st);
      drew = true;
    } else if (takeDirty(st.peaks, scratchRange)) {
      drawRange(st, scratchRange[0], scratchRange[1]);
      drew = true;
    }
  }
  if (!drew) return;
  const ms = performance.now() - t0;
  stats.frames++;
  stats.drawMs += ms;
  if (ms > stats.maxDrawMs) stats.maxDrawMs = ms;
}

function drawAll(st) {
  st.needsFull = false;
  takeDirty(st.peaks, scratchRange);
  stats.fullRedraws++;
  const g = st.g;
  g.setTransform(1, 0, 0, 1, 0, 0);
  g.clearRect(0, 0, st.canvas.width, st.canvas.height);
  g.fillStyle = st.color;
  g.globalAlpha = 0.92;
  const last = Math.min(st.bars - 1, Math.ceil((st.peaks.filledTo * st.bars) / st.peaks.points));
  for (let i = 0; i <= last; i++) drawBar(st, i);
}

function drawRange(st, lo, hi) {
  barsForPoints(lo, hi, st.bars, st.peaks.points, scratchBars);
  const g = st.g;
  g.fillStyle = st.color;
  g.globalAlpha = 0.92;
  const slot = (st.cssW / st.bars) * st.scaleX;
  for (let i = scratchBars[0]; i <= scratchBars[1]; i++) {
    // The bar's whole slot, rounded outward, so a bar that grew leaves no
    // sliver of its old self.
    const x0 = Math.floor(i * slot);
    g.clearRect(x0, 0, Math.ceil((i + 1) * slot) - x0, st.canvas.height);
    drawBar(st, i);
  }
}

function drawBar(st, i) {
  const amp = barAmplitude(st.peaks, i, st.bars, 0);
  if (amp < 0) return;
  barRect(i, st.bars, st.cssW, st.cssH, amp, st.norm, scratchRect);
  st.g.fillRect(
    scratchRect[0] * st.scaleX,
    scratchRect[1] * st.scaleY,
    scratchRect[2] * st.scaleX,
    scratchRect[3] * st.scaleY,
  );
}
