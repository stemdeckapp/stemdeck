// The effects chain of one lane, as data (Advanced mode only). No DOM, no
// Web Audio, so tests/js/fx-chain.test.mjs runs it in node.
//
// One descriptor rides in the lane's mixer entry, mixerState[lane].fx, after
// the EQ descriptor, so it is saved per song, per mode and per version in the
// same record (mixer.js mixKey). Shape, schema v1:
//
//   { id: "chain", v: 1,
//     inst: "guitar" | "bass" | "voice" | null,   // null: follow the lane
//     on: true,                                   // the whole chain
//     trim: 0,                                    // input trim, dB
//     pedals: [ { iid, type, on, preset, p: { control id: value } } ] }
//
// ARRAY ORDER IS SIGNAL ORDER. A list cannot be cyclic or unpatched, which a
// free cable graph could be, with silence and no error (report 2.4). Each
// pedal has its own instance id, so one type can appear twice and a reorder
// is a permutation of ids. Values are in real units (seconds, Hz, dB, 0..1).
//
// Anything read from the store is untrusted: normalizeChain clamps every
// value to its control, drops unknown keys and types, caps the length and
// keeps the ids unique, as normalizeEq does for the EQ.

import {
  INSTRUMENTS, DEFAULT_ORDER, SYNC_FACTORS, pedalSpec, controlsFor, pedalDefaults, presetById,
  clampControl, mixGains,
} from "./pedalSpecs.js";

export const CHAIN_ID = "chain";
export const CHAIN_VERSION = 1;
/** At most this many pedals on one lane (report D9). */
export const MAX_PEDALS = 8;
export const TRIM_MIN = -24;
export const TRIM_MAX = 24;
/**
 * The longest tail a replaced chain is kept for, and the longest tail an
 * export print adds past the song's end (report 3.7 and 3.12, proposal).
 */
export const TAIL_CAP_SEC = 4;
/**
 * Bumped whenever the DSP of any pedal changes what it outputs for the same
 * settings: it is part of every export print's key, so a print made by an
 * older build is never reused (fxPrint.js, app/api/fx_prints.py).
 */
export const RENDERER_VERSION = 1;

const IID_RE = /^[a-z0-9]{4,8}$/;

/** The instrument a lane gets by default, from its panel kind (eqPanelKinds.js). */
export function laneInstrument(panelKind) {
  if (panelKind === "bass") return "bass";
  if (panelKind === "voice") return "voice";
  // Guitar, and every lane the name cannot place (other, piano, drums, a
  // line input): the guitar voicings are the general pedal ones.
  return "guitar";
}

/** A lane that can carry a chain: a single stem or a recording track (D4). */
export function laneTakesChain(lane) {
  return typeof lane === "string" && lane !== "" && lane !== "original" && lane !== "mix" && !lane.startsWith("__");
}

export function emptyChain() {
  return { id: CHAIN_ID, v: CHAIN_VERSION, inst: null, on: true, trim: 0, pedals: [] };
}

/** A new instance id, unique among `taken`. `rand` is Math.random by default. */
export function newIid(taken = [], rand = Math.random) {
  const used = new Set(taken);
  for (let i = 0; i < 64; i++) {
    const id = Math.floor(rand() * 36 ** 6).toString(36).padStart(6, "0").slice(-6);
    if (IID_RE.test(id) && !used.has(id)) return id;
  }
  // 64 collisions in a row cannot happen with a working random source; count
  // up from a fixed base so an id is still unique.
  let n = 0;
  while (used.has(`p${String(n).padStart(4, "0")}`)) n++;
  return `p${String(n).padStart(4, "0")}`;
}

function normalizeParams(type, raw, inst) {
  const src = raw && typeof raw === "object" ? raw : {};
  const out = {};
  for (const c of pedalSpec(type).controls) {
    out[c.id] = c.id in src ? clampControl(c, src[c.id], inst) : clampControl(c, undefined, inst);
  }
  return out;
}

/** The instrument a chain is voiced for: its own choice, or the lane's. */
export function chainInstrument(desc, laneDefault) {
  const own = desc?.inst;
  if (INSTRUMENTS.includes(own)) return own;
  return INSTRUMENTS.includes(laneDefault) ? laneDefault : "guitar";
}

/**
 * A chain descriptor made safe: every value clamped, unknown fields and
 * pedal types dropped, at most MAX_PEDALS, unique ids. `laneDefault` is the
 * lane's instrument (for the defaults of a control that is missing).
 */
