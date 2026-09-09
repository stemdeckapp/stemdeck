// A self-rescheduling callback that keeps running while the tab is hidden.
//
// requestAnimationFrame is not delivered to a hidden tab at all. That is right
// for drawing and wrong for everything the audio depends on:
//
//   - chunkedAudioEngine calls _maybeSchedule() from its tick, and that is the
//     only call site. When the tick stops, chunk scheduling stops, playback
//     runs out the LOOKAHEAD_SEC already queued, and then it is silent. No
//     error, no event, just twelve seconds and then nothing (#600).
//   - audioEngine keeps sounding, because every source is scheduled up front,
//     but it stops noticing loop ends and end-of-track until you come back.
//
// While hidden the same callback runs from setTimeout instead. Browsers clamp
// background timers to roughly 1 Hz, which is far below frame rate and far
// above what either scheduler needs against a twelve second lookahead.
//
// This lives in its own module because both engines need it and because the
// cancel path is easy to get wrong: the handle is a raf id or a timeout id
// depending on which clock scheduled it, and passing one to the other's cancel
// silently does nothing.

// Long enough to be well inside any lookahead, short enough that the clamp the
// browser actually applies is what governs the rate, not this number.
const HIDDEN_INTERVAL_MS = 100;

// Every live loop, so that one document listener serves all of them. A
// listener per loop would mean a listener per engine, and engines are created
// and destroyed on every track change; document outlives all of them and would
// hold each dead engine's closure, and its decoded buffers, forever.
const live = new Set();
let listening = false;

function onVisibilityChange() {
  // Only the hide transition needs help. On the way back the pending timeout
  // fires normally and schedule() picks requestAnimationFrame again by itself.
  if (!document.hidden) return;
  for (const loop of live) loop._onHidden();
}

function ensureListening() {
  if (listening || typeof document === "undefined") return;
  document.addEventListener("visibilitychange", onVisibilityChange);
  listening = true;
}

// Resolved per call rather than at module load: these are read from the global
// at the moment they are used, so a test can install fakes after importing.
const isHidden = () => typeof document !== "undefined" && document.hidden === true;
const raf = (cb) =>
  typeof requestAnimationFrame === "function"
    ? requestAnimationFrame(cb)
    : setTimeout(cb, HIDDEN_INTERVAL_MS);
const cancelRaf = (id) => {
  if (typeof cancelAnimationFrame === "function") cancelAnimationFrame(id);
  else clearTimeout(id);
};

/**
 * @param {() => void} fn Called once per tick. It is expected to call
 *   `schedule()` again itself if it wants another tick, matching the shape of
 *   a plain requestAnimationFrame loop.
 */
export function createTickLoop(fn) {
  let handle = null;
  let timed = false; // whether `handle` came from setTimeout rather than raf

  function schedule() {
    cancel();
    if (isHidden()) {
      timed = true;
      handle = setTimeout(fn, HIDDEN_INTERVAL_MS);
    } else {
      timed = false;
      handle = raf(fn);
    }
  }

  function cancel() {
    if (handle === null) return;
    if (timed) clearTimeout(handle);
    else cancelRaf(handle);
    handle = null;
  }

  const loop = {
    schedule,
    cancel,

    // Stop the loop and drop it from `live`. Callers must do this on teardown:
    // a loop left in the set keeps its whole closure reachable, which for an
    // audio engine means every decoded stem buffer it captured.
    dispose() {
      cancel();
      live.delete(loop);
    },

    // The browser drops a pending animation frame when the tab hides rather
    // than deferring it, so a loop waiting on one has no callback left to
    // notice it should change clocks. This is that missing callback.
    //
    // Only a loop actually waiting on a frame is restarted. A stopped loop
    // (handle null, after pause or dispose) stays stopped, and one already on
    // a timeout is left alone.
    _onHidden() {
      if (handle !== null && !timed) schedule();
    },
  };

  live.add(loop);
  ensureListening();
  return loop;
}

// Test seam. Not used by the app.
export function _liveLoopCount() {
  return live.size;
}
