// The Delay pedal: native Web Audio nodes, any BaseAudioContext (live or
// the export print's OfflineAudioContext).
//
//   input ─┬─ dry ────────────────────────────────────────────────┬─ output
//          └─ feed ─ D1 (time) ─ s ─ low cut ─ high cut ─ shaper ─┼─ wet ─┘
//                                ^                           │
//                                └── D2 (time - overhead) ─ fb ┘
//
// - D1 places the first repeat at exactly `time`. The loop through D2 then
//   adds one more `time` per repeat: D2 is shorter than `time` by what this
//   browser adds to every pass round a feedback cycle, which is measured, not
//   assumed (dspBlocks.loopOverheadFrames: 128 frames in Chromium 151).
// - The filters and the shaper are inside the loop, so every repeat is cut
//   and saturated once more, the way a bucket-brigade or tape delay darkens.
// - The shaper bounds the loop: at 98 % feedback a repeat can never grow.
//   Digital: exactly linear below 0.8, a soft knee above. Analog and tape: a
//   tanh, slope 1 at zero.
// - Ping-pong: the input summed to mono, the first repeat on the left, then
//   alternating sides, with one line of two repeats in the cycle (see below).
// - Modulation: one LFO on every line's delay time (the wow, Modulation and
//   Rate); tape adds a fixed flutter of 0.1 ms at 6.5 Hz (proposal).
// - Ducking (voice): an envelope follower on the input lowers the wet level
//   while the voice sings, by up to `duck` dB.
// - Off: the feed stops and the dry goes to 1; the repeats already in the
//   loop ring out (trails, report D7).

import { rampTo, XFADE } from "./ramp.js";
import { createLfo, loopCurve, createDuckFollower, BUTTERWORTH_Q_DB } from "./dspBlocks.js";
import { delaySeconds, pedalMix } from "./chainModel.js";

const MAX_TIME = 2.0 + 0.006 + 0.002; // the knob's top, plus modulation
const FLUTTER_HZ = 6.5;
const FLUTTER_SEC = 0.0001;
// A time change in analog and tape glides with this time constant, the
// pitch bend of a real analog or tape delay (proposal).
const GLIDE_TAU = 0.06;

function makeSide(ctx, p) {
  const s = ctx.createGain();
  const hp = ctx.createBiquadFilter();
  hp.type = "highpass";
  hp.frequency.value = p.lowcut;
  hp.Q.value = BUTTERWORTH_Q_DB;
  const lp = ctx.createBiquadFilter();
  lp.type = "lowpass";
  lp.frequency.value = p.highcut;
  lp.Q.value = BUTTERWORTH_Q_DB;
  const pre = ctx.createGain();
  pre.gain.value = 0.5;
  const sh = ctx.createWaveShaper();
  sh.curve = loopCurve(p.char === "digital" ? "knee" : "tanh");
  s.connect(hp);
  hp.connect(lp);
  lp.connect(pre);
  pre.connect(sh);
  return { s, hp, lp, pre, sh, out: sh, nodes: [s, hp, lp, pre, sh] };
}

/**
 * pedal: a normalised delay pedal ({iid, type, on, p}).
 * env: {inst, bpm, loopExtra: {mono, pingpong} (frames), songTime, when}.
 */
