// Keeps the footer control strip inside the width it actually has.
//
// .footer-clusters is `overflow-x: auto` with `flex: none` children, so it
// never reflows. Past a certain width it silently scrolls instead, and controls
// the user needs sit off the right-hand edge with nothing to say so. Measured
// on a real track: the strip wants 1443px, so it already overflows by 88px at a
// maximized 1080p window with the sidebar open, and by 472px at the 1536px
// logical viewport a 4K panel gets at 250% Windows scaling (#586).
//
// Why this is measured in JS rather than written as a media query:
//
// The space available is not a function of the viewport. Collapsing the library
// sidebar moves it by 324px (390px -> 66px), which is enough to flip the answer
// on its own: at 1920 the strip overflows by 88px with the sidebar open and
// fits with room to spare when it is shut. A breakpoint keyed on window width
// would collapse a footer that had plenty of room, and miss one that did not.
// The click cluster also appears and disappears with the track's beat grid.
//
// The order below is cheapest first. Each level is re-measured from the
// uncollapsed state rather than added on top of the last one, so the strip can
// open back up as room returns and there is no hysteresis to tune.

// Least destructive first. "click" moves the click-track options into a
// popover behind their own disclosure, which is worth 706px of the 1443px
// strip, about half of it. "tight" only closes up the gaps, and exists for the
// narrow windows where losing the options still is not enough.
const LEVELS = ["click", "tight"];

let strip = null;
let level = 0;
let queued = false;

function apply() {
  queued = false;
  if (!strip || !strip.isConnected) return;

  // Always measure from the top. Deciding from the current, already-collapsed
  // state means the strip can only ever tighten, and never recovers when the
  // window grows or the sidebar opens.
  while (level > 0) strip.classList.remove(`collapse-${LEVELS[--level]}`);

  // A hidden strip has no width to fit anything into, and measuring one would
  // collapse every level for nothing.
  if (!strip.clientWidth) return;

  while (strip.scrollWidth > strip.clientWidth && level < LEVELS.length) {
    strip.classList.add(`collapse-${LEVELS[level++]}`);
  }
}

/**
 * Re-fit on the next frame.
 *
 * Coalesced because the things that call it arrive in bursts: a drag on the
 * window edge fires the observer per frame, and a language switch rewrites
 * every label in one go.
 */
export function refitFooter() {
  if (queued) return;
  queued = true;
  requestAnimationFrame(apply);
}

export function initFooterFit() {
  strip = document.querySelector(".footer-clusters");
  if (!strip) return;

  // Watches the strip itself, not the window, so a sidebar collapse or any
  // other layout change that moves the boundary is picked up without anything
  // having to remember to tell us.
  if (typeof ResizeObserver === "function") {
    new ResizeObserver(refitFooter).observe(strip);
  } else {
    window.addEventListener("resize", refitFooter);
  }

  refitFooter();
}
