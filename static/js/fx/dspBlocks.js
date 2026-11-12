// Shared Web Audio building blocks of the pedals (native nodes only, so the
// chain works over plain HTTP on a LAN, where AudioWorklet does not exist).
// Every block works in any BaseAudioContext, live or offline: the export
// print renders with the same code (fxPrint.js).

/**
 * One cycle of a waveform at 1 Hz, as a looping buffer: an LFO whose phase is
 * set by its start offset. An OscillatorNode has no phase control, and the
 * phase has to follow song time so the export print modulates at the same
 * place as playback (report 3.7). Cached per context.
 */
const lfoBuffers = new WeakMap();
function lfoBuffer(ctx, shape) {
  let byShape = lfoBuffers.get(ctx);
  if (!byShape) { byShape = new Map(); lfoBuffers.set(ctx, byShape); }
  let buf = byShape.get(shape);
  if (buf) return buf;
  if (shape === "drift") {
    buf = driftBuffer(ctx);
    byShape.set(shape, buf);
    return buf;
  }
  const n = Math.max(64, Math.round(ctx.sampleRate));
  buf = ctx.createBuffer(1, n, ctx.sampleRate);
  const d = buf.getChannelData(0);
  // Both shapes start at 0 going up, so they share one phase rule.
  for (let i = 0; i < n; i++) {
    const ph = i / n;
    d[i] = shape === "triangle" ? 1 - 4 * Math.abs(((ph + 0.25) % 1) - 0.5) : Math.sin(2 * Math.PI * ph);
  }
  byShape.set(shape, buf);
  return buf;
}

/**
 * Smoothed random drift for a voice doubler (report 4.3: a slow random
 * wander sounds like a second take, a periodic wobble does not). 16 s at
 * rate 1: a new random point every 0.5 s, joined by cosine interpolation,
 * from a fixed seed, so live playback and the export print drift the same
 * way at the same song time. Looping is seamless (the last point is the
 * first). Values in [-1, 1].
 */
export const DRIFT_SECONDS = 16;
function driftBuffer(ctx) {
  const points = DRIFT_SECONDS * 2;
  let s = 0x2545f491;
  const rand = () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
    return (s / 2 ** 32) * 2 - 1;
  };
  const p = Array.from({ length: points }, rand);
  const n = Math.round(ctx.sampleRate * DRIFT_SECONDS);
  const buf = ctx.createBuffer(1, n, ctx.sampleRate);
  const d = buf.getChannelData(0);
  const seg = n / points;
  for (let i = 0; i < n; i++) {
    const k = Math.floor(i / seg);
    const f = (i - k * seg) / seg;
    const a = p[k % points];
    const b = p[(k + 1) % points];
    d[i] = a + (b - a) * (1 - Math.cos(Math.PI * f)) / 2;
  }
  return buf;
}

/**
 * An LFO from -1 to 1 at `rate` Hz, in phase with song time: at song time
 * `songTime` (seconds, at the moment the LFO starts) its phase is
 * frac(songTime * rate), so the same song time gives the same phase in any
 * context. Returns {output (GainNode, the depth), stop()}.
 */
export function createLfo(ctx, { rate, depth = 1, songTime = 0, shape = "sine", when = ctx.currentTime }) {
  const src = ctx.createBufferSource();
  src.buffer = lfoBuffer(ctx, shape);
  src.loop = true;
  src.playbackRate.value = rate;
  const out = ctx.createGain();
  out.gain.value = depth;
  src.connect(out);
  // The buffer plays `rate` of its seconds per second (one cycle per second
  // for sine and triangle), so song time t is buffer time (t x rate) mod its
  // length.
  const dur = src.buffer.duration;
  const pos = songTime * rate;
  src.start(when, pos - Math.floor(pos / dur) * dur);
  return {
    output: out,
    stop() {
      try { src.stop(); } catch { /* not started or already stopped */ }
      try { src.disconnect(); } catch { /* gone */ }
      try { out.disconnect(); } catch { /* gone */ }
    },
  };
}

/**
 * The Q of a Butterworth lowpass or highpass BiquadFilterNode. Web Audio
 * reads Q in dB for these two types (spec: "the Q ... in dB"), so the
 * Butterworth 1/sqrt(2) is 20 log10(1/sqrt(2)) = -3.0103 dB; a Q of 0.7071
 * there is a resonant filter (+0.7 dB, measured by
 * tests/e2e/fx-chain-dsp.spec.mjs through the crossover).
 */
export const BUTTERWORTH_Q_DB = 20 * Math.log10(Math.SQRT1_2);

