// The Distortion pedal (overdrive, distortion, fuzz, saturation): native
// nodes, any BaseAudioContext.
//
//   input ─┬─ bypass ─ align ───────────────────────────────────────────────┬─ output
//          └─ feed ─┬─ [crossover low] ─ align ──────── (lows clean) ──────┤
//                   └─ hi ─┬─ clean (blend dry) ─ align ───────────────────┤
//                          └─ low cut ─ mid ─ drive ─ 1/K ─ shaper (4x) ─ DC block
//                             ─ high cut ─ [cabinet] ─ [bass low] ─ level ─ wet (blend)
//
// - The shaper runs at oversample "4x". Its resampling filters delay the
//   wet path (192 frames in Chromium 151, measured at run time:
//   dspBlocks.shaperLatencyFrames), so every clean path ("align") is delayed
//   by the same amount, or Blend would comb-filter. The bypass is delayed
//   too, so the pedal's latency is the same on and off and the lane
//   alignment (report D8) never jumps.
// - Drive is a smooth gain in front of the shaper; the curve covers +/-K
//   (K = 64), so only a change of clip family needs a new node graph.
// - Curves (proposal, to be fitted by ear; a memoryless waveshaper is not
//   faithful to any circuit, report 4.1): soft symmetric (tanh, odd
//   harmonics), soft asymmetric (one side clips earlier: even harmonics
//   too), hard (clamp at +/-1, diodes to ground), fuzz (strongly asymmetric).
// - The DC blocker is the output coupling capacitor: asymmetric clipping
//   makes DC, which must never reach a later feedback loop.
// - Cabinet (guitar): a generated speaker response from biquads (a cabinet
//   is a linear filter; its values are a proposal): 1x12 open, 2x12, 4x12.
// - Bass: the crossover keeps the fundamental clean (M84, B7K, SansAmp
//   blend), and a low shelf at 100 Hz on the drive (B7K Bass).

import { rampTo, XFADE } from "./ramp.js";
import { createCrossover, BUTTERWORTH_Q_DB } from "./dspBlocks.js";
import { pedalMix } from "./chainModel.js";

const K = 64;
const POINTS = 16385;
const curves = new Map();

/** The transfer of a clip family, x in drive units. */
export function clipFn(kind) {
  if (kind === "hard") return (x) => Math.max(-1, Math.min(1, x));
  if (kind === "softasym") return (x) => (x >= 0 ? Math.tanh(x) : 0.6 * Math.tanh(x / 0.6));
  if (kind === "fuzz") return (x) => (x >= 0 ? 1 - Math.exp(-2 * x) : -0.7 * (1 - Math.exp(1.5 * x / 0.7)));
  return (x) => Math.tanh(x);
}

function curveFor(kind) {
  let c = curves.get(kind);
  if (c) return c;
  const f = clipFn(kind);
  c = new Float32Array(POINTS);
  for (let i = 0; i < POINTS; i++) c[i] = f(K * ((2 * i) / (POINTS - 1) - 1));
  c[(POINTS - 1) / 2] = 0;
  curves.set(kind, c);
  return c;
}

// Speaker cabinets: [type, Hz, Q (dB for low/high pass, linear for
// peaking), gain dB]. A proposal: low resonance, presence, and the steep
// roll-off above about 5 kHz that takes the fizz off a direct drive.
const CABS = {
  open1x12: [["highpass", 90, 0], ["peaking", 2500, 1, 2], ["lowpass", 5500, BUTTERWORTH_Q_DB], ["lowpass", 5500, BUTTERWORTH_Q_DB]],
  "2x12": [["highpass", 80, 0], ["peaking", 1500, 1, 2], ["lowpass", 4500, BUTTERWORTH_Q_DB], ["lowpass", 4500, BUTTERWORTH_Q_DB]],
  "4x12": [["highpass", 70, 0], ["peaking", 120, 1.2, 3], ["lowpass", 4000, BUTTERWORTH_Q_DB], ["lowpass", 4000, BUTTERWORTH_Q_DB]],
};

const db = (v) => (v === 0 ? 1 : 10 ** (v / 20));

