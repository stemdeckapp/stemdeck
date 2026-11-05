// The graphical EQ editor, the pure part: axis mapping, hit testing, drag
// and wheel maths, band defaults, number formats and parsing, undo history,
// and the spectrum's bin-to-pixel map. No DOM, so tests/js/eq-panels.test.mjs
// runs it in node. eqGraphPanel.js draws and handles input with it.

import { EQ_LIMITS, EQ_DEFAULT_Q, normalizeBand } from "../eqMath.js";

export const F_MIN = 20;
export const F_MAX = 20000;
/** The graph shows +-DB_RANGE dB; a drag keeps a band's gain inside it. */
export const DB_RANGE = 18;
/**
 * The spectrum's floor and top, in dB per FFT bin. An 8192-point FFT spreads
 * a stem's energy thinly: measured on a real song, a drum stem at -36 dBFS
 * RMS peaks around -64 dB per bin and its body sits near -80.
 */
export const SPEC_MIN_DB = -115;
export const SPEC_MAX_DB = -35;
/**
 * The spectrum is drawn tilted up by this many dB per octave around 1 kHz,
 * as analysers in mixing EQs commonly do: music falls off towards the top,
 * and without the tilt everything above a few kHz sits on the floor.
 */
export const SPEC_TILT_DB_OCT = 4.5;

const LOG_SPAN = Math.log(F_MAX / F_MIN);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

const GAIN_TYPES = new Set(["bell", "lowshelf", "highshelf", "tilt"]);
const Q_TYPES = new Set(["bell", "notch", "bandpass", "lowcut", "highcut"]);
const CUT_TYPES = new Set(["lowcut", "highcut"]);

export const hasGain = (type) => GAIN_TYPES.has(type);
export const hasQ = (type) => Q_TYPES.has(type);
export const hasSlope = (type) => CUT_TYPES.has(type);

/**
 * The plot area inside a canvas of w x h CSS px: room on the right for the
 * dB labels and at the bottom for the frequency labels.
 */
export function plotRect(w, h) {
  return { x: 0, y: 6, w: Math.max(1, w - 28), h: Math.max(1, h - 22) };
}

export function freqToX(f, r) {
  return r.x + (r.w * Math.log(clamp(f, F_MIN, F_MAX) / F_MIN)) / LOG_SPAN;
}

export function xToFreq(x, r) {
  return clamp(F_MIN * Math.exp(((x - r.x) / r.w) * LOG_SPAN), F_MIN, F_MAX);
}

export function dbToY(db, r, range = DB_RANGE) {
  return r.y + r.h / 2 - (db / range) * (r.h / 2);
}

export function yToDb(y, r, range = DB_RANGE) {
  return ((r.y + r.h / 2 - y) / (r.h / 2)) * range;
}

/** Where a band's handle sits: at its gain, or on the 0 dB line when it has none. */
export function nodeXY(band, r, range = DB_RANGE) {
  const g = hasGain(band.type) ? clamp(band.g, -range, range) : 0;
  return { x: freqToX(band.f, r), y: dbToY(g, r, range) };
}

/**
 * The band under a point, or -1. The nearest handle within `radius` px wins;
 * on a tie the later band (drawn on top) wins.
 */
export function hitTest(bands, px, py, r, range = DB_RANGE, radius = 10) {
  let best = -1;
  let bestD = radius * radius;
  for (let i = 0; i < bands.length; i++) {
    const { x, y } = nodeXY(bands[i], r, range);
    const d = (x - px) ** 2 + (y - py) ** 2;
    if (d <= bestD) { best = i; bestD = d; }
  }
  return best;
}

/**
 * A band dragged by (dx, dy) px from where it was when the drag started.
 * Frequency follows x on the log axis; gain follows y, kept inside the
 * graph's range. `fine` (Shift) moves a fifth as far. Bands without a gain
 * only move in frequency.
 */
export function dragBand(start, dx, dy, r, { range = DB_RANGE, fine = false } = {}) {
  const k = fine ? 0.2 : 1;
  const x = freqToX(start.f, r) + dx * k;
  const out = { ...start, f: round1(xToFreq(x, r)) };
  if (hasGain(start.type)) {
    const y = dbToY(start.g, r, range) + dy * k;
    out.g = round2(clamp(yToDb(y, r, range), -range, range));
  }
  return out;
}

/**
 * Q after a wheel turn (or a two-finger pinch, which browsers send as a wheel
 * with ctrlKey). Up narrows, multiplicatively, so a notch and a wide bell
 * take the same number of notches to halve. `fine` is a quarter as fast.
 */
