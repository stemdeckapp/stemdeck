// Generated reverb impulse responses: no IR files, so no IR licence, and the
// same IR in live playback and in the export print. Pure maths, no Web Audio:
// tests/js/fx-chain.test.mjs runs it in node.
//
// Method: exponentially decaying noise (the Moorer/Jot synthetic reverb tail,
// a standard way to make an IR with an exact decay time), shaped per type:
//   room   discrete early reflections in the first 40 ms over a fast build;
//   hall   a slow build (about 60 ms) and no strong early taps;
//   plate  an immediate, dense build: no distinct echoes (report 4.5: it
//          does not smear consonants);
//   spring a decaying tail plus the drip: an impulse train at the tank's
//          33, 37 and 41 ms round trips (reported) through a dispersive
//          allpass chain, the chirp of a spring (Valimaki, Parker, Abel 2010,
//          reported), scaled by Drip.
// The energy decays by 60 dB in `decay` seconds (RT60), measured by
// tests/e2e/fx-chain-dsp.spec.mjs. Seeded noise, independent per side
// (stereo width). Not done: the report's Freeverb or Dattorro topologies run
// as an IR generator; this statistical tail is a different, simpler method
// with the same decay law.

const cache = new Map();

function lcg(seed) {
  let s = seed >>> 0;
  return () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
    return (s / 2 ** 32) * 2 - 1;
  };
}

/** The IR length for a decay: the decay plus 10 %, at most 10 s. */
export function irSeconds(decay) {
  return Math.min(10, decay * 1.1 + 0.05);
}

/**
 * Two channels of IR as Float32Arrays, energy-normalised (sum of squares 1
 * per channel, so a broadband signal keeps its level in the wet path).
 * Cached by every input.
 */
export function generateIr({ type, decay, drip = 0, rate }) {
  const key = `${type}|${decay.toFixed(3)}|${drip.toFixed(3)}|${rate}`;
  const hit = cache.get(key);
  if (hit) return hit;
  const n = Math.max(1, Math.round(irSeconds(decay) * rate));
  const k = Math.log(1000) / (decay * rate); // amplitude e^(-k i): -60 dB at decay
  const out = [];
  for (let ch = 0; ch < 2; ch++) {
    const rand = lcg(0x9e3779b9 ^ (ch * 0x85ebca6b) ^ type.length * 7919);
    const d = new Float32Array(n);
    const build = type === "hall" ? 0.06 : type === "room" ? 0.008 : type === "spring" ? 0.004 : 0.001;
    const b = build * rate;
    for (let i = 0; i < n; i++) {
      const env = Math.exp(-k * i) * (b > 0 ? 1 - Math.exp(-i / b) : 1);
      d[i] = rand() * env;
    }
    if (type === "room") {
      // Early reflections: 12 taps between 3 and 40 ms, seeded per side.
      for (let r = 0; r < 12; r++) {
        const at = Math.round((0.003 + ((rand() + 1) / 2) * 0.037) * rate);
        if (at < n) d[at] += rand() * 6 * Math.exp(-k * at);
      }
    }
    if (type === "spring" && drip > 0) {
      // The drip: a dispersive chirp repeating at the tank's round trips.
      const train = new Float32Array(n);
      for (const ms of [33, 37, 41]) {
        const step = Math.round((ms / 1000) * rate);
        for (let i = step; i < n; i += step) train[i] += Math.exp(-k * i) * (ch ? 0.9 : 1);
      }
      // 30 first-order allpass stages, a = 0.6: low frequencies arrive late.
      let x = train;
      for (let s = 0; s < 30; s++) {
        const y = new Float32Array(n);
        let x1 = 0, y1 = 0;
        for (let i = 0; i < n; i++) { const v = -0.6 * x[i] + x1 + 0.6 * y1; x1 = x[i]; y1 = v; y[i] = v; }
        x = y;
      }
      let ex = 0, en = 0;
      for (let i = 0; i < n; i++) { ex += x[i] * x[i]; en += d[i] * d[i]; }
      const g = ex > 0 ? Math.sqrt(en / ex) * drip * 1.5 : 0;
      for (let i = 0; i < n; i++) d[i] += x[i] * g;
    }
    let e = 0;
    for (let i = 0; i < n; i++) e += d[i] * d[i];
    const norm = e > 0 ? 1 / Math.sqrt(e) : 0;
    for (let i = 0; i < n; i++) d[i] *= norm;
    out.push(d);
  }
  if (cache.size > 24) cache.delete(cache.keys().next().value);
  cache.set(key, out);
  return out;
}