export function normalizeChain(raw, laneDefault = "guitar") {
  const out = emptyChain();
  if (!raw || typeof raw !== "object") return out;
  out.inst = INSTRUMENTS.includes(raw.inst) ? raw.inst : null;
  out.on = raw.on !== false;
  const trim = Number(raw.trim);
  out.trim = Number.isFinite(trim) ? Math.round(Math.max(TRIM_MIN, Math.min(TRIM_MAX, trim)) * 10) / 10 : 0;
  const inst = chainInstrument(out, laneDefault);
  const seen = [];
  for (const p of Array.isArray(raw.pedals) ? raw.pedals : []) {
    if (out.pedals.length >= MAX_PEDALS) break;
    if (!p || typeof p !== "object" || !pedalSpec(p.type)) continue;
    let iid = typeof p.iid === "string" && IID_RE.test(p.iid) && !seen.includes(p.iid) ? p.iid : null;
    if (!iid) iid = newIid(seen);
    seen.push(iid);
    const preset = typeof p.preset === "string" && presetById(p.preset)?.type === p.type ? p.preset : null;
    out.pedals.push({ iid, type: p.type, on: p.on !== false, preset, p: normalizeParams(p.type, p.p, inst) });
  }
  return out;
}

/** True when a chain is worth saving: anything set at all. */
export function chainIsSet(desc) {
  return !!desc && (desc.pedals.length > 0 || desc.trim !== 0 || desc.inst !== null || desc.on === false);
}

/**
 * True when the chain changes the sound: on, and a pedal on or a trim. An
 * export prints only such a lane.
 */
export function chainIsActive(desc) {
  return !!desc && desc.on !== false && (desc.trim !== 0 || desc.pedals.some((p) => p.on));
}

// ─── Editing (each returns a new descriptor) ───

const clone = (d) => JSON.parse(JSON.stringify(d));

/**
 * Where a pedal of `type` goes by default: after the last pedal already
 * there whose type comes no later in the instrument's order (report 3.5).
 */
export function defaultIndex(pedals, type, inst) {
  const order = DEFAULT_ORDER[inst] || DEFAULT_ORDER.guitar;
  const rank = (t) => {
    const i = order.indexOf(t);
    return i < 0 ? order.length : i;
  };
  const r = rank(type);
  let at = 0;
  pedals.forEach((p, i) => { if (rank(p.type) <= r) at = i + 1; });
  return at;
}

/** Add a pedal (its instrument defaults, or a preset). Null when full. */
export function addPedal(desc, type, inst, { presetId = null, rand = Math.random } = {}) {
  if (!pedalSpec(type) || desc.pedals.length >= MAX_PEDALS) return null;
  const next = clone(desc);
  const iid = newIid(next.pedals.map((p) => p.iid), rand);
  const preset = presetId ? presetById(presetId) : null;
  const p = preset && preset.type === type ? { ...pedalDefaults(type, inst), ...preset.p } : pedalDefaults(type, inst);
  const pedal = { iid, type, on: true, preset: preset ? preset.id : null, p: normalizeParams(type, p, inst) };
  next.pedals.splice(defaultIndex(next.pedals, type, inst), 0, pedal);
  return next;
}

export function removePedal(desc, iid) {
  const next = clone(desc);
  next.pedals = next.pedals.filter((p) => p.iid !== iid);
  return next;
}

/** Move a pedal to `to` (0-based, clamped). Unchanged when it is already there. */
export function movePedal(desc, iid, to) {
  const from = desc.pedals.findIndex((p) => p.iid === iid);
  if (from < 0) return clone(desc);
  const next = clone(desc);
  const [pedal] = next.pedals.splice(from, 1);
  const at = Math.max(0, Math.min(next.pedals.length, Math.round(to)));
  next.pedals.splice(at, 0, pedal);
  return next;
}

export function setPedalOn(desc, iid, on) {
  const next = clone(desc);
  for (const p of next.pedals) if (p.iid === iid) p.on = !!on;
  return next;
}

/** One control of one pedal, clamped. A knob turn leaves the preset. */
export function setPedalParam(desc, iid, key, value, inst) {
  const next = clone(desc);
  const p = next.pedals.find((x) => x.iid === iid);
  if (!p) return next;
  const c = pedalSpec(p.type).controls.find((x) => x.id === key);
  if (!c) return next;
  p.p[key] = clampControl(c, value, inst);
  p.preset = null;
  return next;
}

