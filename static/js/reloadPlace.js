// Reload the open song and keep the user's place: the one helper behind every
// reload that is not the user's own choice of song (a mode's stem choice, a
// mode switch that shows other lanes, Karaoke's lanes, Streamer's "I play").
// The rule itself is in reloadPlaceCore.js.

import { currentJobId, audioEngine, totalDuration, loopEnabled, loopStart, loopEnd } from "./state.js";
import { transport, setPlayheadTime, setLoopRange, togglePlayPause, resumePlayback } from "./transport.js";
import { requestTakesReload } from "./capture/takeLanes.js";
import { newRestore, restoreStep } from "./reloadPlaceCore.js";

// How long a restore may wait for the new engine before giving up.
const RESTORE_WINDOW_MS = 20000;
const TICK_MS = 100;

let pending = null;
let timer = 0;
// Engines whose ready has resolved.
const readyEngines = new WeakSet();

function now() {
  const v = transport()?.getCurrentTime?.();
  return Number.isFinite(v) ? v : 0;
}

function isPlaying() {
  return Boolean(transport()?.isPlaying?.());
}

// Anything the user does while a restore waits ends it (reloadPlaceCore.js):
// their seek, click or key press is never undone. Only once the new engine
// exists: before that, what they do acts on the old one, which is going.
function onUserInput(e) {
  if (!pending || !audioEngine || audioEngine === pending.engine) return;
  if (e.type === "keydown" && ["Shift", "Control", "Alt", "Meta"].includes(e.key)) return;
  pending.state.userActed = true;
}

function stop() {
  pending = null;
  clearInterval(timer);
  timer = 0;
  document.removeEventListener("pointerdown", onUserInput, true);
  document.removeEventListener("keydown", onUserInput, true);
}

/**
 * Reload `jobId`'s lanes (catalog.js answers the takes reload) and put the
 * place back once the new engine is up. Playback stops at once, so what is
 * heard stops where it will resume. A second reload while one is waiting (a
 * mode switch that Streamer follows with its own) keeps the place the first
 * one saved.
 */
export function reloadKeepingPlace(jobId) {
  if (!jobId) return;
  if (!(pending && pending.job === jobId)) {
    const playing = isPlaying();
    const at = {
      t: now(),
      playing,
      loop: loopEnabled && loopEnd > loopStart ? { start: loopStart, end: loopEnd } : null,
    };
    if (playing) togglePlayPause();
    pending = { job: jobId, state: newRestore(at) };
  } else {
    // Wait for the engine this reload brings up, not the last one.
    pending.state = newRestore(pending.state);
  }
  pending.engine = audioEngine;
  pending.until = performance.now() + RESTORE_WINDOW_MS;
  requestTakesReload(jobId);
  clearInterval(timer);
  timer = setInterval(tick, TICK_MS);
  document.addEventListener("pointerdown", onUserInput, true);
  document.addEventListener("keydown", onUserInput, true);
}

/** Whether a restore is waiting (tests and the hunt scripts read it). */
export function restorePending() {
  return Boolean(pending);
}

function tick() {
  const p = pending;
  if (!p || currentJobId !== p.job || performance.now() > p.until) {
    stop();
    return;
  }
  const eng = audioEngine;
  // Up means ready: a seek before the engine's ready resolves does not hold
  // (it comes up at 0), so a Play pressed then started from the top.
  if (eng && eng !== p.engine && !readyEngines.has(eng) && !p.waiting?.has(eng)) {
    (p.waiting ??= new WeakSet()).add(eng);
    Promise.resolve(eng.ready).then((ok) => { if (ok !== false) readyEngines.add(eng); }).catch(() => {});
  }
  const engineUp = Boolean(eng && eng !== p.engine && readyEngines.has(eng) && totalDuration > 0 && transport());
  const actions = restoreStep(p.state, {
    nowMs: performance.now(),
    engineUp,
    time: engineUp ? now() : 0,
    playing: engineUp && isPlaying(),
    loopOn: loopEnabled,
  });
  for (const a of actions) {
    if (a === "loop") setLoopRange(p.state.loop.start, p.state.loop.end);
    else if (a === "seek") setPlayheadTime(p.state.t);
    else if (a === "play") resumePlayback();
  }
  if (p.state.done) stop();
}
