// The master's pure maths: the saved descriptor, the export wire form, the
// presets, and what the live preview needs to stand in for the export
// (app/pipeline/mastering.py renders the exact result with ffmpeg).
//
// No DOM and no Web Audio here, so node tests cover all of it
// (tests/js/master-math.test.mjs).
//
// Descriptor, kept in the master slot's effects list after the master EQ
// (mixerState.__master.fx, 03-eq.md section 14):
//
//   { id: "master", v: 1, bypass: false,
//     params: { preset, target_lufs, ceiling_dbtp, comp } }
//
// bypass true is "mastering off": the settings are kept, nothing is applied
// live or in an export. A song with no descriptor is off as well.

import { base64UrlEncode } from "./eqMath.js";

export const MASTER_ID = "master";
export const MASTER_VERSION = 1;

/** Ranges the server accepts (mastering.py TARGET_*, CEILING_*, COMP_*). */
export const MASTER_LIMITS = {
  targetMin: -24, targetMax: -6,
  ceilingMin: -3, ceilingMax: -0.1,
  compMin: 0, compMax: 1,
};

/** Lossy exports never go above this ceiling (mastering.LOSSY_CEILING_DBTP). */
export const LOSSY_CEILING_DBTP = -1.5;
/** The server's limit sits this far under the ceiling (LIMITER_MARGIN_DB). */
export const LIMITER_MARGIN_DB = 0.1;

/**
 * The presets. target null: no loudness change, only the limiter. Custom
 * starts from the streaming numbers and keeps whatever the user types.
 */
export const MASTER_PRESETS = {
  gentle: { target: null, ceiling: -1, comp: 0 },
  streaming: { target: -14, ceiling: -1, comp: 0 },
  louder: { target: -9, ceiling: -1, comp: 0.5 },
  custom: { target: -14, ceiling: -1, comp: 0 },
};
export const PRESET_ORDER = ["gentle", "streaming", "louder", "custom"];
export const DEFAULT_PRESET = "streaming";

const round = (x, step) => Math.round(x / step) * step;
const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));
const num = (v, d) => (typeof v === "number" && Number.isFinite(v) ? v : d);

/** A descriptor with the defaults (streaming, on). */
export function defaultMaster() {
  const p = MASTER_PRESETS[DEFAULT_PRESET];
  return {
    id: MASTER_ID, v: MASTER_VERSION, bypass: false,
    params: { preset: DEFAULT_PRESET, target_lufs: p.target, ceiling_dbtp: p.ceiling, comp: p.comp },
  };
}

/**
 * Clamp, round and fill in a stored or edited descriptor. A missing one, or
 * one from a newer version, reads as off (bypassed) with the defaults.
 * A named preset always carries its own numbers; custom keeps the user's.
 */
export function normalizeMaster(raw) {
  const out = defaultMaster();
  if (!raw || typeof raw !== "object" || raw.v !== MASTER_VERSION || raw.id !== MASTER_ID) {
    out.bypass = true;
    return out;
  }
  out.bypass = raw.bypass === true;
  const p = raw.params && typeof raw.params === "object" ? raw.params : {};
  const preset = PRESET_ORDER.includes(p.preset) ? p.preset : DEFAULT_PRESET;
  out.params.preset = preset;
  if (preset !== "custom") {
    const def = MASTER_PRESETS[preset];
    out.params.target_lufs = def.target;
    out.params.ceiling_dbtp = def.ceiling;
    // The compressor amount is the user's on every preset; a preset only
    // sets its starting point (applyPreset).
    out.params.comp = round(clamp(num(p.comp, def.comp), 0, 1), 0.01);
    return out;
  }
  const L = MASTER_LIMITS;
  out.params.target_lufs = p.target_lufs === null
    ? null
    : round(clamp(num(p.target_lufs, -14), L.targetMin, L.targetMax), 0.1);
  out.params.ceiling_dbtp = round(clamp(num(p.ceiling_dbtp, -1), L.ceilingMin, L.ceilingMax), 0.1);
  out.params.comp = round(clamp(num(p.comp, 0), L.compMin, L.compMax), 0.01);
  return out;
}

