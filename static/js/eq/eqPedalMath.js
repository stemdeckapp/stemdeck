// The pedal-style panels (Bass EQ, Guitar EQ) and the Keys EQ, the pure
// part: each panel's controls, their ranges, how a knob's position maps to a
// value, and how a set of control values becomes an EQ descriptor on the
// shared engine. No DOM, so tests/js/eq-panels.test.mjs runs it in node.
//
// Functional references (public manuals, read 2026-10-06; product names
// appear only here, never in the UI):
//   Bass EQ   = Empress Bass ParaEq: three parametric bands (low 35-500 Hz,
//               mid 100-1.6k Hz, high 375-9.7k Hz), +-15 dB each, Q knob from
//               wide (Q about 1, 1.5 octaves) through medium (about 2.5, at
//               noon) to narrow (about 4, 1/3 octave); high-pass 10-200 Hz and
//               low-pass 215 Hz-22 kHz, both 12 dB/oct; a low shelf +-15 dB
//               whose break frequency moves with the boost (below 75 Hz at a
//               little boost, up to 400 Hz at full); a high shelf +-15 dB
//               above 1 kHz; a clean boost 0-30 dB.
//   Guitar EQ = Empress ParaEq MKII Deluxe: the same layout with low 35-500,
//               mid 250-5k and high 1k-20k Hz; high-pass 10-330 Hz and
//               low-pass 1.5-22 kHz at 12 dB/oct; low shelf below 200 Hz and
//               high shelf above 1 kHz, +-15 dB.
//   Keys EQ   = a basic take on the tone and mix controls of Spectrasonics
//               Keyscape (bass and treble, low/mid/treble bands, a "boost"
//               of the very high frequencies, a low cut, a colour shift).
//
// What the engine cannot copy: 6 dB/oct and Baxandall shelves (its shelves
// are the Web Audio RBJ shelf, slope 1, centred on the given frequency), and
// a 22 kHz low-pass (its limit is 20 kHz). The boost is the lane's output
// gain, which the engine allows from -24 to +24 dB; a cut is offered too,
// since a mix needs a trim after a boost more often than more level.

import { normalizeEq, EQ_VERSION } from "../eqMath.js";

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const num = (v, d) => (typeof v === "number" && Number.isFinite(v) ? v : d);

// ─── Knob scales: position 0..1 <-> value ───

export function logPos(v, min, max) {
  return clamp(Math.log(clamp(v, min, max) / min) / Math.log(max / min), 0, 1);
}
export function logVal(p, min, max) {
  return min * (max / min) ** clamp(p, 0, 1);
}
export function linPos(v, min, max) {
  return clamp((v - min) / (max - min), 0, 1);
}
export function linVal(p, min, max) {
  return min + clamp(p, 0, 1) * (max - min);
}

// The pedals' Q knob: wide (Q 1) fully left, medium (2.5) at noon, narrow
// (4) fully right, each half geometric.
export const Q_WIDE = 1;
export const Q_MID = 2.5;
export const Q_NARROW = 4;
export function qVal(p) {
  const x = clamp(p, 0, 1);
  return x <= 0.5 ? Q_WIDE * (Q_MID / Q_WIDE) ** (x / 0.5) : Q_MID * (Q_NARROW / Q_MID) ** ((x - 0.5) / 0.5);
}
export function qPos(q) {
  const v = clamp(q, Q_WIDE, Q_NARROW);
  return v <= Q_MID
    ? 0.5 * (Math.log(v / Q_WIDE) / Math.log(Q_MID / Q_WIDE))
    : 0.5 + 0.5 * (Math.log(v / Q_MID) / Math.log(Q_NARROW / Q_MID));
}

// ─── The pedals ───

export const PEDAL_GAIN = 15;
export const LEVEL_MIN = -24;
export const LEVEL_MAX = 24;
export const SHELF_GAIN = 15;

export const PEDALS = Object.freeze({
  bass: Object.freeze({
    low: { min: 35, max: 500 },
    mid: { min: 100, max: 1600 },
    high: { min: 375, max: 9700 },
    hpf: { min: 10, max: 200 },
    lpf: { min: 215, max: 20000 },
    lowShelf: { dynamic: true, fMin: 75, fMax: 400, f: 75 },
    highShelf: { f: 1000 },
  }),
  guitar: Object.freeze({
    low: { min: 35, max: 500 },
    mid: { min: 250, max: 5000 },
    high: { min: 1000, max: 20000 },
    hpf: { min: 10, max: 330 },
    lpf: { min: 1500, max: 20000 },
    lowShelf: { f: 200 },
    highShelf: { f: 1000 },
  }),
});

