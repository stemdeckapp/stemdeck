// The short dip a lane takes around a change of its wiring while it plays: a
// key change (its pitch bus: audioEngine.routeTrack,
// chunkedAudioEngine.routeStem) and an effects chain put in, taken out or
// re-timed (fx/chainHost.js). DIP_FADE down, DIP_HOLD at silence with the
// rewire in its middle, DIP_FADE back up: 32 ms, so the rewire, which
// connect() cannot place on a render quantum, never clicks.
//
// The dip has a gain node of its own, `member.dip`, made by
// fxChain.attachSource in series after the lane gain (lane gain -> dip -> EQ
// slot -> analyser). It used to be scheduled on the lane gain's AudioParam and
// end in a ramp back to the level of that moment, so a volume, mute or Sound
// change made inside those 32 ms (a setTargetAtTime on the same param) was
// overwritten by the ramp already scheduled after it. Now the lane gain only
// ever takes the user's level and the dip only ever goes 1, 0, 1: the two
// multiply, and neither can undo the other. tests/js/lane-dip.test.mjs.

/** Seconds from full to silence, and back. */
export const DIP_FADE = 0.006;
/** Seconds held at silence; the rewire runs in the middle of it. */
export const DIP_HOLD = 0.02;

/** A member's dip node: a gain at 1, outside every dip. */
export function createDip(ctx) {
  const dip = ctx.createGain();
  dip.gain.value = 1;
  return dip;
}

/**
 * Dip `param` (a dip node's gain) from context time `at`: down to 0, held,
 * back to 1. A dip started inside another starts from where that one is
 * (its value is read before the rest of it is cancelled). Returns the
 * seconds from `at` to the middle of the silence, when the rewire is to run.
 */
export function scheduleDip(param, at) {
  const now = param.value;
  param.cancelScheduledValues(at);
  param.setValueAtTime(now, at);
  param.linearRampToValueAtTime(0, at + DIP_FADE);
  param.setValueAtTime(0, at + DIP_FADE + DIP_HOLD);
  param.linearRampToValueAtTime(1, at + DIP_FADE + DIP_HOLD + DIP_FADE);
  return DIP_FADE + DIP_HOLD / 2;
}
