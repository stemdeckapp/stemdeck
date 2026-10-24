// Loopback latency calibration (05 section 5.5).
//
// Five 2 ms noise bursts, 600 ms apart, go straight to ctx.destination (past
// the tempo worklet, whose latency the engine reports separately). The same
// recorder that records takes captures them; each burst is found by
// cross-correlation, and the round trip is the median of the clicks that
// agree within 2 ms. About 4 seconds of sound; about 20 seconds for the user
// with the instructions.
//
// Results are kept per input device and context rate in localStorage. The
// browser's own latency figures are optimistic by tens of milliseconds
// (Kaufman's measurements, MDN), which is why this exists.

import {
  CAL_CLICKS, CAL_SPACING_SEC, makeBurst, measureRoundTrips, summariseCalibration,
} from "./takeMath.js";

const KEY_PREFIX = "stemdeck:capture:latency:";
const LEAD_SEC = 0.3;
const TAIL_SEC = 0.6;

export function calibrationKey(deviceId, rate) {
  return `${KEY_PREFIX}${deviceId || "default"}@${rate}`;
}

export function loadCalibration(deviceId, rate) {
  try {
    const raw = localStorage.getItem(calibrationKey(deviceId, rate));
    if (!raw) return null;
    const cal = JSON.parse(raw);
    return Number.isFinite(cal?.roundTripMs) ? cal : null;
  } catch (e) {
    console.warn("[capture] could not read the stored calibration:", e);
    return null;
  }
}

export function saveCalibration(deviceId, rate, cal) {
  try {
    localStorage.setItem(calibrationKey(deviceId, rate), JSON.stringify(cal));
  } catch (e) {
    console.warn("[capture] could not store the calibration:", e);
  }
}

export function clearCalibration(deviceId, rate) {
  try {
    localStorage.removeItem(calibrationKey(deviceId, rate));
  } catch (e) {
    console.warn("[capture] could not clear the calibration:", e);
  }
}

/**
 * Run one calibration on an open capture. Playback must be stopped first.
 * Resolves { ok: true, roundTripMs, driftPpm, ... } and stores it, or
 * { ok: false, reason: "too-quiet" | "inconsistent" | "busy" }.
 */
export async function runCalibration(capture, { onProgress } = {}) {
  const ctx = capture.ctx;
  if (ctx.state === "suspended") {
    try { await ctx.resume(); } catch (e) { console.warn("[capture] resume failed:", e); }
  }
  const rate = ctx.sampleRate;
  const burst = makeBurst(rate);
  const buf = ctx.createBuffer(1, burst.length, rate);
  buf.copyToChannel(burst, 0);

  const blocks = [];
  let frame0;
  try {
    ({ frame: frame0 } = await capture.recorder.start((m) => {
      const chs = m.channels;
      if (chs.length === 1) { blocks.push(chs[0]); return; }
      const mono = new Float32Array(chs[0].length);
      for (let i = 0; i < mono.length; i++) mono[i] = (chs[0][i] + chs[1][i]) / 2;
      blocks.push(mono);
    }));
  } catch (e) {
    console.warn("[capture] calibration could not start the recorder:", e);
    return { ok: false, reason: "busy" };
  }

  const t0 = ctx.currentTime + LEAD_SEC;
  const clickFrames = [];
  for (let k = 0; k < CAL_CLICKS; k++) {
    const when = t0 + k * CAL_SPACING_SEC;
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);
    src.start(when);
    clickFrames.push(Math.round(when * rate));
  }
  const endAt = t0 + (CAL_CLICKS - 1) * CAL_SPACING_SEC + TAIL_SEC;
  await new Promise((resolve) => {
    const tick = () => {
      const left = endAt - ctx.currentTime;
      onProgress?.(Math.max(0, Math.min(1, 1 - left / (endAt - t0 + LEAD_SEC))));
      if (left <= 0) resolve();
      else setTimeout(tick, 100);
    };
    tick();
  });
  await capture.recorder.stop();

  let total = 0;
  for (const b of blocks) total += b.length;
  const signal = new Float32Array(total);
  let off = 0;
  for (const b of blocks) { signal.set(b, off); off += b.length; }

  const rts = measureRoundTrips(signal, rate, frame0, clickFrames, burst);
  const summary = summariseCalibration(rts);
  if (!summary.ok) return summary;
  const cal = {
    roundTripMs: summary.roundTripMs,
    driftPpm: summary.driftPpm,
    rate,
    measuredAt: Date.now(),
  };
  saveCalibration(capture.deviceId, rate, cal);
  capture.applyCalibration(cal);
  return { ...summary, ...cal };
}
