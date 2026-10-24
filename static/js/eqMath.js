// EQ descriptor schema and coefficient maths, shared by the live engine
// (fxChain.js, eqEngine.js) and mirrored line for line by the export side
// (app/pipeline/fx_filters.py). No DOM, no Web Audio: node tests import it.
//
// Every band becomes one or more second-order sections, each of which is a
// Web Audio BiquadFilterNode type with its (frequency, Q, gain) parameters.
// The coefficients are the Web Audio spec's "Filters Characteristics"
// formulas (the RBJ Audio EQ Cookbook, with Q in dB for lowpass/highpass and
// the shelf slope fixed at S = 1). The export computes the same coefficients
// in Python and hands them to ffmpeg's `biquad` filter, so live and export run
// the same filter. docs/plans/0.21.0/32-eq-engine-contract.md is the contract.

export const EQ_ID = "eq";
export const EQ_VERSION = 1;

/** Band types, in the order a menu would list them. */
export const EQ_TYPES = Object.freeze([
  "bell", "lowshelf", "highshelf", "lowcut", "highcut", "notch", "bandpass", "tilt",
]);

/** Cut slopes in dB per octave (cascaded Butterworth biquads, even orders). */
export const EQ_SLOPES = Object.freeze([12, 24, 36, 48]);

export const EQ_LIMITS = Object.freeze({
  freqMin: 10,
  freqMax: 20000,
  gainMin: -30,
  gainMax: 30,
  qMin: 0.025,
  qMax: 40,
  outMin: -24,
  outMax: 24,
  maxBands: 12,
  // A stored `ui` blob (panel state owned by the EQ panels) is kept in the
  // store, never sent to the server. Bounded so a bug cannot fill the store.
  maxUiChars: 4096,
});

// Bands store Q to 0.001, so the default (and a cut's "no resonance") is
// 0.707, and a cut's resonance is measured against that same 0.707: q = 0.707
// gives exactly the Butterworth Qs.
export const EQ_DEFAULT_Q = 0.707;

// Older or hand-written names that mean the same filter.
const TYPE_ALIASES = {
  peaking: "bell", peak: "bell", highpass: "lowcut", lowpass: "highcut", hpf: "lowcut", lpf: "highcut",
};

const round = (v, step) => Math.round(v / step) * step;
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const num = (v, fallback) => (typeof v === "number" && Number.isFinite(v) ? v : fallback);

/** A flat EQ: no bands, no output gain, not bypassed. */
export function flatEq() {
  return { id: EQ_ID, v: EQ_VERSION, bypass: false, params: { out: 0, bands: [] } };
}

/**
 * One band, sanitised. Values are clamped into range and rounded to the
 * precision the store and the export carry (0.1 Hz, 0.01 dB, 0.001 Q), so the
 * live engine and the server compute from exactly the same numbers.
 * Returns null for something that is not a band at all.
 */
export function normalizeBand(raw) {
  if (!raw || typeof raw !== "object") return null;
  let type = typeof raw.type === "string" ? raw.type : "bell";
  type = TYPE_ALIASES[type] || type;
  if (!EQ_TYPES.includes(type)) return null;
  const f = round(clamp(num(raw.f, 1000), EQ_LIMITS.freqMin, EQ_LIMITS.freqMax), 0.1);
  const g = round(clamp(num(raw.g, 0), EQ_LIMITS.gainMin, EQ_LIMITS.gainMax), 0.01);
  const q = round(clamp(num(raw.q, EQ_DEFAULT_Q), EQ_LIMITS.qMin, EQ_LIMITS.qMax), 0.001);
  let slope = Math.round(num(raw.slope, 12));
  if (!EQ_SLOPES.includes(slope)) {
    slope = EQ_SLOPES.reduce((best, s) => (Math.abs(s - slope) < Math.abs(best - slope) ? s : best), 12);
  }
  return { on: raw.on !== false, type, f: +f.toFixed(1), g: +g.toFixed(2), q: +q.toFixed(3), slope };
}

/**
 * Any stored or incoming EQ descriptor, made safe: unknown fields dropped,
 * numbers clamped, at most EQ_LIMITS.maxBands bands. A descriptor written by a
 * newer build (v > EQ_VERSION) is not understood here and reads as flat; the
 * caller keeps the stored original so a downgrade does not wipe it.
 */
export function normalizeEq(raw) {
  const out = flatEq();
  if (!raw || typeof raw !== "object") return out;
  if (raw.id !== undefined && raw.id !== EQ_ID) return out;
  const v = raw.v === undefined ? EQ_VERSION : raw.v;
  if (v !== EQ_VERSION) return out;
  out.bypass = raw.bypass === true;
  const params = raw.params && typeof raw.params === "object" ? raw.params : {};
  out.params.out = +round(clamp(num(params.out, 0), EQ_LIMITS.outMin, EQ_LIMITS.outMax), 0.01).toFixed(2);
  const bands = Array.isArray(params.bands) ? params.bands : [];
  for (const b of bands) {
    if (out.params.bands.length >= EQ_LIMITS.maxBands) break;
    const n = normalizeBand(b);
    if (n) out.params.bands.push(n);
  }
  if (params.ui !== undefined && params.ui !== null && typeof params.ui === "object") {
    try {
      const s = JSON.stringify(params.ui);
      if (s.length <= EQ_LIMITS.maxUiChars) out.params.ui = JSON.parse(s);
    } catch { /* not serialisable: dropped */ }
  }
  return out;
}

