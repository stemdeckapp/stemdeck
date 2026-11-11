// Public API of the effects chains: the one module the FX panels, the player
// and the export talk to. Advanced mode, plus Performance's own take lane
// (laneChainAllowed) and the live inputs of both (inputChainsAllowed).
//
// Each lane's chain is a descriptor (fx/chainModel.js, schema v1) kept in the
// lane's mixer entry, mixerState[lane].fx, after the EQ, so it is saved per
// song, per mode and per version in the same record as volume, mute and EQ
// (mixer.js mixKey) and comes back when the song opens. No new store key.
//
// The descriptor is the truth; the engine is told about every change, but
// only while the mix in force is Advanced ("daw"). In Practice, Karaoke and
// Performance every lane is told "no chain", so the engine builds no node at
// all and the graph is the graph of 0.21.0 (fx/chainHost.js). The saved
// chains are not touched by that: a mode keeps its own record of a song
// (mixMode.js), and Advanced's record keeps its chains.

import { mixerState, audioEngine, metronome } from "./state.js";
import { persistMix, getMixMode } from "./mixer.js";
import { isTakeName } from "./constants.js";
import { panelKindFor } from "./eq/eqPanelKinds.js";
import { getTracks, trackInput } from "./capture/takeLanes.js";
import {
  CHAIN_ID, normalizeChain, chainIsSet, chainIsActive, chainInstrument, laneInstrument, laneTakesChain,
} from "./fx/chainModel.js";
import { loopOverheadFrames, shaperLatencyFrames } from "./fx/dspBlocks.js";

export {
  CHAIN_ID, MAX_PEDALS, TRIM_MIN, TRIM_MAX, addPedal, removePedal, movePedal, setPedalOn, setPedalParam,
  applyPreset, setInstrument, chainIsActive,
} from "./fx/chainModel.js";

const listeners = new Set();
let tempoBpm = null;
// The measured feedback-loop overhead per context rate (dspBlocks.js).
const loopExtraByRate = new Map();

/** True while the mix in force is Advanced: the only mode with chains. */
export function chainsAllowed() {
  return getMixMode() === "daw";
}

// Where a live input can be heard through effects, by mix mode, and the
// surface that shows its controls: Advanced's recording panel (armed tracks
// with "Hear myself", capture/liveFxUi.js) and Performance's side panel (its
// one input, streamer/streamerFx.js). Practice and Karaoke show no input to
// arm, so they are not here. A mode is added or removed here and nowhere else.
const INPUT_CHAIN_SURFACE = Object.freeze({ daw: "studio", streamer: "performance" });

/**
 * True where a live input can be heard through effects. `surface` ("studio"
 * or "performance") asks for one: each controller builds chains only on its
 * own surface, so the recording panel's armed tracks build nothing in
 * Performance and Performance's input nothing in Advanced. Without it: any.
 */
export function inputChainsAllowed(surface) {
  const here = INPUT_CHAIN_SURFACE[getMixMode()];
  return !!here && (surface == null || surface === here);
}

// The lane of the open song's Performance track (streamer/streamerFx.js tells
// it), or null. Its take is the clean input, played back through a copy of the
// live chain it was heard through, so in Performance that one lane plays its
// chain; every other lane stays without, as in Practice and Karaoke.
let performanceLane = () => null;

/** Performance tells which lane is its own track's (a function, read each time). */
export function setPerformanceLane(fn) {
  performanceLane = typeof fn === "function" ? fn : () => null;
}

/**
 * True when `lane` plays its chain in the mix in force: every lane that takes
 * one in Advanced (chainsAllowed), and in Performance only its own track's.
 */
export function laneChainAllowed(lane) {
  if (chainsAllowed()) return true;
  if (getMixMode() !== "streamer" || !lane) return false;
  try {
    return lane === performanceLane();
  } catch (err) {
    console.warn("[fx] reading the Performance track failed:", err);
    return false;
  }
}

/**
 * What the pedals need measured at a context's rate (the feedback-loop
 * overhead and the 4x waveshaper's latency), once per rate. The live
 * input's chains (capture/inputFx.js) use the same numbers as the lanes.
 */
export function chainFacts(rate) {
  return loopExtraFor(rate);
}

function entryOf(lane) {
  const e = mixerState[lane];
  return e && typeof e === "object" ? e : null;
}

function storedChain(entry) {
  const list = Array.isArray(entry?.fx) ? entry.fx : [];
  return list.find((x) => x && typeof x === "object" && x.id === CHAIN_ID) || null;
}

/** The instrument a lane has by default, from its name or track input. */
export function laneDefaultInstrument(lane) {
  if (!isTakeName(lane)) return laneInstrument(panelKindFor(lane));
  const track = getTracks().find((tr) => tr.id === lane);
  let inputKind = null;
  try {
    inputKind = track ? trackInput(track)?.kind || null : null;
  } catch (e) {
    console.warn("[fx] reading a track's input kind failed:", e);
  }
  return laneInstrument(panelKindFor(lane, { isTrack: true, inputKind }));
}

/** The lane's chain, normalised (a fresh copy; empty when none is saved). */
export function getLaneChain(lane) {
  return normalizeChain(storedChain(entryOf(lane)), laneDefaultInstrument(lane));
}

/** The instrument the lane's chain is voiced for now. */
export function laneChainInstrument(lane) {
  return chainInstrument(getLaneChain(lane), laneDefaultInstrument(lane));
}

