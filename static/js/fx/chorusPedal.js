// The Chorus pedal (chorus, vibrato, voice doubler, flanger): native nodes,
// any BaseAudioContext (live or the export print).
//
//   input ─┬─ bypass (1 when off) ──────────────────────────────────────────┬─ output
//          └─ feed ─┬─ [crossover low] ─────────────────── (lows dry) ──────┤
//                   └─ [crossover high] = hi ─┬─ hiDry (mix dry) ───────────┤
//                                             └─ wet EQ ─ L/R delay lines ─ wet (mix wet)
//
// - Each side is a modulated delay line; one LFO drives both, inverted on the
//   right in "wide" (Juno and Dimension D behaviour, reported). The LFO's
//   phase follows song time (dspBlocks.createLfo), so the export print
//   modulates exactly as playback did.
// - The crossover (bass) keeps the fundamental dry: a Linkwitz-Riley split,
//   lows straight to the output, the part above through the pedal's mix
//   (M83 X-OVER). Off at the knob's lowest setting: no split at all.
// - Feedback (flanger): each side gets the delay pedal's loop, a forward
//   line for the first pass and a loop line shortened by this browser's
//   measured per-pass overhead, so every pass is exactly the swept delay.
//   The sweep's floor is then that overhead plus 0.2 ms.
// - Off: the bypass path at exactly 1 and the feed at 0: bit-identical to
//   the input once the 20 ms crossfade has run. A chorus has no trails.

import { rampTo, XFADE } from "./ramp.js";
import { createLfo, createCrossover, BUTTERWORTH_Q_DB } from "./dspBlocks.js";
import { pedalMix } from "./chainModel.js";

const EXCURSION_SEC = 0.003; // depth 100 % = +/-3 ms
const MIN_SEC = 0.0002;
const MAX_SEC = 0.04;

/** The base delay of each side and the sweep, in seconds, kept above the floor. */
export function chorusTimes(p, floorSec = MIN_SEC) {
  const exc = p.depth * EXCURSION_SEC;
  const left = Math.max(p.dtime / 1000, floorSec + exc);
  const right = Math.max((p.dtime + p.offset) / 1000, floorSec + exc);
  return { left, right, exc };
}

export function createChorusPedal(ctx, pedal, env) {
  const p = pedal.p;
  const sr = ctx.sampleRate;
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

  // The crossover, only when it is on.
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

  // Wet EQ: the M234's LOW shelf and HIGH tone, on the effect only.
  const shelf = ctx.createBiquadFilter();
  shelf.type = "lowshelf";
  shelf.frequency.value = 200;
  shelf.gain.value = p.wetlow;
  const tone = ctx.createBiquadFilter();
  tone.type = "lowpass";
  tone.frequency.value = p.highcut;
  tone.Q.value = BUTTERWORTH_Q_DB;
  nodes.push(shelf, tone);
  // Stereo in, whatever arrives (a mono track is spread to both sides).
  const up = g();
  up.channelCount = 2;
  up.channelCountMode = "explicit";
  up.channelInterpretation = "speakers";
  hi.connect(shelf);
  shelf.connect(tone);
  tone.connect(up);
  const split = ctx.createChannelSplitter(2);
  const merge = ctx.createChannelMerger(2);
  nodes.push(split, merge);
  up.connect(split);

  const fbOn = p.fb > 0;
  const extra = fbOn ? (env.loopExtra?.mono || 0) / sr : 0;
  const floor = MIN_SEC + extra;
  const t = chorusTimes(p, floor);
  const wet = g(mix.wet);
  merge.connect(wet);
  wet.connect(output);

  const line = (sec) => {
    const d = ctx.createDelay(MAX_SEC + EXCURSION_SEC);
    // Explicit, as every delay line of the pedals (delayPedal.js says why).
    d.channelCount = 1;
    d.channelCountMode = "explicit";
    d.delayTime.value = sec;
    nodes.push(d);
    return d;
  };
  const sides = [];
  for (const [ch, base] of [[0, t.left], [1, t.right]]) {
    const fwd = line(base);
    split.connect(fwd, ch);
    const side = { fwd, base, loop: null, fbGain: null };
    if (fbOn) {
      // in -> fwd -> s -> out; out -> fb -> loop (base - overhead) -> s
      const s = g();
      fwd.connect(s);
      const loop = line(Math.max(MIN_SEC, base - extra));
      const fbGain = g(p.fb);
      s.connect(fbGain);
      fbGain.connect(loop);
      loop.connect(s);
      s.connect(merge, 0, ch);
      side.loop = loop;
      side.fbGain = fbGain;
    } else {
      fwd.connect(merge, 0, ch);
    }
    sides.push(side);
  }

  // One LFO, inverted on the right in "wide".
  const lfo = createLfo(ctx, { rate: p.rate, depth: t.exc, songTime: env.songTime || 0, shape: p.shape, when: env.when ?? ctx.currentTime });
  const right = g(p.stereo === "wide" ? -1 : 1);
  lfo.output.connect(right);
  for (const [i, side] of sides.entries()) {
    const src = i === 0 ? lfo.output : right;
    src.connect(side.fwd.delayTime);
    if (side.loop) src.connect(side.loop.delayTime);
  }

  let on = pedal.on;
  let cur = { ...p };

  function update(next, nextEnv, immediate = false) {
    const np = next.p;
    const m = pedalMix(next, nextEnv.inst);
    const nt = chorusTimes(np, floor);
    rampTo(lfo.output.gain, nt.exc, ctx, immediate);
    rampTo(shelf.gain, np.wetlow, ctx, immediate);
    rampTo(tone.frequency, np.highcut, ctx, immediate);
    if (xover) xover.setFreq(np.xover, immediate);
    for (const [i, side] of sides.entries()) {
      const base = i === 0 ? nt.left : nt.right;
      rampTo(side.fwd.delayTime, base, ctx, immediate);
      if (side.loop) rampTo(side.loop.delayTime, Math.max(MIN_SEC, base - extra), ctx, immediate);
      if (side.fbGain) rampTo(side.fbGain.gain, np.fb, ctx, immediate);
    }
    rampTo(wet.gain, m.wet, ctx, immediate);
    rampTo(hiDry.gain, m.dry, ctx, immediate);
    cur = { ...np };
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
    get params() { return cur; },
  };
}
