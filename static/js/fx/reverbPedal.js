// The Reverb pedal: native nodes, any BaseAudioContext.
//
//   input ─┬─ dry (mix dry; 1 when off) ───────────────────────────────────────┬─ output
//          └─ feed ─ [wet low cut] ─ pre-delay ─ convolver ─ high damp ─ [voice de-ess] ─ wet
//
// - The IR is generated (irGen.js) once per type, decay, drip and rate and
//   cached; the ConvolverNode plays it with normalize off, so a change of
//   decay does not change the wet level. A new IR is a new structure: the
//   chain host builds it beside the old one and crossfades (chainHost.js),
//   so the old tail rings out and nothing clicks.
// - Pre-delay keeps the first syllable dry (voice). A change of it is a new
//   structure, crossfaded: a ramped delay line would chirp (Doppler).
// - Bass: a low cut on the wet only, so a long low tail does not mask the
//   fundamentals (report 4.5). Voice: a high shelf at 6 kHz, -6 dB, on the
//   wet only ("De-ess": a bright tail exaggerates sibilance, report 4.5). A
//   split-band compressor de-esser is not done.
// - Off: the feed stops and the dry goes to 1; the tail rings out (trails).

import { rampTo, XFADE } from "./ramp.js";
import { BUTTERWORTH_Q_DB } from "./dspBlocks.js";
import { generateIr } from "./irGen.js";
import { pedalMix } from "./chainModel.js";

export function createReverbPedal(ctx, pedal, env) {
  const p = pedal.p;
  const nodes = [];
  const g = (v = 1) => { const n = ctx.createGain(); n.gain.value = v; nodes.push(n); return n; };
  const mix = pedalMix(pedal, env.inst);
  const input = g();
  const output = g();
  const dry = g(pedal.on ? mix.dry : 1);
  const feed = g(pedal.on ? 1 : 0);
  input.connect(dry);
  dry.connect(output);
  input.connect(feed);

  const lowcut = ctx.createBiquadFilter();
  lowcut.type = "highpass";
  lowcut.frequency.value = p.lowcut > 30 ? p.lowcut : 10;
  lowcut.Q.value = BUTTERWORTH_Q_DB;
  const pre = ctx.createDelay(0.25);
  // Explicit, as every delay line of the pedals (delayPedal.js says why).
  pre.channelCount = 2;
  pre.channelCountMode = "explicit";
  pre.delayTime.value = p.predelay / 1000;
  const conv = ctx.createConvolver();
  conv.normalize = false;
  const [l, r] = generateIr({ type: p.type, decay: p.decay, drip: p.drip, rate: ctx.sampleRate });
  const ir = ctx.createBuffer(2, l.length, ctx.sampleRate);
  ir.copyToChannel(l, 0);
  ir.copyToChannel(r, 1);
  conv.buffer = ir;
  const damp = ctx.createBiquadFilter();
  damp.type = "lowpass";
  damp.frequency.value = p.highdamp;
  damp.Q.value = BUTTERWORTH_Q_DB;
  const deess = ctx.createBiquadFilter();
  deess.type = "highshelf";
  deess.frequency.value = 6000;
  deess.gain.value = env.inst === "voice" && p.deess === "on" ? -6 : 0;
  const wet = g(mix.wet);
  nodes.push(lowcut, pre, conv, damp, deess);
  feed.connect(lowcut);
  lowcut.connect(pre);
  pre.connect(conv);
  conv.connect(damp);
  damp.connect(deess);
  deess.connect(wet);
  wet.connect(output);

  let on = pedal.on;
  let cur = { ...p };

  function update(next, nextEnv, immediate = false) {
    const np = next.p;
    const m = pedalMix(next, nextEnv.inst);
    rampTo(lowcut.frequency, np.lowcut > 30 ? np.lowcut : 10, ctx, immediate);
    rampTo(damp.frequency, np.highdamp, ctx, immediate);
    rampTo(deess.gain, nextEnv.inst === "voice" && np.deess === "on" ? -6 : 0, ctx, immediate);
    rampTo(wet.gain, m.wet, ctx, immediate);
    cur = { ...np };
    setOn(next.on, immediate, m);
  }

  function setOn(value, immediate = false, m = pedalMix({ ...pedal, p: cur }, env.inst)) {
    on = !!value;
    rampTo(feed.gain, on ? 1 : 0, ctx, immediate, XFADE);
    rampTo(dry.gain, on ? m.dry : 1, ctx, immediate, XFADE);
  }

  return {
    input, output, update, setOn,
    dispose() { for (const n of nodes) { try { n.disconnect(); } catch { /* gone */ } } },
    latencyFrames: 0,
    usesSongTime: false,
    isOn: () => on,
  };
}
