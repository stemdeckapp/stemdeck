// The rule for putting the place back after the open song reloads (a stem
// choice, a mode switch, Streamer's "I play"). reloadPlace.js runs it on a
// timer against the real transport; this file has no DOM and no imports, so
// tests/js can drive it step by step.
//
// The place is what the user had when they made the change: the playhead,
// whether it was playing, and the loop. It used to keep the first and the
// last and drop the play state, so a change while playing stopped playback;
// and it went on forcing the playhead back to the saved time for a second
// and a half after it held, so Play, a seek or the loop turned off right
// after the reload was undone every ~0.35 s.
//
// Now: as soon as the new engine is up, the loop comes back and the playhead
// is set; once the engine has settled and the playhead has held, playback
// resumes if it was playing and the restore is over. Anything the user does
// first ends it: a Play starts from the place, and a seek or loop change of
// theirs is never touched.

/** How long the playhead must stay put after the engine is ready. */
export const SETTLE_MS = 300;
/** A playhead this close to the saved time has held. */
export const HELD_SEC = 0.25;
/** Seeks tried before giving up on an engine that will not move. */
export const MAX_SEEKS = 5;

/**
 * A place to restore.
 * @param {{ t: number, playing: boolean, loop: {start:number,end:number}|null }} at
 */
export function newRestore(at) {
  return {
    t: Math.max(0, Number(at?.t) || 0),
    playing: Boolean(at?.playing),
    loop: at?.loop ? { start: at.loop.start, end: at.loop.end } : null,
    readyAt: 0,
    seeks: 0,
    loopDone: false,
    userActed: false,
    done: false,
  };
}

/**
 * One step. `s` is what the transport shows now:
 *   { nowMs, engineUp (the new engine is ready), time, playing, loopOn }
 * Returns the actions to take, in order: "loop", "seek", "play"; and marks
 * `p.done` when the restore is over.
 *
 * The loop and the playhead are put back the moment the new engine is up, so
 * a Play pressed at any point after that starts from the place. Playback
 * resumes only once the engine has settled and the seek has held.
 */
export function restoreStep(p, s) {
  const actions = [];
  if (p.done || !s.engineUp) return actions;
  const away = p.t > 0 && Math.abs(s.time - p.t) >= HELD_SEC;
  if (!p.readyAt) {
    p.readyAt = s.nowMs;
    if (p.loop) {
      p.loopDone = true;
      if (!s.loopOn) actions.push("loop");
    }
    if (away) {
      p.seeks += 1;
      actions.push("seek");
    }
    return actions;
  }
  // The engine comes up paused, so playing means the user pressed Play,
  // and a seek, a click on the timeline or a loop change is theirs too:
  // none of it is undone.
  if (s.playing || p.userActed) {
    p.done = true;
    return actions;
  }
  // Put the playhead back if something moved it since, until it holds.
  if (away) {
    if (p.seeks >= MAX_SEEKS) {
      p.done = true;
      return actions;
    }
    p.seeks += 1;
    actions.push("seek");
    return actions;
  }
  if (s.nowMs - p.readyAt < SETTLE_MS) return actions;
  // Held: playing again if it was, and over.
  if (p.playing) actions.push("play");
  p.done = true;
  return actions;
}