/**
 * A Linkwitz-Riley 4th-order crossover at `freq`: two Butterworth biquads
 * (Q 0.7071) per side. The low and high outputs sum to a flat magnitude (an
 * allpass), which is how the bass voicings keep the fundamental dry while the
 * part above it gets the effect (report 3.8). Returns {input, low, high,
 * setFreq(f, immediate), nodes}.
 */
export function createCrossover(ctx, freq, rampTo) {
  const input = ctx.createGain();
  const mk = (type) => {
    const b = ctx.createBiquadFilter();
    b.type = type;
    b.frequency.value = freq;
    b.Q.value = BUTTERWORTH_Q_DB;
    return b;
  };
  const l1 = mk("lowpass");
  const l2 = mk("lowpass");
  const h1 = mk("highpass");
  const h2 = mk("highpass");
  input.connect(l1);
  l1.connect(l2);
  input.connect(h1);
  h1.connect(h2);
  return {
    input,
    low: l2,
    high: h2,
    setFreq(f, immediate) { for (const b of [l1, l2, h1, h2]) rampTo(b.frequency, f, b.context, immediate); },
    nodes: [input, l1, l2, h1, h2],
  };
}

const curves = new Map();
/**
 * A WaveShaper curve over u in [-1, 1] that applies f(x) to x = 2u, so a
 * GainNode of 0.5 in front gives y = f(x) for |x| <= 2 (a WaveShaperNode
 * clamps its input to [-1, 1]). An odd number of points, so 0 maps to 0.
 *   "tanh":  soft saturation, slope 1 at 0 (analog and tape repeats).
 *   "knee":  exactly linear up to 0.8, then a soft knee to 1: digital repeats
 *            stay clean, and the loop is still bounded at 98 % feedback.
 */
export function loopCurve(kind) {
  let c = curves.get(kind);
  if (c) return c;
  const n = 4097;
  c = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const x = 2 * ((2 * i) / (n - 1) - 1);
    if (kind === "tanh") c[i] = Math.tanh(x);
    else {
      const a = Math.abs(x);
      c[i] = Math.sign(x) * (a <= 0.8 ? a : 0.8 + 0.2 * Math.tanh((a - 0.8) / 0.2));
    }
  }
  c[(n - 1) / 2] = 0;
  curves.set(kind, c);
  return c;
}

/** |x|: the rectifier of the envelope follower. */
let absCurveCache = null;
function absCurve() {
  if (absCurveCache) return absCurveCache;
  const n = 4097;
  absCurveCache = new Float32Array(n);
  for (let i = 0; i < n; i++) absCurveCache[i] = Math.abs((2 * i) / (n - 1) - 1);
  return absCurveCache;
}

/**
 * Envelope to duck amount, 0..1: 0 below FLOOR, 1 from REF up, linear in
 * between. FLOOR and REF are mean |x| levels (proposal: about -46 dBFS and
 * -24 dBFS, a quiet breath and a sung phrase on a separated vocal).
 */
const DUCK_FLOOR = 0.005;
const DUCK_REF = 0.06;
let duckCurveCache = null;
function duckCurve() {
  if (duckCurveCache) return duckCurveCache;
  const n = 2049;
  duckCurveCache = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const e = Math.abs((2 * i) / (n - 1) - 1);
    duckCurveCache[i] = Math.max(0, Math.min(1, (e - DUCK_FLOOR) / (DUCK_REF - DUCK_FLOOR)));
  }
  return duckCurveCache;
}

/**
 * A native envelope follower (report 4, building blocks): |x|, two low-pass
 * biquads, then the duck mapping. Its output is 0..1 and drives an
 * AudioParam through a GainNode. One low-pass gives equal attack and
 * release; separate times need a worklet. Cutoff 4 Hz (about 40 ms,
 * proposal). Returns {input, output, dispose()}.
 */
export function createDuckFollower(ctx) {
  const input = ctx.createGain();
  input.channelCount = 1;
  input.channelCountMode = "explicit";
  input.channelInterpretation = "speakers";
  const rect = ctx.createWaveShaper();
  rect.curve = absCurve();
  const lp1 = ctx.createBiquadFilter();
  const lp2 = ctx.createBiquadFilter();
  for (const lp of [lp1, lp2]) { lp.type = "lowpass"; lp.frequency.value = 4; lp.Q.value = BUTTERWORTH_Q_DB; }
  const map = ctx.createWaveShaper();
  map.curve = duckCurve();
  input.connect(rect);
  rect.connect(lp1);
  lp1.connect(lp2);
  lp2.connect(map);
  return {
    input,
    output: map,
    dispose() { for (const n of [input, rect, lp1, lp2, map]) { try { n.disconnect(); } catch { /* gone */ } } },
  };
}