export function createDrivePedal(ctx, pedal, env) {
  const p = pedal.p;
  const sr = ctx.sampleRate;
  // A live input's Low latency switch (inputFx.js): no oversampling, so no
  // resampling filters and no latency, at the cost of aliasing on high
  // notes (measured in tests/e2e/fx-chain-dsp.spec.mjs). Stem lanes and the
  // export print never set it.
  const low = !!env.lowLatency;
  const lat = low ? 0 : Math.max(0, env.loopExtra?.shaper4x || 0);
  const nodes = [];
  const g = (v = 1) => { const n = ctx.createGain(); n.gain.value = v; nodes.push(n); return n; };
  const align = () => {
    const d = ctx.createDelay(0.1);
    d.channelCount = 2;
    d.channelCountMode = "explicit";
    d.delayTime.value = lat / sr;
    nodes.push(d);
    return d;
  };
  const bq = (type, f, q, gain = 0) => {
    const b = ctx.createBiquadFilter();
    b.type = type;
    b.frequency.value = f;
    b.Q.value = q;
    b.gain.value = gain;
    nodes.push(b);
    return b;
  };
  const mix = pedalMix({ ...pedal, p: { ...p, mix: p.blend } }, env.inst);
  const input = g();
  const output = g();
  const bypass = g(pedal.on ? 0 : 1);
  const feed = g(pedal.on ? 1 : 0);
  const bypassAlign = align();
  input.connect(bypass);
  bypass.connect(bypassAlign);
  bypassAlign.connect(output);
  input.connect(feed);

  let xover = null;
  let hi = feed;
  if (p.xover > 30 + 1e-9) {
    xover = createCrossover(ctx, p.xover, rampTo);
    nodes.push(...xover.nodes);
    feed.connect(xover.input);
    const lowAlign = align();
    xover.low.connect(lowAlign);
    lowAlign.connect(output);
    hi = xover.high;
  }
  const clean = g(mix.dry);
  const cleanAlign = align();
  hi.connect(clean);
  clean.connect(cleanAlign);
  cleanAlign.connect(output);

  // The drive path.
  const lowcut = bq("highpass", p.lowcut, BUTTERWORTH_Q_DB);
  const mid = bq("peaking", p.midf, 0.7, p.midg);
  const drive = g(db(p.drive) / K);
  const shaper = ctx.createWaveShaper();
  shaper.curve = curveFor(p.clip);
  shaper.oversample = low ? "none" : "4x";
  nodes.push(shaper);
  const dcBlock = bq("highpass", 12, BUTTERWORTH_Q_DB);
  const treble = bq("lowpass", p.highcut, BUTTERWORTH_Q_DB);
  hi.connect(lowcut);
  lowcut.connect(mid);
  mid.connect(drive);
  drive.connect(shaper);
  shaper.connect(dcBlock);
  dcBlock.connect(treble);
  let tail = treble;
  if (p.cab && p.cab !== "off" && CABS[p.cab]) {
    for (const [type, f, q, gain] of CABS[p.cab]) {
      const b = bq(type, f, q, gain || 0);
      tail.connect(b);
      tail = b;
    }
  }
  const lowShelf = bq("lowshelf", 100, 0, p.low || 0);
  tail.connect(lowShelf);
  const level = g(db(p.level));
  const wet = g(mix.wet);
  lowShelf.connect(level);
  level.connect(wet);
  wet.connect(output);

  let on = pedal.on;

  function update(next, nextEnv, immediate = false) {
    const np = next.p;
    const m = pedalMix({ ...next, p: { ...np, mix: np.blend } }, nextEnv.inst);
    rampTo(lowcut.frequency, np.lowcut, ctx, immediate);
    rampTo(mid.frequency, np.midf, ctx, immediate);
    rampTo(mid.gain, np.midg, ctx, immediate);
    rampTo(drive.gain, db(np.drive) / K, ctx, immediate);
    rampTo(treble.frequency, np.highcut, ctx, immediate);
    rampTo(lowShelf.gain, np.low || 0, ctx, immediate);
    rampTo(level.gain, db(np.level), ctx, immediate);
    if (xover) xover.setFreq(np.xover, immediate);
    rampTo(clean.gain, m.dry, ctx, immediate);
    rampTo(wet.gain, m.wet, ctx, immediate);
    setOn(next.on, immediate);
  }

  function setOn(value, immediate = false) {
    on = !!value;
    rampTo(feed.gain, on ? 1 : 0, ctx, immediate, XFADE);
    rampTo(bypass.gain, on ? 0 : 1, ctx, immediate, XFADE);
  }

  return {
    input, output, update, setOn,
    dispose() { for (const n of nodes) { try { n.disconnect(); } catch { /* gone */ } } },
    latencyFrames: lat,
    usesSongTime: false,
    isOn: () => on,
  };
}
