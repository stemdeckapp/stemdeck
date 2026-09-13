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

// Least destructive first, and each step costs more than the last.
//
// "wrap" keeps every click-track option on screen and lets the panel wrap,
// at a width measured from the deficit rather than a constant: give back
// exactly the pixels the row is short and no more. A fixed width here was the
// whole trouble with the version before this one -- 190px turned six controls
// into a five-row column, and the popover that replaced it freed 580px when
// the row was 175px short, so the slack pooled as a hole before Export Mix.
// Floored at half the panel's inline width so this stays a two-row wrap.
//
// "click" is that popover, opened from a disclosure beside the on/off pill,
// now only for windows where two rows are still too
// wide. #269 wanted the options visible without a click; they still are at
// every width that can hold them, which after "wrap" is most of them.
//
// "tight" only closes up the spacing between clusters, for the windows where
// even the popover leaves the row overflowing.
const LEVELS = ["wrap", "click", "tight"];

// Below this the panel is too narrow to hold the widest control in it with
// anything beside it, and wrapping stops being a two-row layout.
const MIN_WRAP_PX = 240;

// Rounding headroom on the width asked for; see wrapWidthFor.
const WRAP_SLACK = 4;

let strip = null;
let level = 0; // count of LEVELS applied, in order; 0 is fully inline
let queued = false;

/**
 * Panel width that gives the row back `deficit` pixels, or 0 if wrapping
 * cannot cover it.
 *
 * The panel is `display: contents` while inline, so it has no box of its own
 * to measure: its width is the span its promoted children occupy in the
 * cluster's body. Wrapping it to W shrinks the cluster by (that span - W), so
 * W is just the span less what the row is short.
 */
function wrapWidthFor(deficit) {
  const panel = document.getElementById("t-metro-panel");
  if (!panel || panel.classList.contains("hidden")) return 0;

  // First child's left edge to the last one's right edge, rather than the
  // cluster's width less the controls that are not the panel's. Those controls
  // are not fixed -- the on/off pill has a volume button beside it now -- and
  // a subtraction that has to be kept in step with the markup is a sum that
  // will one day be wrong without saying so.
  const kids = [...panel.children].filter((el) => el.getBoundingClientRect().width > 0);
  if (!kids.length) return 0;
  const inline = kids[kids.length - 1].getBoundingClientRect().right - kids[0].getBoundingClientRect().left;
  if (inline <= 0) return 0;

  // Never below half: past that the wrap is a column, not two rows, and the
  // popover is the better answer for that window.
  //
  // WRAP_SLACK is not a fudge for a wrong sum. Widths here are fractional and
  // scrollWidth is a rounded integer, so asking for exactly the deficit back
  // can land a pixel short -- and a pixel short is not "nearly": it drops the
  // whole cluster to the popover, which is the difference between the options
  // being on screen and being behind a click.
  const want = Math.floor(inline - deficit) - WRAP_SLACK;
  const width = Math.max(want, Math.ceil(inline / 2));
  return width >= MIN_WRAP_PX && width < inline ? width : 0;
}

function apply() {
  queued = false;
  if (!strip || !strip.isConnected) return;

  const was = level;

  // Always measure from the top. Deciding from the current, already-collapsed
  // state means the strip can only ever tighten, and never recovers when the
  // window grows or the sidebar opens.
  while (level > 0) strip.classList.remove(`collapse-${LEVELS[--level]}`);
  strip.style.removeProperty("--metro-wrap-w");

  // A hidden strip has no width to fit anything into, and measuring one would
  // collapse every level for nothing.
  if (!strip.clientWidth) return;

  let deficit = strip.scrollWidth - strip.clientWidth;

  // Wrap: measured against the uncollapsed row, which is the only state where
  // the panel's inline width can be read.
  if (deficit > 0) {
    const w = wrapWidthFor(deficit);
    if (w) {
      strip.style.setProperty("--metro-wrap-w", `${w}px`);
      strip.classList.add("collapse-wrap");
      level = 1;
      deficit = strip.scrollWidth - strip.clientWidth;
    }
  }

  // Popover: replaces the wrap rather than stacking on it. The two are
  // different answers to the same problem, and a cluster cannot be both.
  if (deficit > 0) {
    strip.classList.remove("collapse-wrap");
    strip.style.removeProperty("--metro-wrap-w");
    strip.classList.add("collapse-click");
    level = 2;
    deficit = strip.scrollWidth - strip.clientWidth;
  }

  if (deficit > 0) {
    strip.classList.add("collapse-tight");
    level = 3;
  }

  // "click" is not only a layout change: it is what turns the click-track
  // options into a popover, and a popover left open across the change would be
  // stranded at coordinates that no longer describe its trigger. Announced
  // rather than called directly so this stays a measuring module -- it has no
  // business knowing which cluster it just reshaped.
  if (level !== was) {
    window.dispatchEvent(new CustomEvent("footerfit", { detail: { level: LEVELS[level - 1] ?? null } }));
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