/**
 * The latency of a WaveShaperNode at oversample "4x", in frames, measured in
 * this browser at this rate (Chromium 151: 192 frames at 48 kHz; "none" adds
 * 0, "2x" 128). A distortion pedal delays its clean paths by this so its
 * Blend does not comb-filter, and the lane is aligned by it (report D8).
 */
const shaperLatency = new Map();
export function shaperLatencyFrames(sampleRate) {
  if (shaperLatency.has(sampleRate)) return shaperLatency.get(sampleRate);
  const OAC = globalThis.OfflineAudioContext || globalThis.webkitOfflineAudioContext;
  const p = !OAC ? Promise.resolve(0) : (async () => {
    const ctx = new OAC(1, 2048, sampleRate);
    const buf = ctx.createBuffer(1, 2048, sampleRate);
    buf.getChannelData(0)[100] = 0.5;
    const src = ctx.createBufferSource();
    src.buffer = buf;
    const ws = ctx.createWaveShaper();
    const c = new Float32Array(4097);
    for (let i = 0; i < c.length; i++) c[i] = i / 2048 - 1;
    ws.curve = c;
    ws.oversample = "4x";
    src.connect(ws);
    ws.connect(ctx.destination);
    src.start();
    const d = (await ctx.startRendering()).getChannelData(0);
    let at = 0;
    for (let i = 0; i < d.length; i++) if (Math.abs(d[i]) > Math.abs(d[at])) at = i;
    return Math.max(0, at - 100);
  })().catch((err) => {
    console.warn("[fx] measuring the waveshaper latency failed:", err);
    return 0;
  });
  shaperLatency.set(sampleRate, p);
  return p;
}

/**
 * How many frames a feedback cycle adds beyond its DelayNode's delayTime, in
 * this browser at this rate. Measured, not assumed: in Chromium 151 each
 * pass round a cycle took delayTime + 128 frames (an impulse through a
 * 4800-frame loop came back at 4800, 9728, 14656), while the spec clamps a
 * cycle's delay to one render quantum and adds nothing. A delay pedal sets
 * its loop delay this much short, so the repeats land exactly on the beat.
 *
 * `delays`: the number of delay lines in the measured cycle; the answer is
 * the average per line. Every pedal loop holds one line (with two, Chromium
 * put the whole overhead on one of them, so an average would be wrong).
 * Cached per rate and kind.
 */
const overhead = new Map();
export function loopOverheadFrames(sampleRate, delays = 1) {
  const key = `${sampleRate}:${delays}`;
  if (overhead.has(key)) return overhead.get(key);
  const OAC = globalThis.OfflineAudioContext || globalThis.webkitOfflineAudioContext;
  if (!OAC) {
    const p = Promise.resolve(0);
    overhead.set(key, p);
    return p;
  }
  const p = (async () => {
    const lineFrames = 2048;
    const frames = lineFrames * (delays * 2 + 2) + 4096;
    const ctx = new OAC(1, frames, sampleRate);
    const buf = ctx.createBuffer(1, 128, sampleRate);
    buf.getChannelData(0)[0] = 1;
    const src = ctx.createBufferSource();
    src.buffer = buf;
    const sum = ctx.createGain();
    const lines = [];
    for (let i = 0; i < delays; i++) {
      const d = ctx.createDelay(1);
      // As every delay line of the pedals (delayPedal.js says why).
      d.channelCount = 1;
      d.channelCountMode = "explicit";
      d.delayTime.value = lineFrames / sampleRate;
      lines.push(d);
    }
    // sum -> line0 -> line1 ... -> back to sum at gain 0.5; read after line0.
    const back = ctx.createGain();
    back.gain.value = 0.5;
    src.connect(sum);
    let prev = sum;
    for (const d of lines) { prev.connect(d); prev = d; }
    prev.connect(back);
    back.connect(sum);
    lines[0].connect(ctx.destination);
    src.start();
    const out = (await ctx.startRendering()).getChannelData(0);
    const hits = [];
    let last = -1e9;
    for (let i = 0; i < out.length && hits.length < 2; i++) {
      if (Math.abs(out[i]) > 0.1 && i - last > 64) { hits.push(i); last = i; }
    }
    if (hits.length < 2) return 0;
    const extra = (hits[1] - hits[0] - lineFrames * delays) / delays;
    return Math.max(0, Math.min(1024, Math.round(extra)));
  })().catch((err) => {
    console.warn("[fx] measuring the feedback loop overhead failed:", err);
    return 0;
  });
  overhead.set(key, p);
  return p;
}
