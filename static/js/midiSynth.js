// The sound of a lane's MIDI: its notes rendered once to a mono AudioBuffer of
// the playing context (midiRender.js computes the samples), which the engine
// then plays as the lane's alternate source (audioEngine.addAltSource), so the
// lane's volume, mute, solo, EQ, effects, key, speed, loop and seeks apply to
// it as to the stem. The voice per lane is in midiNotesView.js.
//
// Memory: mono float32 at the context's rate (48 kHz at most,
// audioContext.js), about 11.5 MB a minute, held twice while it is copied in.
// Only the latest rendering of each lane is kept, and a new song drops them
// all (forgetSounds).

import { levelOf, soundKey, voiceFor } from "./midiNotesView.js";
import { fitLevel, renderVoice } from "./midiRender.js";

// lane key (job|stem) -> { key, promise }
const _sounds = new Map();

// The page stays responsive while a long line renders: a pause every so many
// notes lets a frame through.
const YIELD_EVERY = 64;
const pause = () => new Promise((r) => setTimeout(r, 0));

/** Whether a context can take the rendering. */
export const canRender = (context) => !!context && typeof context.createBuffer === "function";

/**
 * Render `notes` for `stem` over `duration` seconds into a buffer of
 * `context`, at the loudness of `reference` (the lane's stem buffer) when
 * given: measured on the test song, the synth came out 16 dB over its bass
 * stem without it, so Stem and MIDI jumped in level.
 */
export async function renderNotes(notes, { stem, duration, context, reference = null }) {
  const rate = context.sampleRate;
  const samples = await renderVoice(notes, voiceFor(stem), {
    duration,
    rate,
    onNote: (k) => ((k + 1) % YIELD_EVERY === 0 ? pause() : null),
  });
  const stemRms = reference && typeof reference.getChannelData === "function" ? levelOf(reference.getChannelData(0)).rms : 0;
  fitLevel(samples, stemRms);
  const buffer = context.createBuffer(1, samples.length, rate);
  buffer.copyToChannel(samples, 0);
  return buffer;
}

/**
 * The lane's sound for its current file: rendered once per file and rate,
 * the promise shared by every caller while it renders.
 */
export function laneSound({ jobId, stem, at, notes, duration, context, reference = null }) {
  const lane = `${jobId}|${stem}`;
  const key = soundKey(jobId, stem, at, context.sampleRate);
  const held = _sounds.get(lane);
  if (held && held.key === key) return held.promise;
  const promise = renderNotes(notes, { stem, duration, context, reference });
  _sounds.set(lane, { key, promise });
  promise.catch((e) => {
    console.warn("[midi] rendering the MIDI sound failed:", e);
    if (_sounds.get(lane)?.promise === promise) _sounds.delete(lane);
  });
  return promise;
}

/** Drop every rendering but those of `keepJobId` (a new song was opened). */
export function forgetSounds(keepJobId = null) {
  for (const lane of [..._sounds.keys()]) {
    if (!keepJobId || !lane.startsWith(`${keepJobId}|`)) _sounds.delete(lane);
  }
}
