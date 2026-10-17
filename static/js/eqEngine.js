// Public EQ API for the studio: the one module the EQ panels talk to.
// Contract: docs/plans/0.21.0/32-eq-engine-contract.md.
//
// Lanes are mixer lane names (stems, "original", recording track ids) plus
// MASTER for the master slot. Each lane's EQ is a descriptor (eqMath.js,
// schema v1) kept in the lane's mixer entry, mixerState[lane].fx, so it is
// saved per song in the same store as volume and mute (stemdeck:mix:<jobId>)
// and comes back when the song opens. The master slot keeps its own entry,
// mixerState[MASTER], in that same store.
//
// The descriptor is the truth; the audio engine is told about every change.
// Nothing here needs an engine to exist: an EQ set while a song loads is
// pushed once the engine is ready (applyAllLaneEq), and the response curve is
// computed from the descriptor, not read from nodes.

import { isPractice, onModeChange } from "./mode.js";
import { mixerState, audioEngine } from "./state.js";
import { persistMix } from "./mixer.js";
import {
  EQ_ID, encodeFxParam, eqIsFlat, eqResponseDb, flatEq, normalizeEq,
} from "./eqMath.js";

export {
  EQ_TYPES, EQ_SLOPES, EQ_LIMITS, EQ_VERSION, flatEq, normalizeEq, bandResponseDb,
} from "./eqMath.js";

/** Lane id of the master slot (never a mixer lane name). */
export const MASTER = "__master";

const listeners = new Set();

function entryOf(laneId) {
  if (laneId === MASTER) {
    if (!mixerState[MASTER] || typeof mixerState[MASTER] !== "object") mixerState[MASTER] = { fx: [] };
    return mixerState[MASTER];
  }
  const e = mixerState[laneId];
  return e && typeof e === "object" ? e : null;
}

function storedEq(entry) {
  const list = Array.isArray(entry?.fx) ? entry.fx : [];
  return list.find((x) => x && typeof x === "object" && x.id === EQ_ID) || null;
}

function emit(laneId, eq) {
  for (const cb of listeners) {
    try { cb({ laneId, eq }); } catch (err) { console.warn("[eq] change listener failed:", err); }
  }
}

function pushToEngine(laneId, eq, opts) {
  if (!audioEngine?.supportsEq) return;
  // Practice mode (mode.js) bypasses every EQ, live and in exports, without
  // touching what is saved: the filters stay warm behind a crossfade.
  const live = isPractice() ? { ...eq, bypass: true } : eq;
  try {
    if (laneId === MASTER) audioEngine.setMasterEq(live, opts);
    else audioEngine.setLaneEq(laneId, live, opts);
  } catch (err) {
    console.warn("[eq] could not apply the EQ to the engine:", err);
  }
}

/** The lane's EQ, normalised (a fresh copy; flat when none is saved). */
export function getLaneEq(laneId) {
  return normalizeEq(storedEq(entryOf(laneId)));
}

/**
 * Set a lane's whole EQ. The descriptor is normalised (clamped, rounded,
 * unknown fields dropped), applied to the playing engine with smoothed
 * parameters, saved with the song's mix, and announced to onChange
 * listeners. Returns the normalised descriptor, or null for an unknown lane.
 */
export function setLaneEq(laneId, descriptor, { save = true } = {}) {
  const entry = entryOf(laneId);
  if (!entry) return null;
  const eq = normalizeEq(descriptor);
  const others = (Array.isArray(entry.fx) ? entry.fx : []).filter((x) => x?.id !== EQ_ID);
  // EQ is first in a slot; effects added later (mastering) follow it.
  const untouched = !eq.bypass && eq.params.out === 0 && !eq.params.bands.length && !eq.params.ui;
  entry.fx = untouched ? others : [eq, ...others];
  pushToEngine(laneId, eq);
  if (save) persistMix();
  emit(laneId, eq);
  return eq;
}

/** Bypass (A/B) a lane's EQ without losing its settings. Saved. */
export function setBypass(laneId, on) {
  const eq = getLaneEq(laneId);
  eq.bypass = !!on;
  return setLaneEq(laneId, eq);
}

/** Back to flat (no bands, 0 dB output, not bypassed, panel state cleared). */
export function resetLane(laneId) {
  return setLaneEq(laneId, flatEq());
}