const geo = (r) => Math.sqrt(r.min * r.max);

/** Every control at its starting point: frequencies at noon, flat, filters open. */
export function pedalDefaults(kind) {
  const p = PEDALS[kind];
  return {
    lf: round1(geo(p.low)), lg: 0, lq: Q_MID,
    mf: round1(geo(p.mid)), mg: 0, mq: Q_MID,
    hf: round1(geo(p.high)), hg: 0, hq: Q_MID,
    hpf: p.hpf.min, lpf: p.lpf.max,
    ls: 0, hs: 0, lvl: 0,
  };
}

/** Control values made safe: numbers clamped into each control's range. */
export function normalizePedal(kind, raw) {
  const p = PEDALS[kind];
  const d = pedalDefaults(kind);
  const c = raw && typeof raw === "object" ? raw : {};
  const g = (v) => round2(clamp(num(v, 0), -PEDAL_GAIN, PEDAL_GAIN));
  const q = (v) => round3(clamp(num(v, Q_MID), Q_WIDE, Q_NARROW));
  return {
    lf: round1(clamp(num(c.lf, d.lf), p.low.min, p.low.max)), lg: g(c.lg), lq: q(c.lq),
    mf: round1(clamp(num(c.mf, d.mf), p.mid.min, p.mid.max)), mg: g(c.mg), mq: q(c.mq),
    hf: round1(clamp(num(c.hf, d.hf), p.high.min, p.high.max)), hg: g(c.hg), hq: q(c.hq),
    hpf: round1(clamp(num(c.hpf, d.hpf), p.hpf.min, p.hpf.max)),
    lpf: round1(clamp(num(c.lpf, d.lpf), p.lpf.min, p.lpf.max)),
    ls: round2(clamp(num(c.ls, 0), -SHELF_GAIN, SHELF_GAIN)),
    hs: round2(clamp(num(c.hs, 0), -SHELF_GAIN, SHELF_GAIN)),
    lvl: round2(clamp(num(c.lvl, 0), LEVEL_MIN, LEVEL_MAX)),
  };
}

/**
 * The bass pedal's low shelf frequency for a shelf gain: below 75 Hz for a
 * little boost or cut (up to 3 dB, one hour of the knob), moving up to
 * 400 Hz at the full 15 dB, geometrically in between.
 */
export function dynamicShelfFreq(g, fMin = 75, fMax = 400) {
  const a = Math.abs(g);
  if (a <= 3) return fMin;
  return round1(fMin * (fMax / fMin) ** ((Math.min(a, SHELF_GAIN) - 3) / (SHELF_GAIN - 3)));
}

/** Whether the high-pass knob is at its stop (off) or the low-pass at its top (off). */
export const hpfOn = (kind, hz) => hz > PEDALS[kind].hpf.min + 0.05;
export const lpfOn = (kind, hz) => hz < PEDALS[kind].lpf.max - 0.05;

/**
 * The EQ descriptor of a pedal: seven bands in fixed places (high-pass, low
 * shelf, low, mid and high bells, high shelf, low-pass), so turning a knob
 * never reorders the engine's filters. A band at 0 dB, or a filter at its
 * stop, is neutral (no filter runs). The controls ride along in params.ui.
 */
export function pedalEq(kind, controls, { bypass = false, ui = {} } = {}) {
  const p = PEDALS[kind];
  const c = normalizePedal(kind, controls);
  const lsF = p.lowShelf.dynamic ? dynamicShelfFreq(c.ls, p.lowShelf.fMin, p.lowShelf.fMax) : p.lowShelf.f;
  const bands = [
    { on: hpfOn(kind, c.hpf), type: "lowcut", f: c.hpf, g: 0, q: 0.707, slope: 12 },
    { on: true, type: "lowshelf", f: lsF, g: c.ls, q: 0.707, slope: 12 },
    { on: true, type: "bell", f: c.lf, g: c.lg, q: c.lq, slope: 12 },
    { on: true, type: "bell", f: c.mf, g: c.mg, q: c.mq, slope: 12 },
    { on: true, type: "bell", f: c.hf, g: c.hg, q: c.hq, slope: 12 },
    { on: true, type: "highshelf", f: p.highShelf.f, g: c.hs, q: 0.707, slope: 12 },
    { on: lpfOn(kind, c.lpf), type: "highcut", f: c.lpf, g: 0, q: 0.707, slope: 12 },
  ];
  return normalizeEq({
    id: "eq", v: EQ_VERSION, bypass,
    params: { out: c.lvl, bands, ui: { ...ui, panel: kind, c } },
  });
}

