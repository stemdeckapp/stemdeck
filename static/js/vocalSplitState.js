// Lead + Backing: whether this install can make it, and the words for why a
// split did not (#832).
//
// A split that failed used to leave one Vocals stem and say nothing, so the
// person who chose Lead + Backing could not tell it apart from a song with no
// backing vocals. The server now gives each failure a code
// (app/core/vocal_split_outcome.py), and /api/health says when a missing
// dependency rules the split out on this install. This module owns both
// answers; job.js, stemChoice.js, stemsControl.js and karaoke.js only read them.

import { t } from "./i18n.js";
import { vocalSplitMode } from "./state.js";

/** The codes the server sends, each with its sentence (split.reason.*). */
export const SPLIT_REASONS = Object.freeze([
  "dependency_missing",
  "model_unavailable",
  "out_of_memory",
  "worker_failed",
  "timeout",
  "cancelled",
  "interrupted",
]);
const KNOWN = new Set(SPLIT_REASONS);

/** A code the server sent, or "worker_failed" for anything else. */
export function splitReason(code) {
  return KNOWN.has(code) ? code : "worker_failed";
}

/** One line on why the split did not work and what to do about it. */
export function splitReasonText(code) {
  return t(`split.reason.${splitReason(code)}`);
}

/** What the Stems popover, Karaoke and the Extract button say when the split
 * of an open song did not work: that, why, and that Vocals stays one lane. */
export function splitFailedText(code) {
  return t("stems.splitFailedWhy", { reason: splitReasonText(code) });
}

// The reason Lead + Backing cannot be made here, or null when it can (or the
// answer is not in yet, which is read as "can": the split is tried as before).
let unavailable = null;
const listeners = new Set();

function setUnavailable(reason) {
  if (reason === unavailable) return;
  unavailable = reason;
  for (const fn of listeners) {
    try {
      fn(unavailable);
    } catch (err) {
      console.warn("[vocalSplit] an availability listener failed:", err);
    }
  }
}

/** The reason code when Lead + Backing cannot be made here, else null. */
export function vocalSplitUnavailable() {
  return unavailable;
}

/** Called with the new reason (or null) whenever availability changes. */
export function onVocalSplitAvailability(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** Read /api/health's vocal_split field: { available, reason }. */
export function applyVocalSplitHealth(health) {
  const v = health?.vocal_split;
  setUnavailable(v && v.available === false ? splitReason(v.reason) : null);
}

/** What a split just did on this install: a missing dependency rules it out
 * until the server says otherwise, a success rules it back in. Any other
 * failure belongs to that one run and changes nothing. */
export function noteSplitOutcome(code) {
  if (code === null) setUnavailable(null);
  else if (code === "dependency_missing") setUnavailable(code);
}

/** The Vocals mode an import will actually ask for: "all" while Lead +
 * Backing cannot be made, whatever was last chosen. The choice itself is
 * kept, so it comes back once the split is available again. */
export function effectiveVocalSplitMode() {
  return unavailable ? "all" : vocalSplitMode;
}
