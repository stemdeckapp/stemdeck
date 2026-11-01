// When a playhead position counts as the end of a track. Shared by both audio
// engines (audioEngine.js, chunkedAudioEngine.js), which decide "ended" from
// their tick and report it through onEnded.
//
// Both engines start with a duration of 0 and learn the real one only once the
// stems have been read. Position 0 is "at or past" a duration of 0, so a tick
// in that window took the song for finished: nothing played, and onEnded fired
// the stemdeck:track-ended event, which in Karaoke moves the queue on to the
// next song. Only a known, positive duration has an end.

/**
 * @param {number} position  Playhead, in seconds.
 * @param {number} duration  Track length in seconds as the engine knows it.
 * @returns {boolean} True only when `duration` is a finite positive length and
 *   `position` has reached it.
 */
export function reachedEnd(position, duration) {
  return Number.isFinite(duration) && duration > 0 && position >= duration;
}
