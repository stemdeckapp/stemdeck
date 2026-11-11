// Live input effects: an input heard through its own effects chain while
// "Hear myself" is on. One controller per surface, made by createInputFx:
// the studio's armed recording tracks (this module's own exports, the
// recording panel) and Performance's one input (streamer/streamerFx.js).
//
//   input -> the route's channels -> [trim -> pedals] -> monitor gain -> out
//
// The chain is the lanes' chain (fx/chainModel.js descriptor, fx/chainHost.js
// host, fx/pedals.js pedals), reused as it is, in the capture graph's
// monitor route (captureGraph.js). The recorder never sees it: the take is
// the clean input. When a take is saved, its track's lane gets a copy of the
// chain (fxEngine.setLaneChain), so the take plays back as it was heard.
//
// Where it runs: fxEngine.inputChainsAllowed(surface). What is built:
// nothing while there is no route or Hear myself is off
// (fx/inputChainModel.js planInputRoutes); a host per route with a chain
// otherwise.
//
// What is kept is the surface's own business (`read` and `write`): the
// studio keeps each track's chain and Low latency switch in this browser
// (captureSettings.js); Performance keeps its input's in its recording
// settings. Hear myself is never kept: it is off at every page load
// (feedback safety).

import * as fx from "../fxEngine.js";
import { armedTracks, getTracks, trackInput } from "./takeLanes.js";
import { readInputFxStore, writeInputFx } from "./captureSettings.js";
import { chainInstrument, chainIsActive, normalizeChain } from "../fx/chainModel.js";
import {
  inputDefaultInstrument, normalizeInputFx, planInputRoutes, inputHostWanted, inputChainFrames, monitorLatency,
} from "../fx/inputChainModel.js";

/**
 * A live input effects controller.
 *   surface        "studio" or "performance" (fxEngine.inputChainsAllowed)
 *   routes()       [{id, channels}] the inputs to monitor now, in order;
 *                  [] monitors the whole device as it comes, without chains
 *   read(id)       the stored record of a route (raw), or undefined
 *   write(id, rec) store a route's record {chain, low}
 *   defaultInst(id) the instrument Auto means for a route ("voice", "guitar")
 *   tag            the console's name for it
 */
