// Public API of the master (the mastering effect on the mix bus). The panel,
// the footer chip, the exports and the Guide talk to this module only.
//
// - The descriptor (masterMath.js) is the truth. It is saved per song with the
//   mix, in the master slot's effects list after the master EQ
//   (mixerState.__master.fx), so it comes back when the song opens.
// - Exports carry it as master= (exportMasterParam); the server measures and
//   renders the exact master (app/pipeline/mastering.py).
// - The live preview (masterFx.js) is approximate. Its gain and compressor
//   threshold come from the server's measurement of this very mix
//   (GET mixdown/loudness, the plan the export renders), fetched again when
//   the mix changes; the browser never guesses the loudness.
// - Listening "master" or "original" and matching their loudness are a
//   transient compare (not saved); matching is remembered per viewer.

import { mixerState, audioEngine, masterVolume, currentJobId } from "./state.js";
import { persistMix } from "./mixer.js";
import * as eq from "./eqEngine.js";
import { isPractice, onModeChange } from "./mode.js";
import {
  MASTER_ID, applyPreset, encodeMasterParam, holdFall, kWeightingCoefficients,
  loudnessFromMeanSquares, masterIsOn, meanSquareTail, normalizeMaster, peakDb,
} from "./masterMath.js";

export { MASTER_PRESETS, PRESET_ORDER, MASTER_LIMITS, normalizeMaster, applyPreset } from "./masterMath.js";

const MATCH_KEY = "stemdeck.master.match";
// The mix is compared with the last one measured this often while the master
// is on, and measured again once it has stopped changing for SETTLE_MS.
const WATCH_MS = 600;
const SETTLE_MS = 700;

const listeners = new Set();
let listen = "master";
let match = readMatch();
let plan = null; // the last plan for the current song (any mix)
let planSig = ""; // the mix + master signature that plan belongs to
let planState = "idle"; // idle | measuring | ready | error
let wantedSig = "";
let mixSource = null; // () => {jobId, query} | null (player.js)
let watchTimer = 0;
let settleTimer = 0;
let inflight = null; // AbortController
let planJob = null;
// Match loudness, measured on the live preview itself. The server's plan
// gives the exact export's before/after difference, but the live master is an
// approximation of that render: on Louder for a quiet song the two differed
// by 0.53 to 0.7 dB, past the 0.5 dB a match must hold. So while the song
// plays with the drawer open, the panel's meters feed the live master's own
// In and Out loudness here, and after 3 s of sound the match uses that.
const LIVE_MATCH_MIN_READS = 30; // meter reads, 100 ms apart
let live = { sumIn: 0, sumOut: 0, n: 0, diff: NaN };

function resetLiveMatch() {
  live = { sumIn: 0, sumOut: 0, n: 0, diff: NaN };
}

/**
 * One meter read of the live master's momentary loudness at its input and
 * output (LUFS, export level). Silence is skipped.
 */
export function noteLiveLoudness(inLufs, outLufs) {
  if (!isMasterOn() || !Number.isFinite(inLufs) || !Number.isFinite(outLufs)) return;
  if (inLufs < -60 || outLufs < -60) return;
  live.sumIn += 10 ** (inLufs / 10);
  live.sumOut += 10 ** (outLufs / 10);
  live.n += 1;
  if (live.n < LIVE_MATCH_MIN_READS) return;
  const diff = 10 * Math.log10(live.sumOut / live.sumIn);
  if (Number.isFinite(live.diff) && Math.abs(diff - live.diff) < 0.05) return;
  live.diff = diff;
  if (match) applyToEngine();
}

function readMatch() {
  try {
    return localStorage.getItem(MATCH_KEY) !== "0";
  } catch (e) {
    console.warn("[master] reading the match setting failed:", e);
    return true;
  }
}

function emit(what) {
  for (const cb of listeners) {
    try { cb(what); } catch (err) { console.warn("[master] change listener failed:", err); }
  }
}

function entry() {
  if (!mixerState[eq.MASTER] || typeof mixerState[eq.MASTER] !== "object") {
    mixerState[eq.MASTER] = { fx: [] };
  }
  if (!Array.isArray(mixerState[eq.MASTER].fx)) mixerState[eq.MASTER].fx = [];
  return mixerState[eq.MASTER];
}

function stored() {
  return entry().fx.find((x) => x && typeof x === "object" && x.id === MASTER_ID) || null;
}

/** The song's master, normalised (off, with the defaults, when none is saved). */
export function getMaster() {
  return normalizeMaster(stored());
}

export function isMasterOn() {
  // Practice mode (mode.js) has no master: off, live and in exports, with
  // the saved master kept for DAW mode.
  return !isPractice() && masterIsOn(getMaster()) && !!stored();
}

/**
 * Replace the song's master. Saved with the mix (debounced), pushed to the
 * live preview, announced. Returns the normalised descriptor.
 */
