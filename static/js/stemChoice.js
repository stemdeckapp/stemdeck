// The Extract row's visual state, in one place.
//
// Three controls describe the same fact, "which stems will be extracted": the
// six stem chips, the All button beside them, and the Combined / Lead +
// Backing toggle that is only meaningful while Vocals is among them. All
// three are painted from `selectedStems` and `vocalSplitMode`, and none of
// them stores anything of its own.
//
// They used to be refreshed by whoever happened to change the selection, and
// the paths did not agree. The All button was synced from a closure inside
// main.js's wireAllButton, reachable only from a click, so both paths that set
// the selection without one left it stale: the restore on load, which arrives
// asynchronously after the wiring has already synced once against the
// all-stems default, and catalog.js opening a library track, which set
// `aria-pressed` on each chip by hand and never touched All. Two stems
// selected, All lit, on every page load (#658).
//
// So the refresh lives here, outside both modules, and every path calls the
// same function. main.js imports catalog.js, so catalog.js cannot import back
// into main.js; a shared module is what lets the two of them agree without a
// cycle.
import { selectedStems, vocalSplitMode } from "./state.js";
import { STEM_NAMES } from "./constants.js";

/**
 * Point every control in the Extract row at the current selection.
 *
 * Safe to call before the DOM those controls live in exists: each lookup is
 * optional, so an early call is a no-op rather than a throw.
 */
export function refreshStemChoiceVisuals() {
  for (const btn of document.querySelectorAll(".stem-choice[data-stem]")) {
    btn.setAttribute("aria-pressed", String(selectedStems.has(btn.dataset.stem)));
  }

  // Derived, never stored: All is pressed when every stem is, and that is the
  // only thing that makes it true. Reading it from the selection each time is
  // what stops it drifting from the chips beside it.
  document
    .getElementById("stemAllBtn")
    ?.setAttribute("aria-pressed", String(selectedStems.size === STEM_NAMES.length));

  // Nothing selected is a real state the row can be left in, and the server
  // reads an empty list as "all of them" (app/api/jobs.py). Submitting from
  // here would therefore extract six stems the row says it is not extracting,
  // so the button is closed rather than the selection quietly rewritten.
  const submit = document.getElementById("submit");
  if (submit) submit.disabled = selectedStems.size === 0;

  // Combined / Lead + Backing carries the whole of the vocals decision, so
  // between them they have three states and not two: one of them lit, the
  // other lit, or neither. Neither is what "vocals are not being extracted"
  // looks like, and the chip beside them follows from the same fact above.
  //
  // The mode itself is remembered while they are both dark, so switching the
  // vocals back on returns to the way they were last asked for.
  const wrap = document.getElementById("vocalModeToggle");
  if (!wrap) return;
  const hasVocals = selectedStems.has("vocals");
  for (const btn of wrap.querySelectorAll(".vocal-mode-btn")) {
    btn.setAttribute(
      "aria-pressed",
      String(hasVocals && btn.dataset.mode === vocalSplitMode),
    );
  }
}
