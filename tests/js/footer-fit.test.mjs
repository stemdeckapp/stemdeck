// The footer's fit-to-width decision, against a model of the strip's layout.
//
// #633: on a maximized 4K window at 250% Windows scaling the footer controls
// "breathed" while a track played, every gap between clusters growing and
// shrinking by the same share and Export Mix, outside the strip, standing
// still. That is the strip's own width going up and down with space-between
// spreading the difference, and footerFit re-measuring every time it did.
//
// A decision with a single threshold cannot settle there: any wobble across it
// flips the level, the flip resizes the strip, and the observer fires again.
// These checks pin the two thresholds that replaced it, so a strip sitting on
// the boundary collapses once and stays put, and still opens back up when the
// room really returns.
//
// The layout is a model, not a browser. What it has to get right is only what
// footerFit reads: where each cluster's edges fall at each level. The widths
// are close to a real track's but the exact values do not matter.

const GAP = 16;
const TIGHT_GAP = 8;
// Transport, divider, Position, divider, Speed, divider, Global key, divider.
const FIXED = [90, 1, 330, 1, 100, 1, 120, 1];
// Click track: on/off pill, gap, volume pill, gap, then whatever holds the
// options -- their inline span, the measured wrap width, or the 28px
// disclosure that opens the popover.
const CLICK_HEAD = 34 + 8 + 34 + 8;
const PANEL_INLINE = 450;
const MORE_BTN = 28;

const sum = (a) => a.reduce((s, v) => s + v, 0);
const NATURAL = sum(FIXED) + CLICK_HEAD + PANEL_INLINE + GAP * FIXED.length;
const CLICK_W = sum(FIXED) + CLICK_HEAD + MORE_BTN + GAP * FIXED.length;
const TIGHT_W = sum(FIXED) + CLICK_HEAD + MORE_BTN + TIGHT_GAP * FIXED.length;

// ── A strip that lays itself out from its classes ─────────────────────────────

let width = 2000; // the strip's own width, fractional like the real one

const classes = new Set();
const props = new Map();
const classList = {
  add: (c) => classes.add(c),
  remove: (c) => classes.delete(c),
  contains: (c) => classes.has(c),
};
const style = {
  setProperty: (k, v) => props.set(k, String(v)),
  removeProperty: (k) => props.delete(k),
  get justifyContent() { return props.get('justify-content') ?? ''; },
  set justifyContent(v) {
    if (v) props.set('justify-content', v);
    else props.delete('justify-content');
  },
};

function layout() {
  const click = classes.has('collapse-click');
  const wrap = !click && classes.has('collapse-wrap');
  const gap = classes.has('collapse-tight') ? TIGHT_GAP : GAP;
  const wrapW = parseFloat(props.get('--metro-wrap-w')) || 380;
  const widths = [...FIXED, CLICK_HEAD + (click ? MORE_BTN : wrap ? wrapW : PANEL_INLINE)];
  const total = sum(widths) + gap * (widths.length - 1);
  // space-between at the "click" level, unless something holds the row packed
  // left. An overflowing row falls back to flex-start on its own.
  let step = gap;
  if (click && style.justifyContent !== 'flex-start' && total < width) {
    step = gap + (width - total) / (widths.length - 1);
  }
  let x = 0;
  const rects = widths.map((w) => {
    const r = { left: x, right: x + w, width: w };
    x += w + step;
    return r;
  });
  return { rects, total, clickLeft: rects[rects.length - 1].left, inline: !click && !wrap };
}

const children = FIXED.concat([0]).map((_, i) => ({
  getBoundingClientRect: () => layout().rects[i],
}));

// Integers, as the real ones are: these are what the version before #633 read.
const strip = {
  isConnected: true,
  classList,
  style,
  children,
  get clientWidth() { return Math.round(width); },
  get scrollWidth() { return Math.max(Math.round(width), Math.round(layout().total)); },
  getBoundingClientRect: () => ({ left: 0, right: width, width }),
};

// The options panel, for the wrap width. Its two outermost controls span
// PANEL_INLINE while the options sit inline.
const panelKid = (from, to) => ({
  getBoundingClientRect() {
    const l = layout();
    if (!l.inline) return { left: 0, right: 0, width: 0 };
    const left = l.clickLeft + CLICK_HEAD + from;
    return { left, right: left + (to - from), width: to - from };
  },
});
const panel = {
  classList: { contains: () => false },
  children: [panelKid(0, 120), panelKid(PANEL_INLINE - 90, PANEL_INLINE)],
};

globalThis.document = {
  querySelector: (sel) => (sel === '.footer-clusters' ? strip : null),
  getElementById: (id) => (id === 't-metro-panel' ? panel : null),
};

const events = [];
globalThis.window = {
  dispatchEvent: (e) => events.push(e.detail.level),
  addEventListener: () => {},
};
if (typeof globalThis.CustomEvent !== 'function') {
  globalThis.CustomEvent = class { constructor(type, init) { this.type = type; this.detail = init?.detail; } };
}

