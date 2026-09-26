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
// open back up as room returns. Opening back up takes REOPEN_SLACK to spare,
// though, not just a fit; see there for why (#633).

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

// Room a less collapsed level must have to spare before the strip goes back to
// it. Collapsing still happens the moment the row is short by any amount.
//
// Without this the decision has one threshold, and a strip sitting exactly on
// it flips on any disturbance smaller than a pixel: a fractional width at a 2.5
// device pixel ratio rounding the other way, or a scrollbar arriving and
// leaving. Each flip changes the strip's size, the observer fires, and it
// flips back, which is the footer "breathing" in #633. With two thresholds
// REOPEN_SLACK apart, whatever just collapsed a level has to give back more
// than this before it undoes it, so a wobble smaller than that settles on the
// first pass instead of cycling. 24px is wider than a classic Windows
// scrollbar at any scale (17px), the largest width change anything in the
// footer can make on its own.
const REOPEN_SLACK = 24;

let strip = null;
let level = 0; // count of LEVELS applied, in order; 0 is fully inline
let queued = false;
let observedWidth = -1; // strip content width the observer last reported

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
  // the panel is given a whole number of pixels, so asking for exactly the
  // deficit back can land a pixel short -- and a pixel short is not "nearly":
  // it drops the whole cluster to the popover, which is the difference between
  // the options being on screen and being behind a click.
  const want = Math.floor(inline - deficit) - WRAP_SLACK;
  const width = Math.max(want, Math.ceil(inline / 2));
  return width >= MIN_WRAP_PX && width < inline ? width : 0;
}

/**
 * How far the row runs past the strip's right edge: positive when it
 * overflows, negative by the room it has to spare.
 *
 * Not scrollWidth - clientWidth. That never goes below zero, so it cannot say
 * how much room there is, which is what REOPEN_SLACK needs to know. And both
 * are integers rounded from fractional widths, so at a 2.5 device pixel ratio
 * the same row could read as fitting on one pass and a pixel over on the next.
 * Rect edges are fractional and the same on every pass.
 *
 * Only meaningful with the clusters packed to the left: apply() holds them
 * there while it measures.
 */
function overrun() {
  const box = strip.getBoundingClientRect();
  let left = Infinity;
  let right = -Infinity;
  for (const el of strip.children) {
    const r = el.getBoundingClientRect();
    if (!r.width) continue;
    left = Math.min(left, r.left);
    right = Math.max(right, r.right);
  }
  // Edge to edge of the clusters rather than against the strip's own edges,
  // so a strip that has been scrolled sideways measures the same as one that
  // has not.
  return right > left ? right - left - box.width : -box.width;
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

  // The "click" level spreads the clusters with space-between, and a spread
  // row always spans the strip exactly: it would never show room to spare.
  // Held left-packed for the measurement only, and let go below.
  strip.style.justifyContent = "flex-start";

  // Positive means level n does not fit. A level less collapsed than the one
  // in force also has to leave REOPEN_SLACK spare (#633).
  const slack = (n) => (n < was ? REOPEN_SLACK : 0);
  const natural = overrun();
  let deficit = natural + slack(0);

  // Wrap: measured against the uncollapsed row, which is the only state where
  // the panel's inline width can be read. Asks for the slack this level needs,
  // and for at least a pixel: when the row fits outright but not with the
  // slack to reopen, wrapping is where it stays.
  if (deficit > 0) {
    const w = wrapWidthFor(Math.max(1, natural + slack(1)));
    if (w) {
      strip.style.setProperty("--metro-wrap-w", `${w}px`);
      strip.classList.add("collapse-wrap");
      level = 1;
      deficit = overrun() + slack(1);
    }
  }

  // Popover: replaces the wrap rather than stacking on it. The two are
  // different answers to the same problem, and a cluster cannot be both.
  if (deficit > 0) {
    strip.classList.remove("collapse-wrap");
    strip.style.removeProperty("--metro-wrap-w");
    strip.classList.add("collapse-click");
    level = 2;
    deficit = overrun() + slack(2);
  }

  if (deficit > 0) {
    strip.classList.add("collapse-tight");
    level = 3;
  }

  strip.style.removeProperty("justify-content");

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
  //
  // Width changes only. The strip's height is an output of the fit, not an
  // input: wrapping makes it taller, and a horizontal scrollbar coming or
  // going changes it too. Re-fitting on those is a loop from the fit back into
  // itself with nothing new to measure (#633). Content changes that do matter
  // call refitFooter() themselves, and still go straight through.
  if (typeof ResizeObserver === "function") {
    new ResizeObserver((entries) => {
      const width = entries[entries.length - 1].contentRect.width;
      if (width === observedWidth) return;
      observedWidth = width;
      refitFooter();
    }).observe(strip);
  } else {
    window.addEventListener("resize", refitFooter);
  }

  refitFooter();
}