/**
 * Play a different EQ on the lane without saving or announcing it: an A/B
 * "compare" (preview the flat or bypassed version), or soloing one band
 * (preview a single band-pass at that band). Pass null to go back to the
 * lane's saved EQ. Never reaches the store or the export.
 */
export function previewLaneEq(laneId, descriptor) {
  if (!entryOf(laneId)) return null;
  const eq = descriptor === null ? getLaneEq(laneId) : normalizeEq(descriptor);
  pushToEngine(laneId, eq);
  return eq;
}

/** True when the lane's EQ changes the sound (for the "EQ on" indicator). */
export function isLaneActive(laneId) {
  return !eqIsFlat(getLaneEq(laneId));
}

/**
 * Listen for EQ changes: cb({laneId, eq}). laneId is null after a song's
 * EQs were loaded or re-applied as a whole. Returns an unsubscribe function.
 */
export function onChange(cb) {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

/** The sample rate the live EQ runs at (44100 when no engine is up). */
export function liveSampleRate() {
  return audioEngine?.audioContext?.sampleRate || 44100;
}

/**
 * Magnitude response of the lane's EQ in dB at each frequency in `freqs`
 * (Hz), output gain included, computed from the descriptor at the live
 * rate (or `sampleRate`). Bypassed or flat: all zeros. Float32Array.
 */
export function getFrequencyResponse(laneId, freqs, { sampleRate } = {}) {
  return Float32Array.from(eqResponseDb(getLaneEq(laneId), freqs, sampleRate || liveSampleRate()));
}

/**
 * The response read back from the engine's own BiquadFilterNodes (what is
 * sounding now), or null without an engine. For checks, not for drawing.
 */
export function getNodeResponse(laneId, freqs) {
  if (!audioEngine?.supportsEq) return null;
  const db = laneId === MASTER
    ? audioEngine.getMasterNodeResponseDb(freqs)
    : audioEngine.getLaneNodeResponseDb(laneId, freqs);
  return db ? Float32Array.from(db) : null;
}

/**
 * An AnalyserNode on the lane's signal before ("pre") or after ("post") its
 * EQ, for a spectrum display. Created and connected on first call, shared by
 * later calls; call releaseAnalyser when the display closes so it costs
 * nothing. Null when no engine is playing the lane. `opts`: {fftSize,
 * smoothing} for a newly created analyser (default 8192, 0.8). The engine is
 * rebuilt on every song load: ask again after a load.
 */
export function getAnalyser(laneId, which = "post", opts) {
  if (!audioEngine?.supportsEq) return null;
  const w = which === "pre" ? "pre" : "post";
  return laneId === MASTER ? audioEngine.getMasterTap(w, opts) : audioEngine.getLaneTap(laneId, w, opts);
}

export function releaseAnalyser(laneId, which = "post") {
  if (!audioEngine?.supportsEq) return;
  const w = which === "pre" ? "pre" : "post";
  if (laneId === MASTER) audioEngine.releaseMasterTap(w);
  else audioEngine.releaseLaneTap(laneId, w);
}

/**
 * Push every saved EQ into the current engine at once (no ramps). Called by
 * the player when an engine is ready and when a song's mix has loaded.
 */
export function applyAllLaneEq() {
  for (const name of Object.keys(mixerState)) {
    if (name === MASTER) continue;
    const eq = getLaneEq(name);
    pushToEngine(name, eq, { immediate: true });
  }
  pushToEngine(MASTER, getLaneEq(MASTER), { immediate: true });
  emit(null, null);
}

/**
 * The export `fx=` parameter for these lanes plus the master slot:
 * base64url JSON, or "" when no EQ changes the sound.
 */
export function exportFxParam(laneNames) {
  // Practice mode: the export sounds like what plays, with no EQ.
  if (isPractice()) return "";
  const lanes = {};
  for (const name of laneNames || []) lanes[name] = getLaneEq(name);
  return encodeFxParam(lanes, getLaneEq(MASTER));
}

// Practice and DAW: the saved EQs go to the engine again, bypassed in
// Practice, as they are in DAW. Ramped like any change, so playback does not
// click.
onModeChange(() => {
  for (const name of Object.keys(mixerState)) {
    if (name === MASTER) continue;
    pushToEngine(name, getLaneEq(name));
  }
  pushToEngine(MASTER, getLaneEq(MASTER));
});