export function createInputFx({ surface, routes, read, write, defaultInst, tag = "inputFx" }) {
  let capture = null;
  let monitorOn = false;
  let token = 0;
  let inited = false;
  const listeners = new Set();

  function emit(id) {
    for (const cb of listeners) {
      try { cb({ trackId: id }); } catch (err) { console.warn(`[${tag}] change listener failed:`, err); }
    }
  }

  /** Listen for changes: cb({trackId}); null when the routes changed as a whole. */
  function onInputFxChange(cb) {
    listeners.add(cb);
    return () => listeners.delete(cb);
  }

  /** A route's live input effects: {chain, low}, normalised (a fresh copy). */
  function getInputFx(id) {
    return normalizeInputFx(read(id), defaultInst(id));
  }

  /** The instrument a route's live chain is voiced for now. */
  function inputChainInstrument(id) {
    return chainInstrument(getInputFx(id).chain, defaultInst(id));
  }

  function store(id, rec) {
    write(id, rec);
    apply();
    emit(id);
  }

  /** Set a route's live chain (normalised, saved, heard at once). */
  function setInputChain(id, desc) {
    const rec = getInputFx(id);
    rec.chain = normalizeChain(desc, defaultInst(id));
    store(id, rec);
    return rec.chain;
  }

  /** The Low latency switch of a route's live chain. */
  function setInputLowLatency(id, on) {
    const rec = getInputFx(id);
    rec.low = !!on;
    store(id, rec);
  }

  /** The open input the routes are built on, or null. */
  function attachCapture(c) {
    capture = c && !c.closed ? c : null;
    apply();
  }

  /** Hear myself on or off (the chains; the capture's own setMonitor plays them). */
  function setMonitoring(on) {
    monitorOn = !!on;
    apply();
  }

  function isMonitoring() {
    return monitorOn;
  }

  function specFor(id, facts) {
    const rec = getInputFx(id);
    return {
      desc: rec.chain,
      inst: chainInstrument(rec.chain, defaultInst(id)),
      bpm: fx.chainTempo(),
      loopExtra: facts,
      lowLatency: rec.low,
    };
  }

  /**
   * Build the monitor routes for the inputs wanted now, with a chain host on
   * each route that has a chain, when monitoring is on and allowed here.
   */
  function apply() {
    const c = capture;
    const mine = ++token;
    if (!c || c.closed) return;
    const list = routes();
    if (!list.length) {
      c.setMonitorRoutes(null);
      emit(null);
      return;
    }
    const plan = planInputRoutes({
      allowed: fx.inputChainsAllowed(surface),
      monitorOn,
      tracks: list.map((r) => ({ id: r.id, channels: r.channels, desc: getInputFx(r.id).chain })),
    });
    const send = (facts) => {
      if (mine !== token || capture !== c || c.closed) return;
      c.setMonitorRoutes(plan.map((p) => ({ key: p.key, channels: p.channels, spec: p.host ? specFor(p.id, facts) : null })));
      emit(null);
    };
    // Taking chains out needs no measurement: do it now.
    if (!plan.some((p) => p.host)) { send(null); return; }
    fx.chainFacts(c.sampleRate).then(send).catch((err) => console.warn(`[${tag}] measuring the pedals failed:`, err));
  }

  /** Chain hosts in the monitor path now (0 when nothing is built). */
  function hostCount() {
    return capture && !capture.closed ? capture.chainHostCount() : 0;
  }

  /**
   * How late a route's player hears themselves: {roundTripMs, fxMs, totalMs,
   * measured}, or null with no input open. The effects' share is what the
   * chain adds as set now (its Low latency switch counted), measured facts.
   */
  async function heardLatency(id) {
    const c = capture;
    if (!c || c.closed) return null;
    const rec = getInputFx(id);
    const facts = inputHostWanted(rec.chain) ? await fx.chainFacts(c.sampleRate) : null;
    return monitorLatency({
      roundTripSec: c.roundTripSeconds,
      calibrated: c.calibrated,
      chainFrames: inputChainFrames(rec, facts),
      rate: c.sampleRate,
    });
  }

  /** The open input (for the meters and the tests), or null. */
  function inputCapture() {
    return capture && !capture.closed ? capture : null;
  }

  /**
   * A take was saved: lane `lane` gets a copy of the live chain `desc` (as it
   * was when the take began), when that chain was on and changing the sound,
   * so the take plays back as it was heard. The take itself is the clean
   * input. Returns true when copied. The instrument is written out (Auto
   * resolved with `inst`, the route's default then), so the lane does not
   * fall back to its own default.
   */
  function copyChainToLane(lane, desc, inst) {
    if (!desc || !chainIsActive(desc)) return false;
    const copy = { ...normalizeChain(desc, inst), inst: chainInstrument(desc, inst) };
    return !!fx.setLaneChain(lane, copy);
  }

  /**
   * Follow the mode (inputChainsAllowed) and the song's tempo (a synced delay
   * follows it): build again when either changes. Called once by the
   * surface's own init, not at import: fxEngine.js sits in an import cycle
   * with these modules (fxEngine, takeLanes, job, player, transport,
   * recordUi), so a top-level call into it could run before it is ready.
   */
  function init() {
    if (inited) return;
    inited = true;
    document.addEventListener("stemdeck:mix-switched", () => apply());
    fx.onChange(({ lane }) => { if (lane === null) apply(); });
  }

  return {
    surface, onInputFxChange, getInputFx, inputChainInstrument, setInputChain, setInputLowLatency,
    attachCapture, setMonitoring, isMonitoring, apply, hostCount, heardLatency, inputCapture,
    copyChainToLane, init, defaultInst,
  };
}

// ── The studio: each armed recording track's input (the recording panel) ──

/** The armed tracks that record sound (a video track records none). */
export function audioArmedTracks() {
  return armedTracks().filter((tr) => tr.kind !== "video");
}

function trackOf(trackId) {
  return getTracks().find((tr) => tr.id === trackId) || null;
}

/** The instrument a track's input is voiced for by default (its input's kind). */
export function inputDefault(trackId) {
  const track = trackOf(trackId);
  let kind = null;
  try {
    kind = track ? trackInput(track)?.kind || null : null;
  } catch (err) {
    console.warn("[inputFx] reading a track's input kind failed:", err);
  }
  return inputDefaultInstrument(kind);
}

const studio = createInputFx({
  surface: "studio",
  routes: () => audioArmedTracks().map((tr) => ({ id: tr.id, channels: trackInput(tr).channels })),
  read: (id) => readInputFxStore()[id],
  write: (id, rec) => writeInputFx(id, rec),
  defaultInst: inputDefault,
});

export const onInputFxChange = studio.onInputFxChange;
export const getInputFx = studio.getInputFx;
export const inputChainInstrument = studio.inputChainInstrument;
export const setInputChain = studio.setInputChain;
export const setInputLowLatency = studio.setInputLowLatency;
export const attachCapture = studio.attachCapture;
export const setMonitoring = studio.setMonitoring;
export const isMonitoring = studio.isMonitoring;
export const apply = studio.apply;
export const hostCount = studio.hostCount;
export const heardLatency = studio.heardLatency;
export const inputCapture = studio.inputCapture;
export const initInputFx = studio.init;

/** The studio's controller itself (liveFxUi.js hands it to the shared strip source). */
export const studioInputFx = studio;

/**
 * A take was saved on this track: its lane gets a copy of the live chain,
 * when the chain was on and changing the sound (createInputFx's
 * copyChainToLane, the track's input default resolving Auto).
 */
export function copyChainToLane(trackId, desc) {
  return studio.copyChainToLane(trackId, desc, inputDefault(trackId));
}
