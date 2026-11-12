// The Phaser pedal: native nodes, any BaseAudioContext.
//
//   input ─┬─ bypass (1 when off) ─────────────────────────────────────┬─ output
//          └─ feed ─┬─ [crossover low] ─────────────── (lows dry) ──────┤
//                   └─ [crossover high] = hi ─┬─ hiDry (mix dry) ───────┤
//                                             └─ allpass x N/2 ─ wet ───┘
//
// - A Web Audio "allpass" biquad at Q 0.5 (allpass Q is linear) has a double
//   real pole: it is exactly two identical first-order allpass stages. So N/2
//   biquads are the N-stage phaser of the Phase 45 (2) and Phase 90 (4), all
//   stages moved by one control (report 4.2, derived). Mixed 50/50 with the
//   dry signal, 4 stages notch at 0.414 and 2.414 times the corner, 2 stages
//   at the corner.
// - The sweep is exponential, as a JFET or OTA phaser's: the LFO drives each
//   biquad's detune (cents), Depth octaves wide, centred on Centre.
// - Wide: a second chain on the right side with the LFO inverted.
// - Not here, by design: feedback (the block-logo Phase 90, Empress
//   resonance) and odd stage counts (3). A Web Audio cycle needs a delay of
//   at least one render quantum, which turns phaser feedback into a comb, and
//   a sweepable first-order allpass has no native node. Both need an
//   AudioWorklet; they are listed as not done.

import { rampTo, XFADE } from "./ramp.js";
import { createLfo, createCrossover } from "./dspBlocks.js";
import { pedalMix } from "./chainModel.js";

/** The LFO rate: the knob, or one bar of 4/4 at the song's tempo. */
export function phaserRate(p, bpm) {
  return p.sync === "bar" && bpm > 0 ? bpm / 240 : p.rate;
}

export function createPhaserPedal(ctx, pedal, env) {
  const p = pedal.p;
  const nodes = [];
  const g = (v = 1) => { const n = ctx.createGain(); n.gain.value = v; nodes.push(n); return n; };
  const mix = pedalMix(pedal, env.inst);
  const input = g();
  const output = g();
  const bypass = g(pedal.on ? 0 : 1);
  const feed = g(pedal.on ? 1 : 0);
  input.connect(bypass);
  bypass.connect(output);
  input.connect(feed);

  const xoverOn = p.xover > 30 + 1e-9;
  let xover = null;
  let hi = feed;
  if (xoverOn) {
    xover = createCrossover(ctx, p.xover, rampTo);
    nodes.push(...xover.nodes);
    feed.connect(xover.input);
    xover.low.connect(output);
    hi = xover.high;
  }
  const hiDry = g(mix.dry);
  hi.connect(hiDry);
  hiDry.connect(output);
  const wet = g(mix.wet);
  wet.connect(output);

  const stages = Number(p.stages);
  const chain = (src, dst, dstInput = 0) => {
    const list = [];
    let prev = src;
    for (let i = 0; i < stages / 2; i++) {
      const ap = ctx.createBiquadFilter();
      ap.type = "allpass";
      ap.frequency.value = p.centre;
      ap.Q.value = 0.5;
      nodes.push(ap);
      prev.connect(ap);
      prev = ap;
      list.push(ap);
    }
    prev.connect(dst, 0, dstInput);
    return list;
  };

  const lfo = createLfo(ctx, {
    rate: phaserRate(p, env.bpm), depth: (p.depth / 2) * 1200, songTime: env.songTime || 0, shape: p.shape,
    when: env.when ?? ctx.currentTime,
  });
  const all = [];
  if (p.stereo === "wide") {
    const up = g();
    up.channelCount = 2;
    up.channelCountMode = "explicit";
    up.channelInterpretation = "speakers";
    const split = ctx.createChannelSplitter(2);
    const merge = ctx.createChannelMerger(2);
    nodes.push(split, merge);
    hi.connect(up);
    up.connect(split);
    const left = g();
    const right = g();
    split.connect(left, 0);
    split.connect(right, 1);
    const l = chain(left, merge, 0);
    const r = chain(right, merge, 1);
    merge.connect(wet);
    const inv = g(-1);
    lfo.output.connect(inv);
    for (const ap of l) lfo.output.connect(ap.detune);
    for (const ap of r) inv.connect(ap.detune);
    all.push(...l, ...r);
  } else {
    const l = chain(hi, wet);
    for (const ap of l) lfo.output.connect(ap.detune);
    all.push(...l);
  }

  let on = pedal.on;

  function update(next, nextEnv, immediate = false) {
    const np = next.p;
    const m = pedalMix(next, nextEnv.inst);
    rampTo(lfo.output.gain, (np.depth / 2) * 1200, ctx, immediate);
    for (const ap of all) rampTo(ap.frequency, np.centre, ctx, immediate);
    if (xover) xover.setFreq(np.xover, immediate);
    rampTo(wet.gain, m.wet, ctx, immediate);
    rampTo(hiDry.gain, m.dry, ctx, immediate);
    setOn(next.on, immediate);
  }

  function setOn(value, immediate = false) {
    on = !!value;
    rampTo(feed.gain, on ? 1 : 0, ctx, immediate, XFADE);
    rampTo(bypass.gain, on ? 0 : 1, ctx, immediate, XFADE);
  }

  return {
    input, output, update, setOn,
    dispose() {
      lfo.stop();
      for (const n of nodes) { try { n.disconnect(); } catch { /* gone */ } }
    },
    latencyFrames: 0,
    usesSongTime: true,
    isOn: () => on,
  };
}