export function applyPreset(desc, iid, presetId, inst) {
  const preset = presetById(presetId);
  const next = clone(desc);
  const p = next.pedals.find((x) => x.iid === iid);
  if (!p || !preset || preset.type !== p.type) return next;
  p.p = normalizeParams(p.type, { ...pedalDefaults(p.type, inst), ...preset.p }, inst);
  p.preset = preset.id;
  return next;
}

/**
 * The chain for another instrument: controls the new instrument does not
 * have go back to their defaults (a voice's ducking leaves with the voice),
 * presets of the old instrument are dropped, every other value is kept.
 */
export function setInstrument(desc, inst, laneDefault) {
  const next = clone(desc);
  next.inst = INSTRUMENTS.includes(inst) ? inst : null;
  const now = chainInstrument(next, laneDefault);
  for (const p of next.pedals) {
    const shown = new Set(controlsFor(p.type, now).map((c) => c.id));
    const defs = pedalDefaults(p.type, now);
    for (const key of Object.keys(p.p)) if (!shown.has(key)) p.p[key] = defs[key];
    if (p.preset && presetById(p.preset)?.inst !== now) p.preset = null;
  }
  return next;
}

// ─── Tempo ───

/** The song's tempo from its beat grid (median beat interval), or null. */
export function bpmFromBeats(beats) {
  if (!Array.isArray(beats) || beats.length < 2) return null;
  const d = [];
  for (let i = 1; i < beats.length; i++) {
    const x = beats[i] - beats[i - 1];
    if (Number.isFinite(x) && x > 0) d.push(x);
  }
  if (!d.length) return null;
  d.sort((a, b) => a - b);
  const med = d[d.length >> 1];
  const bpm = 60 / med;
  return Number.isFinite(bpm) && bpm >= 20 && bpm <= 400 ? Math.round(bpm * 1000) / 1000 : null;
}

/** Seconds of a synced division at `bpm`, or null when it cannot sync. */
export function syncSeconds(sync, bpm) {
  const f = SYNC_FACTORS[sync];
  if (!f || !(bpm > 0)) return null;
  return (60 / bpm) * f;
}

/**
 * The delay time a delay pedal plays: the synced division when Sync is on
 * and the song has a tempo, its Time knob otherwise; in the knob's range.
 */
export function delaySeconds(p, bpm) {
  const spec = pedalSpec("delay").controls.find((c) => c.id === "time");
  const synced = p.sync && p.sync !== "off" ? syncSeconds(p.sync, bpm) : null;
  const t = synced ?? p.time;
  return Math.max(spec.min, Math.min(spec.max, t));
}

// ─── What the engine needs to know ───

/**
 * The part of a pedal's settings that cannot change on a running node graph
 * (a different node layout, or a change that would click): when it differs,
 * the chain is built again and crossfaded (chainHost.js). Everything else is
 * a smooth AudioParam ramp.
 */
export function pedalStructureKey(pedal, bpm) {
  if (pedal.type === "delay") {
    const p = pedal.p;
    // A digital delay crossfades to a new line on a time change; analog and
    // tape glide. The modulation rate restarts the LFO in phase with the song
    // (so the live sound and the export print agree, fxPrint.js).
    const time = p.char === "digital" ? delaySeconds(p, bpm).toFixed(6) : "";
    const rate = p.mod > 0 ? Number(p.rate).toFixed(4) : "";
    return `${p.char}|${p.stereo}|${time}|${rate}|${p.duck > 0 ? "d" : ""}`;
  }
  if (pedal.type === "chorus") {
    const p = pedal.p;
    // The node layout (shape, stereo, crossover and feedback on or off) and
    // the LFO rate (restarted in phase with the song, as the delay's).
    return `${p.shape}|${p.stereo}|${p.xover > 30 ? "x" : ""}|${p.fb > 0 ? "f" : ""}|${Number(p.rate).toFixed(4)}`;
  }
  if (pedal.type === "phaser") {
    const p = pedal.p;
    // The LFO rate as played (synced to the bar when Sync is on).
    const rate = p.sync === "bar" && bpm > 0 ? bpm / 240 : p.rate;
    return `${p.stages}|${p.shape}|${p.stereo}|${p.xover > 30 ? "x" : ""}|${Number(rate).toFixed(5)}`;
  }
  if (pedal.type === "drive") {
    // The clip curve and the cabinet are node layouts; everything else ramps.
    return `${pedal.p.clip}|${pedal.p.cab}|${pedal.p.xover > 30 ? "x" : ""}`;
  }
  if (pedal.type === "reverb") {
    // The IR's inputs: a new IR is a new convolver, crossfaded (irGen.js).
    // Pre-delay too: sliding a delay line tens of milliseconds in 20 ms is a
    // Doppler chirp (measured: the steepest step rose by 40 %), so a new
    // pre-delay crossfades like a digital delay's time (the IR is cached).
    return `${pedal.p.type}|${Number(pedal.p.decay).toFixed(3)}|${Number(pedal.p.drip).toFixed(3)}|${Number(pedal.p.predelay).toFixed(2)}`;
  }
  return "";
}

