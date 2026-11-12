// Performance and the library sidebar (streamer.js). Beside an open library
// a window up to about 1140 px wide leaves Performance too narrow for its two
// windows side by side, so they stacked and pushed the Timeline / Effects card
// below the fold. While Performance is open in such a window the library
// folds to the rail (reviewModel.foldsLibrary), through the same state the
// rail's toggle uses (catalog.setSidebarCollapsed), but without saving it:
// the person's own choice stays theirs.
//
// - It folds only a library that is open, and only for Performance.
// - Leaving Performance opens it again (and so does a window made wide
//   enough while Performance is open).
// - A person who opens or folds the library by hand while Performance is
//   open has the last word: nothing is folded or opened for them again until
//   Performance opens the next time.

import { setSidebarCollapsed, isSidebarCollapsed } from "../catalog.js";
import { foldsLibrary } from "./reviewModel.js";

let active = false;
// The library is folded by Performance, and nobody has touched it since.
let folded = false;
// The library was opened or folded by hand while Performance is open.
let byHand = false;

function onCollapse(e) {
  if (!active || !e.detail?.byHand) return;
  byHand = true;
  folded = false;
}

/** Performance opened: from now on the library follows its room. */
export function libraryFoldStart() {
  if (active) return;
  active = true;
  folded = false;
  byHand = false;
  document.addEventListener("stemdeck:library-collapse", onCollapse);
}

/** Performance closed: a library it folded opens again. */
export function libraryFoldEnd() {
  if (!active) return;
  document.removeEventListener("stemdeck:library-collapse", onCollapse);
  active = false;
  if (folded) setSidebarCollapsed(false, { byHand: false });
  folded = false;
  byHand = false;
}

/**
 * Fold or open the library for Performance's layer, `layerW` CSS pixels
 * wide now, with a centre column `padX` of padding across. Called whenever
 * the layer is placed (open, a resize, the sidebar sliding).
 */
export function fitLibrary(layerW, padX) {
  if (!active || byHand) return;
  const app = document.querySelector(".app");
  const side = document.getElementById("catalogPanel");
  if (!app || !side) return;
  const sidebarOpenW = parseFloat(getComputedStyle(app).getPropertyValue("--sidebar-w"));
  const want = foldsLibrary({ layerW, sidebarW: side.getBoundingClientRect().width, sidebarOpenW, padX });
  if (want && !isSidebarCollapsed()) {
    folded = true;
    setSidebarCollapsed(true, { byHand: false });
  } else if (!want && folded) {
    folded = false;
    setSidebarCollapsed(false, { byHand: false });
  }
}

/** For the browser tests: what Performance has done to the library. */
export function libraryFoldState() {
  return { active, folded, byHand, collapsed: isSidebarCollapsed() };
}