/** True when a band changes nothing (so it is left out of the graph). */
export function bandIsNeutral(band) {
  if (!band.on) return true;
  if (band.type === "bell" || band.type === "lowshelf" || band.type === "highshelf" || band.type === "tilt") {
    return band.g === 0;
  }
  return false;
}

/** True when the whole descriptor changes nothing: bypassed or all neutral. */
export function eqIsFlat(eq) {
  if (!eq || eq.bypass) return true;
  if (eq.params.out !== 0) return false;
  return eq.params.bands.every(bandIsNeutral);
}

/**
 * Butterworth section Qs for an even order N (N/2 sections), lowest Q first.
 * Q_k = 1 / (2 cos(theta_k)), theta_k = (2k - 1) pi / (2N).
 */
export function butterworthQs(order) {
  const qs = [];
  for (let k = 1; k <= order / 2; k++) {
    qs.push(1 / (2 * Math.cos(((2 * k - 1) * Math.PI) / (2 * order))));
  }
  return qs.sort((a, b) => a - b);
}

/**
 * The biquad sections for one band: [{type, f, q, g}] where `type` is the
 * Web Audio BiquadFilterNode type and `q` is the LINEAR Q (or ignored). The
 * node's Q parameter is nodeQ(section): dB for lowpass/highpass.
 *
 * Cuts: slope / 6 = Butterworth order; the band's q scales the Q of the
 * sharpest section by q / 0.707, so q = 0.707 is plain Butterworth (maximally
 * flat) and a higher q adds a resonant peak at the corner. Tilt: a low shelf at -g/2 and a high shelf at +g/2 at
 * the same frequency, so +g tilts the spectrum up towards the treble.
 */
export function bandSections(band) {
  if (bandIsNeutral(band)) return [];
  const { type, f, g, q } = band;
  switch (type) {
    case "bell": return [{ type: "peaking", f, q, g }];
    case "lowshelf": return [{ type: "lowshelf", f, q: 0, g }];
    case "highshelf": return [{ type: "highshelf", f, q: 0, g }];
    case "notch": return [{ type: "notch", f, q, g: 0 }];
    case "bandpass": return [{ type: "bandpass", f, q, g: 0 }];
    case "tilt": return [
      { type: "lowshelf", f, q: 0, g: -g / 2 },
      { type: "highshelf", f, q: 0, g: g / 2 },
    ];
    case "lowcut":
    case "highcut": {
      const wa = type === "lowcut" ? "highpass" : "lowpass";
      const qs = butterworthQs(band.slope / 6);
      qs[qs.length - 1] *= q / EQ_DEFAULT_Q;
      return qs.map((sq) => ({ type: wa, f, q: sq, g: 0 }));
    }
    default: return [];
  }
}

/** All sections of an EQ in processing order (empty when flat or bypassed). */
export function eqSections(eq) {
  if (!eq || eq.bypass) return [];
  const out = [];
  for (const b of eq.params.bands) out.push(...bandSections(b));
  return out;
}

/** The value to put in BiquadFilterNode.Q for a section. */
export function nodeQ(section) {
  if (section.type === "lowpass" || section.type === "highpass") return 20 * Math.log10(section.q);
  if (section.type === "lowshelf" || section.type === "highshelf") return 1; // ignored by the spec
  return section.q;
}

/**
 * Normalised coefficients (a0 = 1) of one section at sample rate `fs`, by the
 * Web Audio spec formulas. Returns {b0, b1, b2, a1, a2}.
 * Mirrored by section_coefficients() in app/pipeline/fx_filters.py.
 */