export function createDelayPedal(ctx, pedal, env) {
  const p = pedal.p;
  const sr = ctx.sampleRate;
  const nodes = [];
  const g = (v = 1) => { const n = ctx.createGain(); n.gain.value = v; nodes.push(n); return n; };
  const input = g();
  const output = g();
  const mix = pedalMix(pedal, env.inst);
  const dry = g(pedal.on ? mix.dry : 1);
  const feed = g(pedal.on ? 1 : 0);
  const wet = g(mix.wet);
  input.connect(dry);
  dry.connect(output);
  input.connect(feed);
  wet.connect(output);

  const ping = p.stereo === "pingpong";
  const time = delaySeconds(p, env.bpm);
  const extra = env.loopExtra?.mono || 0;
  // A line `k` repeats long; the one in the cycle is short by the overhead.
  const lineTime = (t, loop, k) => Math.max(0, k * t - (loop ? extra / sr : 0));
  const fbGains = [];
  const lines = []; // [{node, loop: bool, k}]
  const line = (t, loop, k = 1) => {
    const d = ctx.createDelay(MAX_TIME * k);
    // An explicit channel count, always. Measured in Chromium 151: a
    // DelayNode at the default ("max") empties its line when its input's
    // channel count changes, which happens when the source stops, so the
    // repeats of a stopped song (the trails) were lost; "explicit" keeps
    // them. Ping-pong lines carry the mono sum.
    d.channelCount = ping ? 1 : 2;
    d.channelCountMode = "explicit";
    d.channelInterpretation = "speakers";
    d.delayTime.value = lineTime(t, loop, k);
    nodes.push(d);
    lines.push({ node: d, loop, k });
    return d;
  };
  // An LFO moves a line k repeats long k times as far.
  const modTarget = (l) => {
    if (l.k === 1) return l.node.delayTime;
    const scale = g(l.k);
    scale.connect(l.node.delayTime);
    return scale;
  };
  const sides = [];
  if (!ping) {
    const a = makeSide(ctx, p);
    sides.push(a);
    const d1 = line(time, false);
    const d2 = line(time, true);
    const fb = g(p.fb);
    fbGains.push(fb);
    feed.connect(d1);
    d1.connect(a.s);
    a.out.connect(fb);
    fb.connect(d2);
    d2.connect(a.s);
    a.out.connect(wet);
  } else {
    // One delay line in the cycle, of two repeats' length, and the right
    // side as a tap outside it. Measured in Chromium 151:
    // - with two delay lines in one cycle, the whole 128-frame overhead
    //   landed on one of them, so it cannot be split between two lines;
    // - a delay fed from the node right after the cycle's line (A) delayed
    //   exactly, one fed from further round the cycle (B) was 128 frames late.
    // So the left side is A, the cycle runs two passes per two repeats (A,
    // feedback, B, feedback, the line), and the right side is A delayed by
    // one repeat and then given that repeat's pass (feedback, filters,
    // shaper) by a copy of B: the same signal, because a pure delay commutes
    // with the filters and with a memoryless shaper.
    //   feed - mono - D1 (t) - A ─ fb - B - fb - Dloop (2t - overhead) - A
    //                          A ─ DR (t) - fb - C (a copy of B) - right
    const mono = g();
    mono.channelCount = 1;
    mono.channelCountMode = "explicit";
    mono.channelInterpretation = "speakers";
    const left = makeSide(ctx, p);
    const back = makeSide(ctx, p);
    const right = makeSide(ctx, p);
    sides.push(left, back, right);
    const d1 = line(time, false);
    const loop2 = line(time, true, 2);
    const toR = line(time, false);
    const fbA = g(p.fb);
    const fbB = g(p.fb);
    const fbR = g(p.fb);
    fbGains.push(fbA, fbB, fbR);
    feed.connect(mono);
    mono.connect(d1);
    d1.connect(left.s);
    left.out.connect(fbA);
    fbA.connect(back.s);
    back.out.connect(fbB);
    fbB.connect(loop2);
    loop2.connect(left.s);
    left.out.connect(toR);
    toR.connect(fbR);
    fbR.connect(right.s);
    const merger = ctx.createChannelMerger(2);
    nodes.push(merger);
    left.out.connect(merger, 0, 0);
    right.out.connect(merger, 0, 1);
    merger.connect(wet);
  }
  for (const s of sides) nodes.push(...s.nodes);

  // Modulation of every line's time. Built only when it moves something.
  const lfos = [];
  const songTime = env.songTime || 0;
  const when = env.when ?? ctx.currentTime;
  let modDepth = null;
  const targets = (p.mod > 0 || p.char === "tape") ? lines.map(modTarget) : [];
  if (p.mod > 0) {
    const lfo = createLfo(ctx, { rate: p.rate, depth: p.mod / 1000, songTime, when });
    lfos.push(lfo);
    modDepth = lfo.output;
    for (const tgt of targets) lfo.output.connect(tgt);
  }
  if (p.char === "tape") {
    const fl = createLfo(ctx, { rate: FLUTTER_HZ, depth: FLUTTER_SEC, songTime, when });
    lfos.push(fl);
    for (const tgt of targets) fl.output.connect(tgt);
  }

  // Ducking (voice).
  let follower = null;
  let duckAmt = null;
  const duckGain = (pp, m) => -m.wet * (1 - 10 ** (-(pp.duck || 0) / 20));
  if (env.inst === "voice" && p.duck > 0) {
    follower = createDuckFollower(ctx);
    duckAmt = g(duckGain(p, mix));
    input.connect(follower.input);
    follower.output.connect(duckAmt);
    duckAmt.connect(wet.gain);
  }

  let on = pedal.on;
  let cur = { ...p };

  function update(next, nextEnv, immediate = false) {
    const np = next.p;
    const m = pedalMix(next, nextEnv.inst);
    for (const fb of fbGains) rampTo(fb.gain, np.fb, ctx, immediate);
    for (const s of sides) {
      rampTo(s.hp.frequency, np.lowcut, ctx, immediate);
      rampTo(s.lp.frequency, np.highcut, ctx, immediate);
    }
    rampTo(wet.gain, m.wet, ctx, immediate);
    if (duckAmt) rampTo(duckAmt.gain, duckGain(np, m), ctx, immediate);
    if (modDepth) rampTo(modDepth.gain, np.mod / 1000, ctx, immediate);
    // Analog and tape: a time change glides. Digital: a time change is a
    // new structure (chainModel.pedalStructureKey), so it never gets here.
    const t = delaySeconds(np, nextEnv.bpm);
    if (np.char !== "digital" && Math.abs(t - delaySeconds(cur, nextEnv.bpm)) > 1e-9) {
      for (const l of lines) {
        const target = lineTime(t, l.loop, l.k);
        if (immediate) { l.node.delayTime.cancelScheduledValues(0); l.node.delayTime.value = target; }
        else {
          l.node.delayTime.cancelScheduledValues(ctx.currentTime);
          l.node.delayTime.setTargetAtTime(target, ctx.currentTime, GLIDE_TAU);
        }
      }
    }
    cur = { ...np };
    setOn(next.on, immediate, m);
  }

  function setOn(value, immediate = false, m = pedalMix({ ...pedal, p: cur }, env.inst)) {
    on = !!value;
    rampTo(feed.gain, on ? 1 : 0, ctx, immediate, XFADE);
    rampTo(dry.gain, on ? m.dry : 1, ctx, immediate, XFADE);
  }

  function dispose() {
    for (const l of lfos) l.stop();
    follower?.dispose();
    for (const n of nodes) { try { n.disconnect(); } catch { /* gone */ } }
  }

  return {
    input, output, update, setOn, dispose,
    latencyFrames: 0,
    // An LFO whose phase follows song time: rebuilt on a play start.
    usesSongTime: lfos.length > 0,
    isOn: () => on,
  };
}