/** The descriptor after picking a preset (its numbers, its compressor). */
export function applyPreset(desc, preset) {
  const cur = normalizeMaster(desc);
  const def = MASTER_PRESETS[preset] || MASTER_PRESETS[DEFAULT_PRESET];
  const keepCustom = preset === "custom" && cur.params.preset === "custom";
  return normalizeMaster({
    ...cur,
    params: keepCustom ? cur.params : {
      preset,
      target_lufs: preset === "custom" ? (cur.params.target_lufs ?? -14) : def.target,
      ceiling_dbtp: preset === "custom" ? cur.params.ceiling_dbtp : def.ceiling,
      comp: preset === "custom" ? cur.params.comp : def.comp,
    },
  });
}

export function masterIsOn(desc) {
  return !!desc && desc.bypass !== true;
}

/**
 * The export `master=` parameter, or "" when mastering is off. The server
 * validates exactly this shape (mastering.parse_master_param).
 */
export function encodeMasterParam(desc) {
  const m = normalizeMaster(desc);
  if (!masterIsOn(m)) return "";
  const body = { v: 1, target: m.params.target_lufs, ceiling: m.params.ceiling_dbtp };
  if (m.params.comp > 0) body.comp = m.params.comp;
  return base64UrlEncode(JSON.stringify(body));
}

// ─── Live preview ───

/**
 * Chromium's DynamicsCompressorNode output for a static input (its
 * "Saturate" curve, dynamics_compressor_kernel.cc), and the fixed makeup gain
 * the Web Audio spec makes every compressor apply: "the 0.6 power of full
 * range makeup gain". The live master cancels that makeup with a GainNode of
 * the inverse, so a compressor below its threshold passes the signal at
 * unity. The knee curve is implementation-defined in the spec; this is the
 * Blink kernel (WebView2, Chrome). Checked against the real node in
 * tests/e2e/master-live.spec.mjs.
 */
export function compressorMakeupGain(thresholdDb, kneeDb, ratio) {
  const f = Math.fround;
  const dbToLin = (db) => f(10 ** (db / 20));
  const linToDb = (x) => (x ? f(20 * Math.log10(x)) : -1000);
  const linearThreshold = dbToLin(thresholdDb);
  const kneeCurve = (x, k) => {
    if (x < linearThreshold) return x;
    return f(linearThreshold + (1 - Math.exp(-k * (x - linearThreshold))) / k);
  };
  const slopeAt = (x, k) => {
    if (x < linearThreshold) return 1;
    const x2 = f(x * 1.001);
    const xDb = linToDb(x);
    const x2Db = linToDb(x2);
    const yDb = linToDb(kneeCurve(x, k));
    const y2Db = linToDb(kneeCurve(x2, k));
    return f((y2Db - yDb) / (x2Db - xDb));
  };
  const kAtSlope = (desired) => {
    const x = dbToLin(thresholdDb + kneeDb);
    let minK = 0.1;
    let maxK = 10000;
    let k = 5;
    for (let i = 0; i < 15; i++) {
      const slope = slopeAt(x, k);
      if (slope < desired) maxK = k; else minK = k;
      k = f(Math.sqrt(minK * maxK));
    }
    return k;
  };
  const slope = f(1 / ratio);
  const k = kAtSlope(slope);
  const kneeThresholdDb = f(thresholdDb + kneeDb);
  const kneeThreshold = dbToLin(kneeThresholdDb);
  const yKneeThresholdDb = linToDb(kneeCurve(kneeThreshold, k));
  const saturate = (x) => {
    if (x < kneeThreshold) return kneeCurve(x, k);
    const xDb = linToDb(x);
    return dbToLin(yKneeThresholdDb + slope * (xDb - kneeThresholdDb));
  };
  const fullRangeGain = saturate(1);
  return f((1 / fullRangeGain) ** 0.6);
}

/** Glue compressor settings for an amount (mirrors mastering.comp_* ). */
export function glueSettings(comp, thresholdDb) {
  if (!(comp > 0) || !Number.isFinite(thresholdDb)) return null;
  return {
    threshold: thresholdDb,
    ratio: Math.round((1 + 2 * comp) * 1000) / 1000,
    knee: 6,
    attack: 0.02,
    release: 0.25,
  };
}

