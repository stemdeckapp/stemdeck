// The Extract row's visual state, in one place.
//
// Three controls describe the same fact, "which stems will be extracted": the
// six stem chips, the All button beside them, and the Lead + Backing toggle
// that is only meaningful while Vocals is among them. All three are derived
// from `selectedStems` and none of them stores anything of its own.
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
import { selectedStems } from "./state.js";
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

  // Lead + Backing has nothing to act on without vocals, so it is hidden
  // rather than left implying a choice. Same staleness applied here: a
  // restored selection without vocals used to leave it on screen.
  document
    .getElementById("vocalModeToggle")
    ?.classList.toggle("hidden", !selectedStems.has("vocals"));
}