export function setMaster(desc, { save = true } = {}) {
  const m = normalizeMaster(desc);
  const e = entry();
  // The master EQ stays first in the slot; the master follows it.
  e.fx = [...e.fx.filter((x) => x?.id !== MASTER_ID), m];
  resetLiveMatch();
  if (save) persistMix();
  applyToEngine();
  emit("master");
  watch();
  return m;
}

export function setMasterOn(on) {
  const cur = stored() ? getMaster() : applyPreset(null, "streaming");
  return setMaster({ ...cur, bypass: !on });
}

export function choosePreset(preset) {
  return setMaster({ ...applyPreset(getMaster(), preset), bypass: false });
}

/** The export's master= value, "" when mastering is off. */
export function exportMasterParam() {
  return isMasterOn() ? encodeMasterParam(getMaster()) : "";
}

// ─── Compare ───

export function getListen() { return listen; }
export function setListen(which) {
  listen = which === "original" ? "original" : "master";
  applyToEngine();
  emit("listen");
}
export function getMatch() { return match; }
export function setMatch(on) {
  match = !!on;
  try {
    localStorage.setItem(MATCH_KEY, match ? "1" : "0");
  } catch (e) {
    console.warn("[master] saving the match setting failed:", e);
  }
  applyToEngine();
  emit("listen");
}

// ─── The measured plan ───

/** The plan for the current mix and master (null until measured). */
export function getPlan() {
  return planSig && planSig === signature() ? plan : null;
}
/** The last plan measured for this song, even if the mix moved on since. */
export function getLastPlan() { return plan; }
export function getPlanState() { return planState; }

/** player.js tells the master how to describe the current mix. */
export function setMixSource(fn) { mixSource = fn; }
/** The current mix as the export describes it ({jobId, query}), or null. */
export function currentMixSource() { return mixSource?.() || null; }

function signature() {
  const src = mixSource?.();
  if (!src || !isMasterOn()) return "";
  return `${src.jobId}|${src.query}|${exportMasterParam()}`;
}

function stopWatch() {
  if (watchTimer) clearInterval(watchTimer);
  if (settleTimer) clearTimeout(settleTimer);
  watchTimer = 0;
  settleTimer = 0;
}

/** Start or stop following the mix, depending on whether the master is on. */
export function watch() {
  if (!isMasterOn() || !mixSource) {
    stopWatch();
    return;
  }
  if (!watchTimer) watchTimer = setInterval(check, WATCH_MS);
  check();
}

function check() {
  const sig = signature();
  if (!sig) return;
  if (sig === planSig || sig === wantedSig) return;
  wantedSig = sig;
  if (settleTimer) clearTimeout(settleTimer);
  settleTimer = setTimeout(() => {
    settleTimer = 0;
    measure();
  }, plan ? SETTLE_MS : 0);
}

/** Measure the current mix now (also what the panel's refresh does). */
export async function measure() {
  const src = mixSource?.();
  if (!src || !isMasterOn()) return null;
  const sig = signature();
  inflight?.abort();
  const ctl = new AbortController();
  inflight = ctl;
  planState = "measuring";
  emit("plan");
  const q = new URLSearchParams(src.query);
  q.set("master", exportMasterParam());
  try {
    const r = await fetch(`/api/jobs/${src.jobId}/mixdown/loudness?${q}`, { signal: ctl.signal });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const body = await r.json();
    if (inflight !== ctl) return null;
    plan = body;
    planSig = sig;
    resetLiveMatch();
    planJob = src.jobId;
    planState = "ready";
  } catch (err) {
    if (ctl.signal.aborted) return null;
    console.warn("[master] measuring the mix failed:", err);
    planState = "error";
    wantedSig = "";
  } finally {
    if (inflight === ctl) inflight = null;
  }
  applyToEngine();
  emit("plan");
  return plan;
}

/** A new song: forget the old song's plan and compare state. */
export function resetForSong() {
  inflight?.abort();
  inflight = null;
  plan = null;
  planSig = "";
  wantedSig = "";
  planState = "idle";
  listen = "master";
  resetLiveMatch();
  stopWatch();
  emit(null);
}

// ─── The live preview ───

/** The parameters the live master runs with now, or null when it is off. */
export function liveParams() {
  if (!isMasterOn()) return null;
  const m = getMaster();
  const p = planJob === currentJobId ? plan : null;
  return {
    gainDb: p ? p.gain_db : 0,
    comp: m.params.comp,
    compThresholdDb: p ? p.comp_threshold_db : null,
    ceiling: m.params.ceiling_dbtp,
    fader: masterVolume,
    listen,
    match,
    diffLu: Number.isFinite(live.diff) ? live.diff
      : p && Number.isFinite(p.post?.i) && Number.isFinite(p.pre?.i) ? p.post.i - p.pre.i : NaN,
  };
}

/**
 * Keep the live master warm while the drawer is open (fxChain warmMasterFx),
 * settled on the master the power button would switch on.
 */