/** The live limiter: the export's limit, ratio 20 (the spec's maximum). */
export function limiterSettings(ceilingDb) {
  return {
    threshold: ceilingDb - LIMITER_MARGIN_DB,
    ratio: 20,
    knee: 0,
    attack: 0.002,
    release: 0.05,
  };
}

/**
 * Levels for a loudness-matched comparison: the louder of the master and the
 * original is turned down by the measured difference, the quieter one plays
 * as it is. `diffLu` is master minus original (post.i - pre.i). Returns
 * linear gains {master, original}.
 */
export function matchedLevels(diffLu, match = true) {
  if (!match || !Number.isFinite(diffLu)) return { master: 1, original: 1 };
  const d = clamp(diffLu, -40, 40);
  return d >= 0
    ? { master: 10 ** (-d / 20), original: 1 }
    : { master: 1, original: 10 ** (d / 20) };
}

/**
 * BS.1770 K-weighting as two biquads for a sample rate (pyloudnorm's design,
 * RBJ cookbook: a +4 dB high shelf at 1.5 kHz, Q 0.707, and a high-pass at
 * 38 Hz, Q 0.5; within 1e-4 of the standard's 48 kHz coefficients),
 * as IIRFilterNode coefficients {feedforward, feedback}.
 */
export function kWeightingCoefficients(fs) {
  const shelf = (() => {
    const G = 4;
    const Q = Math.SQRT1_2;
    const fc = 1500;
    const A = 10 ** (G / 40);
    const w0 = (2 * Math.PI * fc) / fs;
    const alpha = Math.sin(w0) / (2 * Q);
    const c = Math.cos(w0);
    const sA = Math.sqrt(A);
    const b0 = A * ((A + 1) + (A - 1) * c + 2 * sA * alpha);
    const b1 = -2 * A * ((A - 1) + (A + 1) * c);
    const b2 = A * ((A + 1) + (A - 1) * c - 2 * sA * alpha);
    const a0 = (A + 1) - (A - 1) * c + 2 * sA * alpha;
    const a1 = 2 * ((A - 1) - (A + 1) * c);
    const a2 = (A + 1) - (A - 1) * c - 2 * sA * alpha;
    return { feedforward: [b0 / a0, b1 / a0, b2 / a0], feedback: [1, a1 / a0, a2 / a0] };
  })();
  const highpass = (() => {
    const Q = 0.5;
    const fc = 38;
    const w0 = (2 * Math.PI * fc) / fs;
    const alpha = Math.sin(w0) / (2 * Q);
    const c = Math.cos(w0);
    const a0 = 1 + alpha;
    return {
      feedforward: [(1 + c) / 2 / a0, -(1 + c) / a0, (1 + c) / 2 / a0],
      feedback: [1, (-2 * c) / a0, (1 - alpha) / a0],
    };
  })();
  return { shelf, highpass };
}

/** Mean square of the last `n` samples of a buffer. */
export function meanSquareTail(buf, n) {
  const len = Math.min(n, buf.length);
  let sum = 0;
  for (let i = buf.length - len; i < buf.length; i++) sum += buf[i] * buf[i];
  return len ? sum / len : 0;
}

/** Momentary loudness (LUFS) from K-weighted per-channel mean squares. */
export function loudnessFromMeanSquares(channels) {
  const total = channels.reduce((a, b) => a + b, 0);
  return total > 0 ? -0.691 + 10 * Math.log10(total) : -Infinity;
}

/** Largest absolute sample of a buffer, as dBFS (-Infinity for silence). */
export function peakDb(buf) {
  let m = 0;
  for (let i = 0; i < buf.length; i++) {
    const a = Math.abs(buf[i]);
    if (a > m) m = a;
  }
  return m > 0 ? 20 * Math.log10(m) : -Infinity;
}

/** A meter value that rises at once and falls at most `fallDb` per update. */
export function holdFall(prev, next, fallDb) {
  if (!Number.isFinite(prev) || next >= prev) return next;
  return Math.max(next, prev - fallDb);
}

/** "-14.0", "+1.4", a middle dot for a missing number. Units are added by the caller. */
export function fmtDb(v, { sign = false, digits = 1 } = {}) {
  if (v === null || v === undefined || !Number.isFinite(v)) return String.fromCharCode(183);
  const s = v.toFixed(digits);
  return sign && v > 0 ? `+${s}` : s;
}
