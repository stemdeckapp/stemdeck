// Which worklet input a mixer lane belongs on.
//
// The SoundTouch worklet exposes one input per semitone it offers, so a lane's
// transpose is expressed as a *connection*, not as a parameter: routing the
// lane to input k is what makes it sound k - ZERO_INPUT semitones away. Both
// audio engines have to agree on that mapping exactly, which is why it lives
// here rather than twice.

export const PITCH_MIN = -6;
export const PITCH_MAX = 6;
export const INPUT_COUNT = PITCH_MAX - PITCH_MIN + 1;
// Input for semitone 0. It is the plain delay line, so it carries drums, the
// click, and every lane sitting at its own key.
export const ZERO_INPUT = -PITCH_MIN;

export function clampPitch(semitones) {
  const n = Math.round(Number(semitones) || 0);
  return Math.max(PITCH_MIN, Math.min(PITCH_MAX, n));
}

/**
 * The shift a lane actually gets.
 *
 * A lane's stored key is absolute, not an offset from the global control. The
 * global control moves every lane by the amount it changed, so the number on a
 * lane is always the key that lane is actually in, and there is never a second
 * number to add in your head to know what you are hearing.
 *
 * Drums are never transposed, whatever any control says. Resampling a snare
 * does not move it to another key, it makes it a different drum.
 * `pitchable === false` covers the same refusal for a rendered complement lane
 * that still has drums mixed into it.
 */
export function effectivePitch(name, semitones, pitchable) {
  if (name === "drums" || pitchable === false) return 0;
  return clampPitch(semitones);
}

/** Worklet input index for a given shift. */
export function inputForPitch(semitones) {
  return clampPitch(semitones) + ZERO_INPUT;
}

/**
 * The i18n key explaining why transpose is unavailable.
 *
 * Here for the same reason the input mapping is: the transport control and the
 * lane steppers both have to say it, and two copies of this answer would drift
 * into two different answers to one question.
 *
 * AudioWorklet is a secure-context API. A browser on http://<lan-ip> is never
 * given it, so the SoundTouch stage cannot be built at all -- which is the
 * common case by far, StemDeck hosted on the network and opened from another
 * machine. "Needs Web Audio" sent those people looking for a missing browser
 * feature when Web Audio was working fine and the origin was the problem
 * (#552). Only on an already-secure origin is a genuine worklet failure the
 * remaining explanation.
 */
export function pitchBlockedKey() {
  return window.isSecureContext === false ? "pitch.insecureOrigin" : "pitch.unavailable";
}

/**
 * The global key a song's saved record was left at.
 *
 * The record holds it under `__global.pitch`. A record from before the global
 * key was saved (0.20 and earlier, `stemdeck:mix:<jobId>`) has only the lanes,
 * which are absolute: the global control moved every lane but drums by the
 * same amount. Opening one showed 0 on the counter over lanes still playing
 * two semitones down (#781), so the key is read back from the lanes: the one
 * most of the song's pitched lanes are in, the one nearer the original on a
 * tie, and 0 when no lane says anything.
 *
 * `laneNames` are the lanes the song actually has; a record also holds every
 * lane the app knows, at whatever the control last pushed them to.
 */
export function savedGlobalPitch(record, laneNames) {
  const stored = record?.__global?.pitch;
  if (Number.isFinite(stored)) return clampPitch(stored);
  const counts = new Map();
  for (const name of laneNames || []) {
    if (name === "drums") continue;
    const p = record?.[name]?.pitch;
    if (!Number.isFinite(p)) continue;
    const k = clampPitch(p);
    counts.set(k, (counts.get(k) || 0) + 1);
  }
  let best = 0;
  let bestCount = 0;
  for (const [k, n] of counts) {
    if (n > bestCount || (n === bestCount && Math.abs(k) < Math.abs(best))) {
      best = k;
      bestCount = n;
    }
  }
  return best;
}