let warm = false;
export function warmLive(on) {
  warm = !!on;
  if (!audioEngine?.supportsMaster || !audioEngine.warmMasterFx) return;
  try {
    const m = stored() ? getMaster() : applyPreset(null, "streaming");
    const p = planJob === currentJobId ? plan : null;
    audioEngine.warmMasterFx(on, on ? {
      gainDb: p ? p.gain_db : 0,
      comp: m.params.comp,
      compThresholdDb: p ? p.comp_threshold_db : null,
      ceiling: m.params.ceiling_dbtp,
      fader: masterVolume,
      listen: "master",
      match,
      diffLu: NaN,
    } : null);
  } catch (err) {
    console.warn("[master] could not warm the live master:", err);
  }
}

/** Push the master to the engine (the player calls it when an engine is up). */
export function applyToEngine({ immediate = false } = {}) {
  if (!audioEngine?.supportsMaster) return;
  try {
    audioEngine.setMasterFx(liveParams(), { immediate });
  } catch (err) {
    console.warn("[master] could not apply the master to the engine:", err);
  }
}

// A song's EQs were (re)loaded or the engine was rebuilt: the master too.
eq.onChange(({ laneId }) => {
  if (laneId !== null) return;
  applyToEngine({ immediate: true });
  // A rebuilt engine has no warm master yet.
  if (warm) warmLive(true);
  emit(null);
  watch();
});

export function onChange(cb) {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

// Practice and DAW: the master goes off or comes back as saved.
onModeChange(() => {
  applyToEngine();
  watch();
  emit("master");
});

// ─── Meters (approximate, for the panel) ───

/**
 * Peak and momentary loudness of the master's input and output, at export
 * level. Built on the live master's own nodes while a panel asks; read()
 * returns {in: {peak, lufs}, out: {peak, lufs}, reduction} in dB.
 * Momentary loudness: BS.1770 K-weighting (IIRFilterNode) over the last
 * 400 ms, per channel. Approximate: sample peaks, no gating.
 */
export function createMeters() {
  let built = null; // { nodes, sides, rate }
  const held = { inPeak: -Infinity, outPeak: -Infinity };

  function side(ctx, node) {
    const k = kWeightingCoefficients(ctx.sampleRate);
    const shelf = ctx.createIIRFilter(k.shelf.feedforward, k.shelf.feedback);
    const hp = ctx.createIIRFilter(k.highpass.feedforward, k.highpass.feedback);
    const kSplit = ctx.createChannelSplitter(2);
    const rawSplit = ctx.createChannelSplitter(2);
    const mk = (size) => {
      const a = ctx.createAnalyser();
      a.fftSize = size;
      a.smoothingTimeConstant = 0;
      return a;
    };
    const kA = [mk(32768), mk(32768)];
    const pA = [mk(4096), mk(4096)];
    node.connect(shelf);
    shelf.connect(hp);
    hp.connect(kSplit);
    kSplit.connect(kA[0], 0);
    kSplit.connect(kA[1], 1);
    node.connect(rawSplit);
    rawSplit.connect(pA[0], 0);
    rawSplit.connect(pA[1], 1);
    const kBuf = new Float32Array(32768);
    const pBuf = new Float32Array(4096);
    return {
      read() {
        const win = Math.round(0.4 * ctx.sampleRate);
        const ms = kA.map((a) => { a.getFloatTimeDomainData(kBuf); return meanSquareTail(kBuf, win); });
        const peaks = pA.map((a) => { a.getFloatTimeDomainData(pBuf); return peakDb(pBuf); });
        return { lufs: loudnessFromMeanSquares(ms), peak: Math.max(...peaks) };
      },
      dispose() {
        try { node.disconnect(shelf); } catch { /* gone */ }
        try { node.disconnect(rawSplit); } catch { /* gone */ }
        for (const n of [shelf, hp, kSplit, rawSplit, ...kA, ...pA]) {
          try { n.disconnect(); } catch { /* gone */ }
        }
      },
    };
  }

  function ensure() {
    const nodes = audioEngine?.getMasterFxNodes?.() || null;
    if (built && nodes && built.nodes.input === nodes.input) return built;
    if (built) { built.inS.dispose(); built.outS.dispose(); built = null; }
    if (!nodes) return null;
    const ctx = audioEngine.audioContext;
    built = { nodes, inS: side(ctx, nodes.input), outS: side(ctx, nodes.output) };
    return built;
  }

  return {
    read() {
      const b = ensure();
      if (!b) return null;
      const i = b.inS.read();
      const o = b.outS.read();
      held.inPeak = holdFall(held.inPeak, i.peak, 1.5);
      held.outPeak = holdFall(held.outPeak, o.peak, 1.5);
      const red = audioEngine?.masterFxReduction?.() || null;
      return {
        in: { peak: held.inPeak, lufs: i.lufs },
        out: { peak: held.outPeak, lufs: o.lufs },
        reduction: red ? Math.min(0, red.glue) + Math.min(0, red.limiter) : 0,
      };
    },
    dispose() {
      if (built) { built.inS.dispose(); built.outS.dispose(); built = null; }
    },
  };
}