const frames = [];
globalThis.requestAnimationFrame = (cb) => { frames.push(cb); return frames.length; };

let observe = null;
globalThis.ResizeObserver = class {
  constructor(cb) { observe = cb; }
  observe() {}
};

const { initFooterFit } = await import('../../static/js/footerFit.js');

// ── Driving it ────────────────────────────────────────────────────────────────

function flush() {
  while (frames.length) frames.shift()();
}

// The strip becomes `w` wide: the observer reports it, and the fit runs on the
// next frame the way it does in the browser.
function resizeTo(w, height = 54) {
  width = w;
  observe([{ contentRect: { width: w, height } }]);
  flush();
}

const levelNow = () => ['collapse-wrap', 'collapse-click', 'collapse-tight'].filter((c) => classes.has(c)).join(' ') || 'inline';

let passed = 0;
let failed = 0;

function check(name, condition, detail = '') {
  if (condition) {
    passed++;
    console.log(`PASS  ${name}`);
  } else {
    failed++;
    console.log(`FAIL  ${name}${detail ? `  -- ${detail}` : ''}`);
  }
}

// Width alternating between `a` and `b` for `n` frames, the way a scrollbar
// arriving and leaving moves it. Returns every level the strip passed through.
// How many times the level changed along the way.
const changes = (seen) => seen.filter((l, i) => i && l !== seen[i - 1]).length;

function wobble(a, b, n = 20) {
  const seen = [];
  for (let i = 0; i < n; i++) {
    resizeTo(i % 2 ? b : a);
    seen.push(levelNow());
  }
  return seen;
}

initFooterFit();
flush();

// Sanity: the model's thresholds are where the test thinks they are.
resizeTo(NATURAL + 50);
check('a wide strip stays fully inline', levelNow() === 'inline', levelNow());
resizeTo(NATURAL - 20);
check('a strip 20px short wraps the options', levelNow() === 'collapse-wrap', levelNow());
resizeTo(CLICK_W - 10);
check('a strip too short for any wrap goes to the popover and tightens', levelNow() === 'collapse-click collapse-tight', levelNow());

{
  // The inline/wrap boundary, with a 17px classic scrollbar coming and going.
  resizeTo(NATURAL + 200);
  events.length = 0;
  const seen = wobble(NATURAL - 6, NATURAL + 11);
  check(
    'a scrollbar-sized wobble across the inline threshold collapses once and stays',
    new Set(seen).size === 1 && seen[0] === 'collapse-wrap' && events.length === 1,
    `levels=${[...new Set(seen)].join(',')} footerfit events=${events.length}`,
  );
}

{
  // The same wobble with sub-pixel amplitude: the 250% case, where one layout
  // reads either side of the line depending on how it rounds.
  resizeTo(NATURAL + 200);
  events.length = 0;
  const seen = wobble(NATURAL + 0.4, NATURAL - 0.4);
  check(
    'a sub-pixel wobble across the inline threshold settles',
    changes(seen) <= 1 && events.length <= 1,
    `levels=${[...new Set(seen)].join(',')} footerfit events=${events.length}`,
  );
}

{
  // The popover boundary, where space-between spreads the row: the level whose
  // gaps visibly breathed in the report.
  resizeTo(TIGHT_W + 200);
  const settled = levelNow();
  events.length = 0;
  const seen = wobble(CLICK_W - 6, CLICK_W + 11);
  check(
    'a wobble across the popover/tight threshold collapses once and stays',
    new Set(seen).size === 1 && seen[0] === 'collapse-click collapse-tight' && events.length === 1,
    `from=${settled} levels=${[...new Set(seen)].join(',')} footerfit events=${events.length}`,
  );
}

{
  // Hysteresis must not turn into a ratchet: real room still opens it back up.
  resizeTo(CLICK_W - 10);
  resizeTo(NATURAL + 30);
  check('the strip opens fully back up once the room really returns', levelNow() === 'inline', levelNow());
}

{
  // And it never keeps a level that overflows: collapsing needs no slack.
  resizeTo(NATURAL + 30);
  resizeTo(NATURAL - 0.5);
  check('half a pixel short is still short', levelNow() !== 'inline', levelNow());
}

{
  // The strip's height is an output of the fit. A notification that only
  // changes it must not schedule another pass.
  resizeTo(NATURAL - 20);
  observe([{ contentRect: { width, height: 97 } }]);
  check('a height-only resize does not re-fit', frames.length === 0, `queued=${frames.length}`);
  flush();
}

{
  // Measured left-packed, then handed back to the stylesheet: the inline hold
  // must not outlive the pass, or the popover level never spreads.
  resizeTo(CLICK_W + 5);
  check('the left-packed hold is released after measuring', style.justifyContent === '', style.justifyContent);
}

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