// ─── Keys ───

export const KEYS = Object.freeze({
  body: { f: 120, range: 12 },
  warmth: { f: 250, q: 0.8, range: 12 },
  presence: { min: 2000, max: 5000, q: 1, range: 12 },
  air: { f: 10000, range: 12 },
  tone: { f: 1000, range: 6 },
  lowcut: { min: 20, max: 300 },
  level: { min: -12, max: 12 },
});

export function keysDefaults() {
  return { body: 0, warmth: 0, presence: 0, presF: 3000, air: 0, tone: 0, lowcut: KEYS.lowcut.min, lvl: 0 };
}

export function normalizeKeys(raw) {
  const c = raw && typeof raw === "object" ? raw : {};
  const g = (v, range) => round2(clamp(num(v, 0), -range, range));
  return {
    body: g(c.body, KEYS.body.range),
    warmth: g(c.warmth, KEYS.warmth.range),
    presence: g(c.presence, KEYS.presence.range),
    presF: round1(clamp(num(c.presF, 3000), KEYS.presence.min, KEYS.presence.max)),
    air: g(c.air, KEYS.air.range),
    tone: g(c.tone, KEYS.tone.range),
    lowcut: round1(clamp(num(c.lowcut, KEYS.lowcut.min), KEYS.lowcut.min, KEYS.lowcut.max)),
    lvl: round2(clamp(num(c.lvl, 0), KEYS.level.min, KEYS.level.max)),
  };
}

/**
 * The Keys EQ descriptor: low cut, body (low shelf), warmth (bell), presence
 * (bell, sweepable), air (high shelf) and tone (tilt around 1 kHz), in
 * fixed places, with the level as output gain.
 */
export function keysEq(controls, { bypass = false, ui = {} } = {}) {
  const c = normalizeKeys(controls);
  const bands = [
    { on: c.lowcut > KEYS.lowcut.min + 0.05, type: "lowcut", f: c.lowcut, g: 0, q: 0.707, slope: 12 },
    { on: true, type: "lowshelf", f: KEYS.body.f, g: c.body, q: 0.707, slope: 12 },
    { on: true, type: "bell", f: KEYS.warmth.f, g: c.warmth, q: KEYS.warmth.q, slope: 12 },
    { on: true, type: "bell", f: c.presF, g: c.presence, q: KEYS.presence.q, slope: 12 },
    { on: true, type: "highshelf", f: KEYS.air.f, g: c.air, q: 0.707, slope: 12 },
    { on: true, type: "tilt", f: KEYS.tone.f, g: c.tone, q: 0.707, slope: 12 },
  ];
  return normalizeEq({
    id: "eq", v: EQ_VERSION, bypass,
    params: { out: c.lvl, bands, ui: { ...ui, panel: "keys", c } },
  });
}

/**
 * The controls a panel shows for a lane's saved EQ: the ones it saved, when
 * this panel saved them; the starting point otherwise (an EQ never set, or
 * reset).
 */
export function controlsFromEq(kind, eq) {
  const ui = eq?.params?.ui;
  const mine = ui && ui.panel === kind && ui.c && typeof ui.c === "object" ? ui.c : null;
  if (kind === "keys") return normalizeKeys(mine || keysDefaults());
  return normalizePedal(kind, mine || pedalDefaults(kind));
}

/** A panel's descriptor from its controls (pedal or keys). */
export function panelEq(kind, controls, opts) {
  return kind === "keys" ? keysEq(controls, opts) : pedalEq(kind, controls, opts);
}

function round1(v) { return Math.round(v * 10) / 10; }
function round2(v) { return Math.round(v * 100) / 100; }
function round3(v) { return Math.round(v * 1000) / 1000; }
