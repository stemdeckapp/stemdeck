// The sound of a lane's MIDI notes, computed sample by sample into a mono
// Float32Array: the voice of midiNotesView.voiceFor, an envelope per note,
// and a low-pass over the whole line where the voice has one. The Drums
// lane's hits are drum sounds instead (renderDrums): a falling sine for a
// kick or a tom, filtered noise for a hi-hat or a cymbal, both for a snare. Pure, so
// tests/js/midi-render.test.mjs runs it in node; midiSynth.js copies the
// result into an AudioBuffer of the playing context.
//
// Not an OfflineAudioContext: one there processes every note's nodes for the
// whole song, and 551 bass notes of a 229 s song had not finished after 45 s
// in Chromium. Computed here, a note costs only its own samples (its length
// plus its release, for each partial): measured in Chromium at 44.1 kHz, the
// same 551 bass notes take 277 ms and 594 piano notes 325 ms, peak 0.86.

import { drumRow, levelOf, levelScale, pitchHz, renderSeconds, velocityGain } from "./midiNotesView.js";

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
  if (voice.kind === "drums") return renderDrums(notes, voice, { duration, rate, onNote });
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

// ─── The drums ───

/**
 * A drum hit's sound by its row (midiNotesView.drumRow): `decay` is the
 * time constant of its fall (s), `body` a sine gliding from `from` to `to`
 * Hz over its first 40 ms, `noise` the share of white noise and `bright` how
 * much of it is high-passed (0 dull, 1 a hiss). An open hi-hat (General MIDI
 * 46) rings longer than a closed one.
 */
export function drumSound(pitch) {
  const row = drumRow(pitch);
  if (row === "kick") return { decay: 0.12, body: { from: 120, to: 45, gain: 1 }, noise: 0.05, bright: 0.2 };
  if (row === "snare") return { decay: 0.07, body: { from: 210, to: 170, gain: 0.45 }, noise: 0.8, bright: 0.5 };
  if (row === "hihat") return { decay: pitch === 46 ? 0.12 : 0.025, body: null, noise: 0.5, bright: 0.95 };
  if (row === "tom") {
    // Floor tom (41) to high tom (50), 70 to 196 Hz.
    const hz = 70 + Math.max(0, Math.min(9, pitch - 41)) * 14;
    return { decay: 0.11, body: { from: hz * 1.5, to: hz, gain: 0.9 }, noise: 0.08, bright: 0.3 };
  }
  if (row === "cymbal") return { decay: 0.45, body: null, noise: 0.45, bright: 0.9 };
  return { decay: 0.03, body: { from: 900, to: 650, gain: 0.35 }, noise: 0.3, bright: 0.7 };
}

/** How long a hit sounds: five time constants of its fall, about 43 dB down. */
export const DRUM_TAIL = 5;

/**
 * The Drums lane's hits ([start s, end s, General MIDI note, velocity]) as
 * drum sounds into a mono buffer of `rate` samples a second, `duration`
 * seconds long at least. The noise is a fixed sequence (xorshift), so the
 * same file always sounds the same. `onNote(i)` as renderVoice's.
 */
export async function renderDrums(notes, voice, { duration, rate, onNote = null }) {
  let end = Number.isFinite(duration) && duration > 0 ? duration : 0;
  for (const n of notes) end = Math.max(end, n[0] + drumSound(n[2]).decay * DRUM_TAIL + 0.05);
  const length = Math.max(1, Math.ceil(end * rate));
  const out = new Float32Array(length);
  let seed = 0x9e3779b9;
  const noise = () => {
    seed ^= seed << 13;
    seed ^= seed >>> 17;
    seed ^= seed << 5;
    return ((seed >>> 0) / 4294967296) * 2 - 1;
  };
  for (let k = 0; k < notes.length; k++) {
    const [start, , pitch, velocity] = notes[k];
    const peak = velocityGain(velocity) * voice.gain;
    if (peak > 0) {
      const sound = drumSound(pitch);
      const a = Math.max(0, Math.floor(start * rate));
      const n = Math.min(length - a, Math.ceil(sound.decay * DRUM_TAIL * rate));
      const fall = Math.exp(-1 / Math.max(1, sound.decay * rate));
      const glide = Math.exp(-1 / Math.max(1, 0.04 * rate));
      let env = peak;
      let phase = 0;
      let bend = 1;
      let last = 0;
      for (let i = 0; i < n; i++) {
        let v = 0;
        if (sound.body) {
          const hz = sound.body.to + (sound.body.from - sound.body.to) * bend;
          phase += (2 * Math.PI * hz) / rate;
          v += Math.sin(phase) * sound.body.gain;
          bend *= glide;
        }
        if (sound.noise > 0) {
          const x = noise();
          v += (sound.bright * (x - last) * 0.5 + (1 - sound.bright) * x) * sound.noise;
          last = x;
        }
        // A 1 ms attack, so a hit does not click.
        const attack = Math.min(1, (i + 1) / Math.max(1, 0.001 * rate));
        out[a + i] += v * env * attack;
        env *= fall;
      }
    }
    if (onNote) await onNote(k);
  }
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
