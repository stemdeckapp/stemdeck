// Which saved mix of the open song is in force.
//
// Every mode keeps its own record of a song (mixer.js: mixKey): what is muted,
// soloed and how loud, each lane's key, the EQ and the master, and the speed
// and global key the song was left at. This module watches the mode (Practice
// or Advanced, mode.js), Karaoke Mode (karaoke/karaoke.js, announced by
// "stemdeck:karaoke-changed") and Streamer Mode (streamer/streamer.js,
// "stemdeck:streamer-changed"), and when the one in force changes it has the
// mixer swap the open song's record, then pushes the new one to wherever it
// is heard and shown: the lanes' gains, their keys, the EQ panels and the
// master (which follows the EQ's re-apply), the speed and the key counter.
//
// Announces "stemdeck:mix-switched" ({ id, mode }) when it replaced the mix.
// A song that loads later reads the record of the mode in force by itself.

import { getMode, onModeChange } from "./mode.js";
import { switchMixMode, reloadOpenMix, refreshMixerVisuals, applyMix, applyAllLanePitches } from "./mixer.js";
import { applyAllLaneEq } from "./eqEngine.js";
import { restoreGlobals } from "./transport.js";
import { currentJobId } from "./state.js";

let karaokeOpen = false;
let streamerOpen = false;
// One switch at a time, in the order asked: the mode can change twice in a
// moment (Karaoke opened, then closed), and each switch reads and writes the
// store. The two views are never open together; Karaoke wins if they are.
let chain = Promise.resolve();

const wanted = () => (karaokeOpen ? "karaoke" : streamerOpen ? "streamer" : getMode());

/** The mode whose saved mix and stem layout are in force for the open song:
 * Karaoke or Streamer while open, else Practice or Advanced ("daw"). */
export function mixModeInForce() {
  return wanted();
}

// Called with the mode after each switch settles (modeStems.js reloads the
// song when the new mode shows other lanes). After the mix switch, so the
// reload reads the new mode's record.
const settledListeners = new Set();
export function onMixModeSettled(cb) {
  settledListeners.add(cb);
  return () => settledListeners.delete(cb);
}

// Switches asked for and not finished: while any is, the record in force is
// about to change, and a mute or solo made now could land in the wrong one.
let pending = 0;

/** True while a switch of the saved mix is queued or running. */
export function isMixSwitching() {
  return pending > 0;
}

// Queue `step(mode)`, which resolves whether it replaced the open song's mix
// in memory; a replaced mix is then pushed out, unless the mode moved on.
function enqueue(step) {
  pending += 1;
  chain = chain.then(async () => {
    const mode = wanted();
    if (await step(mode) && mode === wanted()) pushMix(mode);
  }).finally(() => { pending -= 1; });
  return chain;
}

export function syncMixMode() {
  return enqueue(async (mode) => {
    let replaced = false;
    try {
      replaced = await switchMixMode(mode);
    } catch (err) {
      console.warn("[mixMode] could not switch the saved mix:", err);
    }
    if (mode === wanted()) {
      for (const cb of settledListeners) {
        try { cb(mode); } catch (err) { console.warn("[mixMode] settled listener failed:", err); }
      }
    }
    return replaced;
  });
}

/** The open song's version changed with its lanes kept (modeStems.js): its
 * record in the mode in force replaces the mix, in the same queue. */
export function syncVersionMix() {
  return enqueue(() => reloadOpenMix().catch((err) => {
    console.warn("[mixMode] could not read the version's saved mix:", err);
    return false;
  }));
}

function pushMix(mode) {
  refreshMixerVisuals();
  applyMix();
  applyAllLanePitches();
  applyAllLaneEq();
  restoreGlobals();
  document.dispatchEvent(new CustomEvent("stemdeck:mix-switched", { detail: { id: currentJobId, mode } }));
}

export function initMixMode() {
  onModeChange(() => { syncMixMode(); });
  document.addEventListener("stemdeck:karaoke-changed", (e) => {
    karaokeOpen = Boolean(e.detail?.open);
    syncMixMode();
  });
  document.addEventListener("stemdeck:streamer-changed", (e) => {
    streamerOpen = Boolean(e.detail?.open);
    syncMixMode();
  });
}
