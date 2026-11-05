// The live master: an approximate preview of the export's master
// (app/pipeline/mastering.py renders the exact result with ffmpeg).
//
//   input - pre (1/fader) -+- glue - cancel - gain - limiter - cancel - wet -+- post (fader) - output
//                          '- delay (the compressors' look-ahead) --- dry ---'
//
// - The export never applies the monitoring fader, and the master must see
//   the mix at the level the export renders it at: `pre` undoes the fader
//   (mixer.applyMix multiplies every lane by it) and `post` applies it again.
// - Native nodes only (DynamicsCompressorNode), so it runs over plain HTTP
//   and on phones where AudioWorklet does not exist (01-mastering 4.2).
// - Each DynamicsCompressorNode applies a fixed makeup gain (Web Audio spec,
//   1.19.4); the GainNode after it cancels it (masterMath.compressorMakeupGain).
// - The limiter is a ratio-20 compressor with the spec's fixed 6 ms
//   look-ahead: sample peaks only, so small overshoots against the export's
//   4x true-peak alimiter are expected. Labelled approximate in the UI.
// - The A/B "original" path is the same signal delayed by the compressors'
//   look-ahead, so switching between the two never jumps in time, and the
//   comparison is loudness-matched by the wet and dry trims.

import {
  compressorMakeupGain, glueSettings, limiterSettings, matchedLevels,
} from "./masterMath.js";

export const RAMP = 0.02; // seconds
// DynamicsCompressorNode's look-ahead (Web Audio spec: a 6 ms pre-delay).
export const COMPRESSOR_LOOKAHEAD = 0.006;

/**
 * The look-ahead of one compressor in whole frames at `rate`, in seconds.
 * Chromium delays by floor(6 ms x rate) frames: 288 at 48 kHz, 264 at
 * 44.1 kHz (measured: a 529.2-frame delay for two left the original path one
 * sample late against the master at 44.1 kHz).
 */
export function lookaheadSec(rate) {
  return Math.floor(COMPRESSOR_LOOKAHEAD * rate) / rate;
}

function ramp(param, value, ctx, immediate) {
  if (immediate || typeof param.linearRampToValueAtTime !== "function") {
    param.cancelScheduledValues?.(0);
    param.value = value;
    return;
  }
  const now = ctx.currentTime;
  // Not cancelAndHoldAtTime: it clicked on every knob step in Chromium
  // (fxChain.js rampTo has the measurement).
  const cur = param.value;
  param.cancelScheduledValues?.(now);
  param.setValueAtTime?.(cur, now);
  param.linearRampToValueAtTime(value, now + RAMP);
}

function setCompressor(node, s, ctx, immediate) {
  ramp(node.threshold, s.threshold, ctx, immediate);
  ramp(node.knee, s.knee, ctx, immediate);
  ramp(node.ratio, s.ratio, ctx, immediate);
  ramp(node.attack, s.attack, ctx, immediate);
  ramp(node.release, s.release, ctx, immediate);
}

// A compressor that does nothing (ratio 1): keeps the look-ahead, so the
// latency of the effect does not change when the glue compressor is off.
const IDLE = { threshold: 0, knee: 0, ratio: 1, attack: 0.02, release: 0.25 };

/**
 * @param {BaseAudioContext} ctx
 */
export function createMasterEffect(ctx) {
  const input = ctx.createGain();
  const pre = ctx.createGain();
  const glue = ctx.createDynamicsCompressor();
  const glueCancel = ctx.createGain();
  const gain = ctx.createGain();
  const limiter = ctx.createDynamicsCompressor();
  const limCancel = ctx.createGain();
  const wet = ctx.createGain();
  const delay = ctx.createDelay(0.1);
  const dry = ctx.createGain();
  const post = ctx.createGain();
  input.connect(pre);
  pre.connect(glue);
  glue.connect(glueCancel);
  glueCancel.connect(gain);
  gain.connect(limiter);
  limiter.connect(limCancel);
  limCancel.connect(wet);
  wet.connect(post);
  pre.connect(delay);
  delay.connect(dry);
  dry.connect(post);
  const latency = 2 * lookaheadSec(ctx.sampleRate);
  delay.delayTime.value = latency;
  dry.gain.value = 0;

  let state = {
    gainDb: 0, comp: 0, compThresholdDb: null, ceiling: -1, fader: 1, listen: "master", match: true, diffLu: NaN,
  };

  function apply(immediate = false) {
    const s = state;
    const fader = Math.max(0, s.fader);
    ramp(pre.gain, fader > 1e-3 ? 1 / fader : 1, ctx, immediate);
    ramp(post.gain, fader, ctx, immediate);
    const g = glueSettings(s.comp, s.compThresholdDb) || IDLE;
    setCompressor(glue, g, ctx, immediate);
    ramp(glueCancel.gain, 1 / compressorMakeupGain(g.threshold, g.knee, g.ratio), ctx, immediate);
    ramp(gain.gain, 10 ** (s.gainDb / 20), ctx, immediate);
    const l = limiterSettings(s.ceiling);
    setCompressor(limiter, l, ctx, immediate);
    ramp(limCancel.gain, 1 / compressorMakeupGain(l.threshold, l.knee, l.ratio), ctx, immediate);
    const lv = matchedLevels(s.diffLu, s.match);
    ramp(wet.gain, s.listen === "original" ? 0 : lv.master, ctx, immediate);
    ramp(dry.gain, s.listen === "original" ? lv.original : 0, ctx, immediate);
  }

  return {
    input,
    output: post,
    /** The mix as the master receives it, at export level (for the In meter). */
    inTap: pre,
    /** The master's result at export level, before the A/B and the fader. */
    outTap: limCancel,
    /**
     * {gainDb, comp, compThresholdDb, ceiling, fader, listen, match, diffLu};
     * any subset. comp 0 or compThresholdDb null: the glue compressor idles.
     */
    set(patch, { immediate = false } = {}) {
      state = { ...state, ...patch };
      apply(immediate);
    },
    get state() { return { ...state }; },
    /** The time the effect delays the signal by (both paths alike). */
    latencySec: latency,
    /** The compressors' current gain reduction in dB (glue, limiter). */
    reduction() {
      return { glue: glue.reduction ?? 0, limiter: limiter.reduction ?? 0 };
    },
    dispose() {
      for (const n of [input, pre, glue, glueCancel, gain, limiter, limCancel, wet, delay, dry, post]) {
        try { n.disconnect(); } catch { /* not connected */ }
      }
    },
  };
}