export function wheelQ(q, deltaY, fine = false) {
  const per = fine ? 2000 : 500;
  return round3(clamp(q * 2 ** (-deltaY / per), EQ_LIMITS.qMin, EQ_LIMITS.qMax));
}

/**
 * The band a click on empty space adds, by where it lands: a low cut at the
 * very bottom of the range, a low shelf in the lows, a high shelf in the
 * highs, a high cut at the very top, a bell anywhere else.
 */
export function defaultBandAt(f, g) {
  let type = "bell";
  if (f < 30) type = "lowcut";
  else if (f < 90) type = "lowshelf";
  else if (f > 15000) type = "highcut";
  else if (f > 9000) type = "highshelf";
  return normalizeBand({
    on: true,
    type,
    f,
    g: hasGain(type) ? g : 0,
    q: type === "bell" ? 1 : EQ_DEFAULT_Q,
    slope: hasSlope(type) ? 24 : 12,
  });
}

/** A band put back to its neutral starting point (double-click). */
export function resetBand(band) {
  return normalizeBand({
    ...band,
    g: 0,
    q: band.type === "bell" ? 1 : EQ_DEFAULT_Q,
  });
}

/** A band with another type: the frequency stays, the rest takes that type's defaults. */
export function retypeBand(band, type) {
  return normalizeBand({
    ...band,
    type,
    g: hasGain(type) ? band.g : 0,
    q: hasQ(band.type) && hasQ(type) && !hasSlope(type) && !hasSlope(band.type) ? band.q : (type === "bell" ? 1 : EQ_DEFAULT_Q),
    slope: hasSlope(type) ? (hasSlope(band.type) ? band.slope : 24) : 12,
  });
}

/**
 * What soloing a band plays: a band pass at the band's frequency, as wide as
 * the band (a cut or a shelf, which has no width of its own, an octave and a
 * bit), so the ear hears the region the band works on.
 */
export function soloEq(band) {
  const q = band.type === "bell" || band.type === "notch" || band.type === "bandpass"
    ? clamp(band.q, 0.4, 12)
    : 0.9;
  return { id: "eq", v: 1, bypass: false, params: { out: 0, bands: [{ on: true, type: "bandpass", f: band.f, g: 0, q, slope: 12 }] } };
}

// ─── Keyboard steps ───

/** One arrow press on a focused handle. `fine`: Shift. Returns the new band. */
export function keyStep(band, key, fine = false) {
  const b = { ...band };
  const oct = fine ? 1 / 48 : 1 / 12;
  switch (key) {
    case "ArrowLeft": b.f = band.f / 2 ** oct; break;
    case "ArrowRight": b.f = band.f * 2 ** oct; break;
    case "ArrowUp": if (hasGain(band.type)) b.g = band.g + (fine ? 0.1 : 0.5); break;
    case "ArrowDown": if (hasGain(band.type)) b.g = band.g - (fine ? 0.1 : 0.5); break;
    case "PageUp": b.q = band.q * 2 ** (fine ? 1 / 12 : 1 / 4); break;
    case "PageDown": b.q = band.q / 2 ** (fine ? 1 / 12 : 1 / 4); break;
    default: return band;
  }
  b.f = clamp(b.f, EQ_LIMITS.freqMin, EQ_LIMITS.freqMax);
  return normalizeBand(b);
}

// ─── Numbers ───

function round1(v) { return Math.round(v * 10) / 10; }
function round2(v) { return Math.round(v * 100) / 100; }
function round3(v) { return Math.round(v * 1000) / 1000; }

/** "62.5 Hz", "440 Hz", "1.25 kHz", "12.5 kHz". */
export function fmtFreq(f) {
  if (!Number.isFinite(f)) return "";
  if (f >= 1000) return `${trimZeros((f / 1000).toFixed(f >= 10000 ? 1 : 2))} kHz`;
  return `${trimZeros(f.toFixed(f < 100 ? 1 : 0))} Hz`;
}

/** "+3.5 dB", "-12.0 dB", "0.0 dB". */
export function fmtDb(g) {
  if (!Number.isFinite(g)) return "";
  const v = Math.abs(g) < 0.05 ? 0 : g;
  return `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(1)} dB`;
}

/** "0.71", "4.0", "12". */
export function fmtQ(q) {
  if (!Number.isFinite(q)) return "";
  return q >= 10 ? q.toFixed(0) : q >= 1 ? q.toFixed(1) : q.toFixed(2);
}

function trimZeros(s) {
  return s.includes(".") ? s.replace(/0+$/, "").replace(/\.$/, "") : s;
}

