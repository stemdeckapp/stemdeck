// The bar lookup in static/js/beatgrid.js.
//
// barPositionIndex is what the metronome asks to group a bar, and its docstring
// names `_bar_position` in app/pipeline/click_render.py as the same lookup on
// the export side. tests/js/parity.test.mjs pins metronome.js's levelAt against
// the Python fixture, but nothing covered the lookup feeding it -- so the two
// halves of "playback and export agree" were only half tested. A beatgrid that
// reports the wrong bar offset makes the player accent a different beat than
// the exported click track, which is exactly the class of bug #595 was about.
//
// initBeatGrid needs a canvas, so these drive the module through its own
// state-loading path with a stub element and then ask the pure lookups.
//
// Run:  node tests/js/beatgrid-bars.test.mjs

import {
  barLengthAt,
  barPositionIndex,
  destroyBeatGrid,
  getBars,
  getBeats,
  initBeatGrid,
  isDownbeatIndex,
} from "../../static/js/beatgrid.js";

let pass = 0;
let fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) {
    pass++;
    console.log(`PASS  ${name}`);
  } else {
    fail++;
    console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`);
  }
};
const same = (name, actual, expected) =>
  check(
    name,
    JSON.stringify(actual) === JSON.stringify(expected),
    `got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)}`,
  );

// --------------------------------------------------------------------------
// Just enough DOM for initBeatGrid to attach. The module only needs somewhere
// to hang a canvas and its listeners; nothing here is rendered or asserted.
// --------------------------------------------------------------------------

function stubCanvas() {
  const noop = () => {};
  const ctx = new Proxy(
    { canvas: null },
    { get: (t, p) => (p in t ? t[p] : noop) },
  );
  return {
    width: 800,
    height: 60,
    style: {},
    getContext: () => ctx,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 60 }),
    addEventListener: noop,
    removeEventListener: noop,
    setPointerCapture: noop,
    releasePointerCapture: noop,
    classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
    parentElement: null,
    appendChild: noop,
  };
}

globalThis.window = globalThis.window ?? { devicePixelRatio: 1, addEventListener: () => {} };
globalThis.devicePixelRatio = 1;
// initBeatGrid reads its editor preferences through utils.storeGet, which falls
// back to localStorage outside Tauri. It copes with the absence, but only by
// logging -- and a screenful of that would bury a real failure in CI.
globalThis.localStorage = {
  _v: new Map(),
  get length() {
    return this._v.size;
  },
  key(i) {
    return [...this._v.keys()][i] ?? null;
  },
  getItem(k) {
    return this._v.has(k) ? this._v.get(k) : null;
  },
  setItem(k, v) {
    this._v.set(k, String(v));
  },
  removeItem(k) {
    this._v.delete(k);
  },
};
globalThis.requestAnimationFrame = (fn) => fn(0);
globalThis.cancelAnimationFrame = () => {};
globalThis.ResizeObserver = class {
  observe() {}
  disconnect() {}
};

const canvas = stubCanvas();
globalThis.document = {
  getElementById: () => canvas,
  querySelector: () => canvas,
  createElement: () => stubCanvas(),
  addEventListener: () => {},
  removeEventListener: () => {},
};

function load(grid) {
  destroyBeatGrid();
  initBeatGrid({ jobId: "abcdefabcdef", grid, duration: 60, onChange: () => {} });
}

// --------------------------------------------------------------------------
// A plain 4/4 track
// --------------------------------------------------------------------------
{
  load({
    beats: Array.from({ length: 16 }, (_, i) => i * 0.5),
    bars: [{ beat: 0, beats_per_bar: 4 }],
  });

  check("the beats loaded", getBeats().length === 16);
  same("the bar marks loaded", getBars(), [{ beat: 0, beats_per_bar: 4 }]);

  same("beat 0 opens the bar", barPositionIndex(0), [0, 4]);
  same("beat 1 is one into the bar", barPositionIndex(1), [1, 4]);
  same("beat 3 is the last of the bar", barPositionIndex(3), [3, 4]);
  same("beat 4 opens the next bar", barPositionIndex(4), [0, 4]);
  same("beat 9 wraps correctly", barPositionIndex(9), [1, 4]);

  check("every fourth beat is a downbeat", [0, 4, 8, 12].every(isDownbeatIndex));
  check("nothing else is", [1, 2, 3, 5, 6, 7].every((i) => !isDownbeatIndex(i)));

  check("the bar length in force is four", barLengthAt(0) === 4 && barLengthAt(7) === 4);

  // The invariant the metronome relies on: a downbeat is exactly offset zero.
  check(
    "downbeat and offset-zero are the same question",
    Array.from({ length: 16 }, (_, i) => i).every(
      (i) => isDownbeatIndex(i) === (barPositionIndex(i)?.[0] === 0),
    ),
  );
}

// --------------------------------------------------------------------------
// A time-signature change partway through -- the case a single modulo misses
// --------------------------------------------------------------------------
{
  load({
    beats: Array.from({ length: 20 }, (_, i) => i * 0.5),
    bars: [
      { beat: 0, beats_per_bar: 4 },
      { beat: 8, beats_per_bar: 3 },
    ],
  });

  same("before the change the first mark applies", barPositionIndex(5), [1, 4]);
  same("the change itself opens a bar", barPositionIndex(8), [0, 3]);
  same("and counts in three from there", barPositionIndex(9), [1, 3]);
  same("the second bar of the new signature", barPositionIndex(11), [0, 3]);

  // A naive `i % 4` would call beat 12 a downbeat; under 3/4 from beat 8 it is
  // the second beat of a bar.
  check("beat 12 is not a downbeat once the signature changed", !isDownbeatIndex(12));
  check("beat 11 is", isDownbeatIndex(11));

  check("the bar length follows the preceding mark", barLengthAt(5) === 4 && barLengthAt(9) === 3);
}

// --------------------------------------------------------------------------
// Odd and compound bars (#595 is about grouping these)
// --------------------------------------------------------------------------
{
  load({
    beats: Array.from({ length: 14 }, (_, i) => i * 0.5),
    bars: [{ beat: 0, beats_per_bar: 7 }],
  });

  same("a seven-beat bar reports its own length", barPositionIndex(0), [0, 7]);
  same("and wraps at seven", barPositionIndex(7), [0, 7]);
  same("not at four", barPositionIndex(4), [4, 7]);
  check("only every seventh beat is a downbeat", [0, 7].every(isDownbeatIndex));
  check("beat 4 is not", !isDownbeatIndex(4));
}

// --------------------------------------------------------------------------
// Grids with nothing to go on
// --------------------------------------------------------------------------
{
  load({ beats: [0, 0.5, 1.0], bars: [] });

  same("no bar marks means no bar position", barPositionIndex(0), null);
  check("and no downbeats", ![0, 1, 2].some(isDownbeatIndex));
  // The documented default when nothing says otherwise.
  check("the bar length still defaults to four", barLengthAt(0) === 4);
}

{
  // A mark that starts after the beat being asked about must not apply
  // retroactively -- the beats before the first mark belong to no bar.
  load({
    beats: Array.from({ length: 8 }, (_, i) => i * 0.5),
    bars: [{ beat: 4, beats_per_bar: 4 }],
  });

  same("a beat before the first mark has no position", barPositionIndex(0), null);
  same("a beat before the first mark is not a downbeat", isDownbeatIndex(0), false);
  same("the mark applies from its own beat", barPositionIndex(4), [0, 4]);
}

{
  // A malformed mark (a bar length of zero would be a division by zero, a
  // fractional one is meaningless) must degrade to "no bar", not to NaN.
  load({
    beats: [0, 0.5, 1.0, 1.5],
    bars: [{ beat: 0, beats_per_bar: 0 }],
  });
  same("a zero-length bar yields no position", barPositionIndex(1), null);
  check("and no downbeat", !isDownbeatIndex(0));
}

{
  load({
    beats: [0, 0.5, 1.0, 1.5],
    bars: [{ beat: 0, beats_per_bar: 2.5 }],
  });
  same("a fractional bar length yields no position", barPositionIndex(1), null);
}

destroyBeatGrid();

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