/** The song's tempo the delays sync to (null when it has no beat grid). */
export function chainTempo() {
  if (tempoBpm) return tempoBpm;
  // A grid the metronome holds but the player has not passed on yet.
  const live = metronome?.getEffectiveBpm?.();
  const mult = metronome?.getMultiplier?.() || 1;
  return Number.isFinite(live) && live > 0 ? live / mult : null;
}

function emit(lane) {
  for (const cb of listeners) {
    try { cb({ lane }); } catch (err) { console.warn("[fx] change listener failed:", err); }
  }
}

/** Listen for chain changes: cb({lane}); lane is null after a whole reload. */
export function onChange(cb) {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

function loopExtraFor(rate) {
  let p = loopExtraByRate.get(rate);
  if (!p) {
    // Every pedal loop holds exactly one delay line (delayPedal.js); a
    // distortion's latency is its 4x waveshaper's.
    p = Promise.all([loopOverheadFrames(rate, 1), shaperLatencyFrames(rate)])
      .then(([mono, shaper4x]) => ({ mono, shaper4x }));
    loopExtraByRate.set(rate, p);
  }
  return p;
}

/** What the engine plays for a lane: a spec for fx/chainHost.js, or null. */
function laneSpec(lane, loopExtra) {
  if (!laneChainAllowed(lane) || !laneTakesChain(lane)) return null;
  const stored = storedChain(entryOf(lane));
  if (!stored) return null;
  const desc = getLaneChain(lane);
  return { desc, inst: laneChainInstrument(lane), bpm: chainTempo(), loopExtra };
}

function pushToEngine(lane, opts = {}) {
  const eng = audioEngine;
  if (!eng?.supportsChain) return;
  const rate = eng.audioContext?.sampleRate || 48000;
  // Reads the descriptor when it sends, so a burst of knob turns that
  // resolves in order always lands on the latest settings.
  const send = (loopExtra) => {
    if (audioEngine !== eng) return;
    try {
      eng.setLaneChain(lane, laneSpec(lane, loopExtra), opts);
    } catch (err) {
      console.warn("[fx] could not apply the chain to the engine:", err);
    }
  };
  // Taking chains out needs no measurement: do it now.
  if (!laneSpec(lane, null)) { send(null); return; }
  loopExtraFor(rate).then(send);
}

/**
 * Set a lane's whole chain. Normalised, applied to the playing engine
 * (smoothly, or crossfaded when the structure changed), saved with the
 * song's mix and announced. Returns the normalised chain, or null for a lane
 * that cannot have one.
 */
export function setLaneChain(lane, descriptor, { save = true } = {}) {
  const entry = entryOf(lane);
  if (!entry || !laneTakesChain(lane)) return null;
  const desc = normalizeChain(descriptor, laneDefaultInstrument(lane));
  const others = (Array.isArray(entry.fx) ? entry.fx : []).filter((x) => x?.id !== CHAIN_ID);
  // After the EQ, which is first in the list (eqEngine.js). A chain with
  // nothing set is not saved, as a flat EQ is not.
  entry.fx = chainIsSet(desc) ? [...others, desc] : others;
  pushToEngine(lane);
  if (save) persistMix();
  emit(lane);
  return desc;
}

/** True when the lane's chain changes the sound (for the FX tab's dot). */
export function isLaneActive(lane) {
  return laneChainAllowed(lane) && chainIsActive(getLaneChain(lane));
}

/**
 * Push every lane's chain into the current engine at once: when an engine
 * is ready, when a song's mix has loaded, and when the mix in force changed
 * (another mode). In any mode but Advanced this takes every chain out.
 */
export function applyAllLaneChains() {
  for (const lane of Object.keys(mixerState)) {
    if (lane.startsWith("__")) continue;
    pushToEngine(lane, { immediate: !audioEngine?.isPlaying?.() });
  }
  emit(null);
}

/** The song's tempo changed (a beat grid loaded or was edited). */
export function setChainTempo(bpm) {
  const next = Number.isFinite(bpm) && bpm > 0 ? bpm : null;
  if (next === tempoBpm) return;
  tempoBpm = next;
  for (const lane of Object.keys(mixerState)) {
    if (lane.startsWith("__")) continue;
    const desc = getLaneChain(lane);
    if (desc.pedals.some((p) => p.p.sync && p.p.sync !== "off")) pushToEngine(lane);
  }
  emit(null);
}

/** An analyser after the lane's input trim (the chain's meter), or null. */
export function getChainAnalyser(lane, opts) {
  if (!audioEngine?.supportsChain) return null;
  return audioEngine.getChainTap(lane, opts);
}

export function releaseChainAnalyser(lane) {
  if (!audioEngine?.supportsChain) return;
  audioEngine.releaseChainTap(lane);
}

/**
 * The lanes of an export whose chain changes the sound, each with what its
 * print needs. Only the lanes that play their chain (laneChainAllowed): in
 * Practice and Karaoke none, in Performance its own track's only; an export
 * carries exactly the chains playback has.
 */
export function exportChainLanes(names) {
  const out = [];
  for (const lane of names || []) {
    if (!laneChainAllowed(lane) || !laneTakesChain(lane) || !storedChain(entryOf(lane))) continue;
    const desc = getLaneChain(lane);
    if (!chainIsActive(desc)) continue;
    out.push({ lane, desc, inst: laneChainInstrument(lane), bpm: chainTempo() });
  }
  return out;
}

// The mix in force changed (Practice, Advanced, Karaoke, Performance): the
// chains follow it, out of the graph anywhere but Advanced.
document.addEventListener("stemdeck:mix-switched", () => applyAllLaneChains());
