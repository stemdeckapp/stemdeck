// A lane's level as a number, kept apart from the page so it can be tested in
// Node: the linear gain the Studio fader stores (mixerState[lane].volume, 0 to
// LANE_VOLUME_MAX, 1 is 0 dB), its dB text, and the steps a keyboard takes.
//
// Performance's lane volumes (streamer.js) and its Monitor level
// (streamerFx.js) use the same scale as the Studio fader: the slider is linear
// in gain, as the fader's range input is, so the middle of a lane slider is
// 0 dB and its right end +6 dB.

import { LANE_VOLUME_MAX } from "./constants.js";

export { LANE_VOLUME_MAX };

// One arrow key press, and one with Shift or a Page key: the Studio fader's
// wheel steps (mixer.js makeVolumeKnob).
export const LEVEL_STEP = 0.04;
export const LEVEL_BIG_STEP = 0.2;

// The live input heard through Hear myself (captureGraph.setMonitor's gain).
// It starts at, and never goes past, the level Hear myself always played at
// (unity): a monitor that can be pushed louder next to a microphone and
// speakers feeds back sooner. The input's Trim, before the pedals, is where a
// quiet input is raised.
export const MONITOR_LEVEL_MAX = 1;
export const MONITOR_LEVEL_DEFAULT = 1;

/** `v` as a level between 0 and `max`; anything that is not a number is `fallback`. */
export function clampLevel(v, max = LANE_VOLUME_MAX, fallback = 1) {
  const n = typeof v === "number" ? v : Number.NaN;
  if (!Number.isFinite(n)) return Math.max(0, Math.min(max, fallback));
  return Math.max(0, Math.min(max, n));
}

/** A level as the Studio readout shows it: "0.0", "+3.5", "-6.0" or "-∞" (dB). */
export function levelText(v) {
  if (!(v > 0)) return "-∞";
  const db = (20 * Math.log10(v)).toFixed(1);
  // "-0.0" from a level a hair under unity reads as a different number.
  if (db === "-0.0") return "0.0";
  return `${parseFloat(db) > 0 ? "+" : ""}${db}`;
}

/** Where `v` sits on a slider of `max`: 0 at the left end, 1 at the right. */
export function levelFraction(v, max = LANE_VOLUME_MAX) {
  return max > 0 ? clampLevel(v, max) / max : 0;
}

/** The level at fraction `f` of a slider of `max`, on the fader's 0.01 grid. */
export function levelAt(f, max = LANE_VOLUME_MAX) {
  const x = Math.max(0, Math.min(1, Number(f) || 0));
  return Math.round(x * max * 100) / 100;
}

/**
 * The level a key moves `v` to on a slider of `max`, or null for a key the
 * slider does not take. Arrows step LEVEL_STEP (LEVEL_BIG_STEP with Shift),
 * Page Up and Page Down LEVEL_BIG_STEP, Home is silence, End the top.
 */
export function levelKeyStep(v, key, { shift = false, max = LANE_VOLUME_MAX } = {}) {
  const step = shift ? LEVEL_BIG_STEP : LEVEL_STEP;
  const cur = clampLevel(v, max);
  let next;
  switch (key) {
    case "ArrowRight": case "ArrowUp": next = cur + step; break;
    case "ArrowLeft": case "ArrowDown": next = cur - step; break;
    case "PageUp": next = cur + LEVEL_BIG_STEP; break;
    case "PageDown": next = cur - LEVEL_BIG_STEP; break;
    case "Home": return 0;
    case "End": return max;
    default: return null;
  }
  return clampLevel(Math.round(next * 100) / 100, max);
}

/**
 * The one level a group of lanes plays at (Performance's Backing track: every
 * lane heard but the take). They are set together, so they agree; a lane that
 * does not (one that joined later, a record from before) shows the loudest,
 * and the next change sets them all. None: 0 dB.
 */
export function groupLevel(volumes) {
  const list = (volumes || []).filter((v) => typeof v === "number" && Number.isFinite(v));
  if (!list.length) return 1;
  return clampLevel(Math.max(...list));
}

/** A stored Monitor level made safe: 0 to MONITOR_LEVEL_MAX, MONITOR_LEVEL_DEFAULT when missing or wrong. */
export function monitorLevelOf(v) {
  return clampLevel(v, MONITOR_LEVEL_MAX, MONITOR_LEVEL_DEFAULT);
}