export function sectionCoefs(section, fs) {
  const f0 = Math.min(Math.max(section.f, 0), fs / 2);
  const A = 10 ** (section.g / 40);
  const w0 = (2 * Math.PI * f0) / fs;
  const cw = Math.cos(w0);
  const sw = Math.sin(w0);
  const aQ = sw / (2 * section.q);
  const aQdB = sw / (2 * 10 ** (nodeQ(section) / 20));
  const aS = (sw / 2) * Math.SQRT2; // S = 1
  let b0, b1, b2, a0, a1, a2;
  switch (section.type) {
    case "lowpass":
      b0 = (1 - cw) / 2; b1 = 1 - cw; b2 = (1 - cw) / 2;
      a0 = 1 + aQdB; a1 = -2 * cw; a2 = 1 - aQdB;
      break;
    case "highpass":
      b0 = (1 + cw) / 2; b1 = -(1 + cw); b2 = (1 + cw) / 2;
      a0 = 1 + aQdB; a1 = -2 * cw; a2 = 1 - aQdB;
      break;
    case "bandpass":
      b0 = aQ; b1 = 0; b2 = -aQ;
      a0 = 1 + aQ; a1 = -2 * cw; a2 = 1 - aQ;
      break;
    case "notch":
      b0 = 1; b1 = -2 * cw; b2 = 1;
      a0 = 1 + aQ; a1 = -2 * cw; a2 = 1 - aQ;
      break;
    case "peaking":
      b0 = 1 + aQ * A; b1 = -2 * cw; b2 = 1 - aQ * A;
      a0 = 1 + aQ / A; a1 = -2 * cw; a2 = 1 - aQ / A;
      break;
    case "lowshelf": {
      const k = 2 * aS * Math.sqrt(A);
      b0 = A * ((A + 1) - (A - 1) * cw + k);
      b1 = 2 * A * ((A - 1) - (A + 1) * cw);
      b2 = A * ((A + 1) - (A - 1) * cw - k);
      a0 = (A + 1) + (A - 1) * cw + k;
      a1 = -2 * ((A - 1) + (A + 1) * cw);
      a2 = (A + 1) + (A - 1) * cw - k;
      break;
    }
    case "highshelf": {
      const k = 2 * aS * Math.sqrt(A);
      b0 = A * ((A + 1) + (A - 1) * cw + k);
      b1 = -2 * A * ((A - 1) + (A + 1) * cw);
      b2 = A * ((A + 1) + (A - 1) * cw - k);
      a0 = (A + 1) - (A - 1) * cw + k;
      a1 = 2 * ((A - 1) - (A + 1) * cw);
      a2 = (A + 1) - (A - 1) * cw - k;
      break;
    }
    default:
      return { b0: 1, b1: 0, b2: 0, a1: 0, a2: 0 };
  }
  return { b0: b0 / a0, b1: b1 / a0, b2: b2 / a0, a1: a1 / a0, a2: a2 / a0 };
}

/** |H| in dB of one set of normalised coefficients at frequency f (Hz). */
export function coefsMagDb(c, f, fs) {
  const w = (2 * Math.PI * f) / fs;
  const c1 = Math.cos(w), s1 = Math.sin(w), c2 = Math.cos(2 * w), s2 = Math.sin(2 * w);
  const nr = c.b0 + c.b1 * c1 + c.b2 * c2;
  const ni = -(c.b1 * s1 + c.b2 * s2);
  const dr = 1 + c.a1 * c1 + c.a2 * c2;
  const di = -(c.a1 * s1 + c.a2 * s2);
  return 10 * Math.log10((nr * nr + ni * ni) / (dr * dr + di * di));
}

/**
 * Magnitude response of a whole EQ in dB at each frequency, output gain
 * included, at sample rate `fs` (the live context's rate, or the stems' 44.1
 * kHz when nothing is playing). Flat or bypassed: all zeros.
 */
export function eqResponseDb(eq, freqs, fs = 44100) {
  const out = new Float64Array(freqs.length);
  if (!eq || eq.bypass) return out;
  const coefs = eqSections(eq).map((s) => sectionCoefs(s, fs));
  for (let i = 0; i < freqs.length; i++) {
    let db = eq.params.out;
    for (const c of coefs) db += coefsMagDb(c, freqs[i], fs);
    out[i] = db;
  }
  return out;
}

/** Response of one band alone (for drawing a band's own curve). */
export function bandResponseDb(band, freqs, fs = 44100) {
  const out = new Float64Array(freqs.length);
  const coefs = bandSections(band).map((s) => sectionCoefs(s, fs));
  for (let i = 0; i < freqs.length; i++) {
    let db = 0;
    for (const c of coefs) db += coefsMagDb(c, freqs[i], fs);
    out[i] = db;
  }
  return out;
}

/**
 * The export wire form of one lane's EQ, or null when it changes nothing:
 * {id: "eq", out, b: [[type, f, g, q, slope], ...]} with only the bands that
 * reach the graph. app/pipeline/fx_filters.py validates exactly this shape.
 */
export function eqToWire(eq) {
  if (eqIsFlat(eq)) return null;
  const b = eq.params.bands.filter((band) => !bandIsNeutral(band))
    .map((band) => [band.type, band.f, band.g, band.q, band.slope]);
  return { id: EQ_ID, out: eq.params.out, b };
}

/** base64url without padding, of a UTF-8 string. */
export function base64UrlEncode(text) {
  const bytes = new TextEncoder().encode(text);
  let bin = "";
  for (const byte of bytes) bin += String.fromCharCode(byte);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/**
 * The `fx=` export parameter for a set of lanes: base64url(JSON
 * {v: 1, lanes: {name: [effect]}, master: [effect]}), or "" when nothing in it
 * changes the sound (so an export with no EQ keeps its old URL).
 * `lanes` maps lane name to an EQ descriptor; `master` is the master EQ.
 */
export function encodeFxParam(lanes, master = null) {
  const outLanes = {};
  let any = false;
  for (const [name, eq] of Object.entries(lanes || {})) {
    const wire = eqToWire(eq);
    if (wire) { outLanes[name] = [wire]; any = true; }
  }
  const body = { v: 1, lanes: outLanes };
  const mw = master ? eqToWire(master) : null;
  if (mw) { body.master = [mw]; any = true; }
  return any ? base64UrlEncode(JSON.stringify(body)) : "";
}
