// JS half of the click parity gate (#595).
//
// Playback and export must agree beat for beat, or a player monitors one thing
// and exports another. This used to be enforced by writing the same expected
// numbers into tests/js/count-in.test.mjs and tests/test_click_render.py by
// hand, which only catches a mistake in one of them: reason wrongly the same
// way twice and both suites pass.
//
// Both sides are now pinned to one artifact instead. tests/fixtures/
// click_levels.json is generated from Python and asserted by
// tests/test_click_parity.py; this file asserts the browser implementation
// produces the same numbers. Neither language can drift without a visible diff
// to that file.
//
// The two CI jobs cannot run both languages -- pytest runs in a uv container
// with no node, js-syntax in a node container with no python -- so a test that
// shelled out to the other side would simply skip. The shared fixture is what
// makes this gate actually run.
//
// Run:  node tests/js/parity.test.mjs

import { readFileSync } from "node:fs";
import { levelAt, defaultGrouping, computeCountIn } from "../../static/js/metronome.js";

const fixture = JSON.parse(
  readFileSync(new URL("../fixtures/click_levels.json", import.meta.url), "utf8"),
);

let pass = 0,
  fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) {
    pass++;
  } else {
    fail++;
    console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`);
  }
};
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
// Python normalises an empty grouping to None before use; mirror that here so
// the two are compared on the same input, not on a difference in falsiness.
const norm = (g) => (Array.isArray(g) && g.length ? g : null);

// The same grid tests/_click_parity.py counts against.
const STEADY = Array.from({ length: 32 }, (_, i) => 0.5 + i * 0.5);

for (const [n, expected] of Object.entries(fixture.defaultGrouping)) {
  const got = defaultGrouping(Number(n));
  check(`defaultGrouping(${n})`, eq(got, expected), `got ${JSON.stringify(got)}, want ${JSON.stringify(expected)}`);
}

for (const e of fixture.levels) {
  if (e.beatsPerBar === -1) {
    // Auto: each detected bar is grouped by its own length.
    const per = e.bars[0].beats_per_bar;
    const got = e.levels.map((_, i) => levelAt(i % per, per, null));
    check(`auto levels, detected ${per}/bar`, eq(got, e.levels), `got ${got}`);
  } else {
    const n = e.beatsPerBar;
    const got = e.levels.map((_, i) => levelAt(i % n, n, norm(e.groups)));
    check(
      `levels ${n} groups=${JSON.stringify(e.groups)}`,
      eq(got, e.levels),
      `got ${got}, want ${e.levels}`,
    );
  }
}

for (const e of fixture.countIn) {
  const { leadIn, clicks } = computeCountIn(STEADY, [], {
    countBars: e.countBars,
    accentMode: e.beatsPerBar,
    groups: norm(e.groups),
  });
  const label = `countIn ${e.beatsPerBar} x${e.countBars} groups=${JSON.stringify(e.groups)}`;
  check(`${label}: leadIn`, Math.abs(leadIn - e.leadIn) < 1e-6, `got ${leadIn}, want ${e.leadIn}`);
  check(
    `${label}: levels`,
    eq(clicks.map((c) => c.level), e.levels),
    `got ${clicks.map((c) => c.level)}`,
  );
  check(
    `${label}: offsets`,
    eq(clicks.map((c) => Number(c.offset.toFixed(6))), e.offsets),
    `got ${clicks.map((c) => Number(c.offset.toFixed(6)))}`,
  );
}

console.log(`\n${pass}/${pass + fail} parity checks passed against tests/fixtures/click_levels.json`);
process.exit(fail ? 1 : 0);
