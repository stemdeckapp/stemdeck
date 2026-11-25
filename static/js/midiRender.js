// The sound of a lane's MIDI notes, computed sample by sample into a mono
// Float32Array: the voice of midiNotesView.voiceFor, an envelope per note,
// and for Bass one low-pass over the whole line. Pure, so
// tests/js/midi-render.test.mjs runs it in node; midiSynth.js copies the
// result into an AudioBuffer of the playing context.
//
// Not an OfflineAudioContext: one there processes every note's nodes for the
// whole song, and 551 bass notes of a 229 s song had not finished after 45 s
// in Chromium. Computed here, a note costs only its own samples (its length
// plus its release, for each partial): measured in Chromium at 44.1 kHz, the
// same 551 bass notes take 277 ms and 594 piano notes 325 ms, peak 0.86.

import { levelOf, levelScale, pitchHz, renderSeconds, velocityGain } from "./midiNotesView.js";

/** One partial of one note, added into `out` from sample `a` to `b`. */
function addPartial(out, a, b, type, hz, gain, rate, env) {
  const w = (2 * Math.PI * hz) / rate;
  if (type === "sine") {
    // A rotating phasor: two multiplies a sample, no Math.sin in the loop.
    const c = 2 * Math.cos(w);
    let y1 = Math.sin(-w);
    let y2 = Math.sin(-2 * w);
    for (let i = a; i < b; i++) {
      const y = c * y1 - y2;
      y2 = y1;
      y1 = y;
      out[i] += y * gain * env[i - a];
    }
    return;
  }
  const step = hz / rate;
  let phase = 0;
  for (let i = a; i < b; i++) {
    // sawtooth -1..1, triangle -1..1
    const v = type === "sawtooth" ? 2 * phase - 1 : 1 - 4 * Math.abs(phase - 0.5);
    out[i] += v * gain * env[i - a];
    phase += step;
    if (phase >= 1) phase -= 1;
  }
}

/**
 * The note's envelope from its start sample, `n` samples long: a linear
 * attack to `peak`, a decay towards peak * sustain while held, then a release
 * towards silence, the same shape setTargetAtTime would draw.
 */
export function envelope(n, { attack, decay, sustain, release }, peak, heldSec, rate) {
  const env = new Float32Array(n);
  const att = Math.max(1, Math.round(attack * rate));
  const held = Math.max(att, Math.round(heldSec * rate));
  const kDecay = Math.exp(-1 / Math.max(1, (decay / 3) * rate));
  const kRelease = Math.exp(-1 / Math.max(1, (release / 4) * rate));
  const floor = peak * sustain;
  let v = 0;
  for (let i = 0; i < n; i++) {
    if (i < att) v = (peak * (i + 1)) / att;
    else if (i < held) v = floor + (v - floor) * kDecay;
    else v *= kRelease;
    env[i] = v;
  }
  return env;
}

/** A biquad low-pass (RBJ cookbook) over `x`, in place. */
export function lowpassInPlace(x, cutoff, rate, q = 0.7) {
  const w = (2 * Math.PI * Math.min(cutoff, rate * 0.45)) / rate;
  const alpha = Math.sin(w) / (2 * q);
  const cos = Math.cos(w);
  const a0 = 1 + alpha;
  const b0 = (1 - cos) / 2 / a0;
  const b1 = (1 - cos) / a0;
  const b2 = b0;
  const a1 = (-2 * cos) / a0;
  const a2 = (1 - alpha) / a0;
  let x1 = 0, x2 = 0, y1 = 0, y2 = 0;
  for (let i = 0; i < x.length; i++) {
    const x0 = x[i];
    const y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2;
    x2 = x1; x1 = x0; y2 = y1; y1 = y0;
    x[i] = y0;
  }
  return x;
}

/**
 * The notes ([start s, end s, pitch, velocity]) played by `voice` into a
 * mono buffer of `rate` samples a second, `duration` seconds long at least.
 * `onNote(i)` is called after each note (a caller may yield there).
 */
export async function renderVoice(notes, voice, { duration, rate, onNote = null }) {
  const length = Math.max(1, Math.ceil(renderSeconds(notes, duration, voice) * rate));
  const out = new Float32Array(length);
  for (let k = 0; k < notes.length; k++) {
    const [start, end, pitch, velocity] = notes[k];
    const peak = velocityGain(velocity) * voice.gain;
    if (peak > 0) {
      const a = Math.max(0, Math.floor(start * rate));
      const heldSec = Math.max(voice.attack, end - start);
      // The release ends when it is 60 dB down: four time constants is 35 dB,
      // so it runs to 7 of them (release / 4 each).
      const n = Math.min(length - a, Math.ceil((heldSec + (voice.release / 4) * 7) * rate));
      if (n > 0) {
        const env = envelope(n, voice, peak, heldSec, rate);
        const hz = pitchHz(pitch);
        for (const part of voice.partials) {
          const f = hz * part.ratio;
          if (f < rate / 2) addPartial(out, a, a + n, part.type, f, part.gain, rate, env);
        }
      }
    }
    if (onNote) await onNote(k);
  }
  if (voice.lowpass > 0) lowpassInPlace(out, voice.lowpass, rate);
  return out;
}

/**
 * Scale `samples` in place to the loudness of a stem whose RMS is `stemRms`
 * (levelScale), and in every case under a peak of 0.98: five full-velocity
 * notes of the Keys voice reach 1.03 on their own. Returns the gain used.
 */
export function fitLevel(samples, stemRms = 0) {
  const own = levelOf(samples);
  let k = levelScale(own.rms, stemRms, own.peak);
  if (own.peak * k > 0.98) k = 0.98 / own.peak;
  if (k !== 1) for (let i = 0; i < samples.length; i++) samples[i] *= k;
  return k;
}