/**
 * The structure of a whole chain: order, types and each structure key.
 * `opts.lowLatency` (a live input's Low latency switch, inputFx.js) builds
 * other nodes (a distortion without oversampling), so it is part of it: a
 * switch crossfades like a reorder.
 */
export function chainStructureKey(desc, inst, bpm, opts = {}) {
  const low = opts.lowLatency ? "low|" : "";
  return `${low}${inst}#${desc.pedals.map((p) => `${p.iid}:${p.type}:${pedalStructureKey(p, bpm)}`).join(",")}`;
}

/** How long a pedal keeps sounding after its input stops, in seconds. */
export function pedalTailSec(pedal, bpm) {
  if (pedal.type === "delay") {
    const t = delaySeconds(pedal.p, bpm) + pedal.p.mod / 1000;
    const fb = pedal.p.fb;
    // Repeats until -60 dB: fb^n = 1e-3. The loop filters only shorten it.
    const repeats = fb > 0.001 ? Math.ceil(Math.log(1e-3) / Math.log(fb)) : 0;
    return t * (repeats + 1);
  }
  if (pedal.type === "reverb") {
    // Pre-delay plus the decay to -60 dB (the export print keeps it, capped).
    return pedal.p.predelay / 1000 + pedal.p.decay;
  }
  if (pedal.type === "chorus") {
    // The longest line, and with feedback its repeats down to -60 dB.
    const t = (pedal.p.dtime + pedal.p.offset) / 1000 + 0.003;
    const fb = pedal.p.fb;
    const repeats = fb > 0.001 ? Math.ceil(Math.log(1e-3) / Math.log(fb)) : 0;
    return t * (repeats + 1);
  }
  return 0;
}

/** The longest tail of a chain, capped at TAIL_CAP_SEC. */
export function chainTailSec(desc, bpm) {
  let tail = 0;
  for (const p of desc?.pedals || []) tail = Math.max(tail, pedalTailSec(p, bpm));
  return Math.min(TAIL_CAP_SEC, tail);
}

/**
 * The fixed latency a pedal adds to its whole output, in frames. `facts` is
 * what the browser measured at the context's rate ({shaper4x}: the 4x
 * waveshaper's latency, dspBlocks.shaperLatencyFrames). A distortion delays
 * its clean paths and its bypass by the same amount, so it has this latency
 * whether it is on or off. With `opts.lowLatency` the waveshaper runs
 * without oversampling (drivePedal.js), which adds nothing.
 */
export function pedalLatencyFrames(pedal, facts, opts = {}) {
  if (pedal.type !== "drive" || opts.lowLatency) return 0;
  return Math.max(0, facts?.shaper4x || 0);
}

/** The latency of a chain in frames: the sum over its pedals. */
export function chainLatencyFrames(desc, facts, opts = {}) {
  if (!desc || desc.on === false) return 0;
  let n = 0;
  for (const p of desc.pedals) n += pedalLatencyFrames(p, facts, opts);
  return n;
}

/** Dry and wet gains of a pedal for its instrument's mix law. */
export function pedalMix(pedal, inst) {
  const spec = pedalSpec(pedal.type);
  return mixGains(pedal.p.mix, spec?.law?.[inst] || "pedal");
}

/**
 * The chain as the export print's key: only what changes the sound, in a
 * fixed order, as one string. Equal strings render the same audio.
 */
export function canonicalChain(desc, inst, bpm) {
  const pedals = desc.pedals.filter((p) => p.on).map((p) => {
    const keys = Object.keys(p.p).sort();
    const params = {};
    for (const k of keys) params[k] = p.p[k];
    return { type: p.type, p: params };
  });
  // Any synced pedal (a delay's division, a phaser's bar) follows the tempo.
  const usesTempo = desc.pedals.some((p) => p.on && p.p.sync && p.p.sync !== "off");
  return JSON.stringify({
    r: RENDERER_VERSION,
    inst,
    trim: desc.trim,
    bpm: usesTempo ? bpm ?? null : null,
    pedals,
  });
}