/** A typed number: "1,5" and "1.5" both work; a minus sign may be typographic. */
export function parseNumber(text) {
  if (typeof text !== "string") return NaN;
  const s = text.trim().replace(/−/g, "-").replace(",", ".").replace(/[^0-9.+-]/g, "");
  if (!s || !/^[+-]?(\d+\.?\d*|\.\d+)$/.test(s)) return NaN;
  return Number(s);
}

/** A typed frequency: "1200", "1.2k", "1,2 kHz", "80 Hz". NaN when it is none. */
export function parseFreq(text) {
  if (typeof text !== "string") return NaN;
  const kilo = /k/i.test(text);
  const n = parseNumber(text.replace(/hz/i, "").replace(/k/i, ""));
  if (!Number.isFinite(n)) return NaN;
  return kilo ? n * 1000 : n;
}

// ─── Undo ───

/**
 * Undo history of whole EQ descriptors. push() records the state BEFORE a
 * change; undo(current) returns the state to go back to (and remembers
 * `current` for redo). Equal neighbours are not stored twice.
 */
export function createHistory(limit = 100) {
  let past = [];
  let future = [];
  const key = (d) => JSON.stringify(d);
  return {
    push(state) {
      const k = key(state);
      if (past.length && key(past[past.length - 1]) === k) return;
      past.push(JSON.parse(k));
      if (past.length > limit) past = past.slice(past.length - limit);
      future = [];
    },
    undo(current) {
      if (!past.length) return null;
      future.push(JSON.parse(key(current)));
      return past.pop();
    },
    redo(current) {
      if (!future.length) return null;
      past.push(JSON.parse(key(current)));
      return future.pop();
    },
    get canUndo() { return past.length > 0; },
    get canRedo() { return future.length > 0; },
    clear() { past = []; future = []; },
  };
}

// ─── Spectrum ───

/**
 * For each pixel column of the plot, the FFT bins it covers: lo[c]..hi[c]
 * (inclusive), and the display tilt at its frequency (`tiltDbOct` dB per
 * octave around 1 kHz). Columns narrower than a bin (the lows) get the one
 * bin under them. Built once per size; drawing a frame then allocates nothing.
 */
export function columnBins(columns, binCount, sampleRate, r, tiltDbOct = 0) {
  const lo = new Int32Array(columns);
  const hi = new Int32Array(columns);
  const tilt = new Float32Array(columns);
  const binHz = sampleRate / 2 / binCount;
  for (let c = 0; c < columns; c++) {
    const f0 = xToFreq(r.x + (c / columns) * r.w, r);
    const f1 = xToFreq(r.x + ((c + 1) / columns) * r.w, r);
    let a = Math.round(f0 / binHz);
    let b = Math.round(f1 / binHz) - 1;
    a = clamp(a, 1, binCount - 1);
    b = clamp(b, a, binCount - 1);
    lo[c] = a;
    hi[c] = b;
    tilt[c] = tiltDbOct * Math.log2(Math.sqrt(f0 * f1) / 1000);
  }
  return { lo, hi, tilt };
}

/**
 * One frame of the spectrum into `out` (dB per column), with peak smoothing:
 * a column jumps up at once and falls back at most `fall` dB per frame.
 */
export function spectrumFrame(bins, data, out, fall) {
  const { lo, hi, tilt } = bins;
  for (let c = 0; c < out.length; c++) {
    let m = -Infinity;
    for (let i = lo[c]; i <= hi[c]; i++) if (data[i] > m) m = data[i];
    m += tilt[c];
    if (!(m > SPEC_MIN_DB)) m = SPEC_MIN_DB;
    const prev = out[c];
    out[c] = m >= prev ? m : Math.max(m, prev - fall);
  }
  return out;
}

/** Log-spaced frequencies across the graph, one per `step` px (for the curve). */
export function curveFreqs(r, step = 2) {
  const n = Math.max(2, Math.ceil(r.w / step) + 1);
  const out = new Float64Array(n);
  for (let i = 0; i < n; i++) out[i] = xToFreq(r.x + (i / (n - 1)) * r.w, r);
  return out;
}

/** Grid lines: frequencies with a label or a plain line, and dB steps. */
export const GRID_FREQS = [
  [20, "20"], [30], [40], [50, "50"], [60], [70], [80], [90],
  [100, "100"], [200, "200"], [300], [400], [500, "500"], [600], [700], [800], [900],
  [1000, "1k"], [2000, "2k"], [3000], [4000], [5000, "5k"], [6000], [7000], [8000], [9000],
  [10000, "10k"], [20000, "20k"],
];

export function gridDbs(range = DB_RANGE) {
  const step = range > 12 ? 6 : 3;
  const out = [];
  for (let d = -range; d <= range + 1e-9; d += step) out.push(d);
  return out;
}
