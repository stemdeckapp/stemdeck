// A lane's alternate source (a MIDI rendering of the lane, laneMidiPlay.js):
// one in-memory AudioBuffer as long as the song, played beside the lane's stem
// by both engines (audioEngine.js, chunkedAudioEngine.js). The pure part both
// share: which of the two sounds, and where the buffer starts on the clock
// the stems are scheduled on. Kept free of Web Audio so node tests can check
// the maths (tests/js/alt-source.test.mjs).

/** The lane source modes an engine takes (setLaneSource). */
export const LANE_SOURCE_MODES = Object.freeze(["stem", "alt", "both"]);

/** A mode, made one of LANE_SOURCE_MODES ("stem" for anything else). */
export function laneSourceMode(mode) {
  return LANE_SOURCE_MODES.includes(mode) ? mode : "stem";
}

/**
 * 1 when a lane in `mode` sounds its alternate source (`alt` true) or its
 * stem (`alt` false), else 0. The engine multiplies the lane's level by it.
 */
export function sourceOn(mode, alt) {
  const m = laneSourceMode(mode);
  return (alt ? m !== "stem" : m !== "alt") ? 1 : 0;
}

/**
 * Head room for joining a playback that is already running. A source started
 * at a time that has already passed starts at once but from the offset it
 * was given, so it would sound late by however long the start took to reach
 * the audio thread. Starting it this far ahead, at the place the stems will
 * have reached by then, keeps it on the stems' clock.
 */
export const ALT_JOIN_MARGIN = 0.05;

/**
 * Where an alternate source of `bufferDuration` seconds starts.
 *
 * Both engines schedule the stems so that song time `t` enters the graph at
 * context time `startCtxTime + (t - startOffset) / srcRate`, where `srcRate`
 * is the rate the source nodes read their buffers at (1 while the tempo stage
 * stretches, the playback rate in the tape-effect fallback). A buffer that
 * starts at song time `offset` at context time `when` with that same rate
 * stays on that line, so it stays in the same sample frame as the stems.
 *
 * At a start (`join` false) it starts with the stems: at `startCtxTime`,
 * `startOffset` into the buffer. Joining a running playback (`join` true) it
 * starts at the later of `startCtxTime` (a count-in still running) and
 * `ctxNow + margin`, at the song time the stems reach then.
 *
 * Null when that place is at or past the end of the buffer.
 *
 * @returns {{when:number, offset:number}|null}
 */
export function altSourceStart({
  startCtxTime, startOffset, srcRate = 1, ctxNow = 0, bufferDuration, join = false, margin = ALT_JOIN_MARGIN,
}) {
  const rate = srcRate > 0 ? srcRate : 1;
  const when = join ? Math.max(startCtxTime, ctxNow + margin) : startCtxTime;
  const offset = Math.max(0, startOffset + (when - startCtxTime) * rate);
  if (!(offset < bufferDuration)) return null;
  return { when, offset };
}
