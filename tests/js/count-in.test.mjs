// Parity + behaviour test for computeCountIn (issue #269).
//
// The live count-in (metronome.js) and the exported one (app/pipeline/
// click_render.py::count_in_beats) MUST agree beat-for-beat, or a player hears
// one thing while monitoring and gets another in the file. This pins the JS
// side; the Python side is pinned by tests/test_click_render.py. The expected
// values below are the shared spec both implementations are held to -- keep the
// two files in lockstep when either changes.
//
// Run:  node tests/js/count-in.test.mjs

import {
  computeCountIn,
  defaultGrouping,
  normaliseGrouping,
  levelAt,
  LEVEL_WEAK,
  LEVEL_GROUP,
  LEVEL_DOWNBEAT,
} from "../../static/js/metronome.js";

let pass = 0,
  fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) {
    pass++;
    console.log(`PASS  ${name}`);
  } else {
    fail++;
    console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`);
  }
};

const approx = (a, b, eps = 1e-6) => Math.abs(a - b) < eps;
const shape = (clicks) => clicks.map((c) => [Number(c.offset.toFixed(4)), c.accent]);

// 120 BPM from 0.5 s -- the same grid the Python parity tests use.
const STEADY = Array.from({ length: 16 }, (_, i) => 0.5 + i * 0.5);

{
  // PI po po po: one bar of four, downbeat accented, half-second spacing.
  const { leadIn, clicks } = computeCountIn(STEADY, [{ beat: 0, beats_per_bar: 4 }]);
  check("4/4: lead-in is one bar (2.0 s)", approx(leadIn, 2.0), `got ${leadIn}`);
  check(
    "4/4: PI po po po",
    JSON.stringify(shape(clicks)) ===
      JSON.stringify([
        [0, true],
        [0.5, false],
        [1, false],
        [1.5, false],
      ]),
    JSON.stringify(shape(clicks)),
  );
}

{
  const { leadIn, clicks } = computeCountIn(STEADY, [{ beat: 0, beats_per_bar: 3 }]);
  check("3/4: lead-in 1.5 s, three clicks", approx(leadIn, 1.5) && clicks.length === 3);
}

{
  const { clicks } = computeCountIn(STEADY, [], { accentMode: 4 });
  check("explicit accent sets the bar length", clicks.length === 4);
}

{
  const { clicks } = computeCountIn(STEADY, [], { accentMode: -1 });
  check("no marks defaults to four", clicks.length === 4);
}

{
  const { leadIn, clicks } = computeCountIn(STEADY, [{ beat: 0, beats_per_bar: 4 }], {
    countBars: 2,
  });
  check(
    "two bars accents each downbeat",
    approx(leadIn, 4.0) &&
      JSON.stringify(clicks.map((c) => c.accent)) ===
        JSON.stringify([true, false, false, false, true, false, false, false]),
  );
}

{
  const { clicks } = computeCountIn(STEADY, [{ beat: 0, beats_per_bar: 4 }], { accentMode: 0 });
  check("accents-off count-in still marks its downbeat", clicks[0].accent === true);
}

{
  const x2 = computeCountIn(STEADY, [{ beat: 0, beats_per_bar: 4 }], { multiplier: 2 });
  const half = computeCountIn(STEADY, [{ beat: 0, beats_per_bar: 4 }], { multiplier: 0.5 });
  check("x2: one bar of the doubled grid, 1.0 s", x2.clicks.length === 4 && approx(x2.leadIn, 1.0));
  check("half: one bar of the halved grid, 4.0 s", half.clicks.length === 4 && approx(half.leadIn, 4.0));
}

{
  // 60 BPM then 150 BPM: the count-in must take the tempo where playback begins.
  const varied = [0.0, 1.0, 2.0, 3.0, 3.4, 3.8, 4.2, 4.6];
  const bars = [{ beat: 0, beats_per_bar: 4 }];
  const slow = computeCountIn(varied, bars, { start: 0.0 });
  const fast = computeCountIn(varied, bars, { start: 3.4 });
  check("tempo tracks the start position", approx(slow.leadIn, 4.0) && approx(fast.leadIn, 1.6),
    `slow=${slow.leadIn} fast=${fast.leadIn}`);
}

{
  const empty = computeCountIn([0.5], [{ beat: 0, beats_per_bar: 4 }]);
  const disabled = computeCountIn(STEADY, [], { countBars: 0 });
  check("empty when grid too short", empty.leadIn === 0 && empty.clicks.length === 0);
  check("empty when disabled", disabled.leadIn === 0 && disabled.clicks.length === 0);
}

{
  // #587: the panel now offers 1-4 bars, so every length the select can reach
  // has to produce a whole number of bars with an accent on each downbeat.
  // Python's count_in_beats is held to the same shape in
  // tests/test_click_render.py::test_longer_count_in_lengthens_the_lead_in.
  const bars = [{ beat: 0, beats_per_bar: 4 }];
  for (const n of [1, 2, 3, 4]) {
    const { leadIn, clicks } = computeCountIn(STEADY, bars, { countBars: n });
    check(`${n}-bar count-in: lead-in is ${n} bars`, approx(leadIn, 2.0 * n), `got ${leadIn}`);
    check(`${n}-bar count-in: ${4 * n} clicks`, clicks.length === 4 * n, `got ${clicks.length}`);
    const accents = clicks.filter((c) => c.accent).length;
    check(`${n}-bar count-in: one accent per bar`, accents === n, `got ${accents}`);
    check(
      `${n}-bar count-in: accents land on downbeats`,
      clicks.every((c, i) => c.accent === (i % 4 === 0)),
    );
  }
}

{
  // A count-in in 7/8 is 7 clicks a bar, not 4 -- the custom meter the click
  // panel now accepts has to reach the count-in too, not just the accents.
  const { leadIn, clicks } = computeCountIn(STEADY, [{ beat: 0, beats_per_bar: 7 }], {
    countBars: 2,
  });
  check("7/8 x2: 14 clicks", clicks.length === 14, `got ${clicks.length}`);
  check("7/8 x2: lead-in is 7.0 s", approx(leadIn, 7.0), `got ${leadIn}`);
  check(
    "7/8 x2: accents only on the two downbeats",
    clicks.filter((c) => c.accent).length === 2 && clicks[0].accent && clicks[7].accent,
  );
}

{
  // #595 grouping. These expectations are the same spec Python is held to in
  // tests/test_click_render.py -- default_grouping, beat_level and
  // count_in_beats there must produce identical numbers, or a player monitors
  // one thing and exports another.
  const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

  check("simple meters keep one group", eq(
    [1, 2, 3, 4].map(defaultGrouping), [[1], [2], [3], [4]]));
  check("odd and compound meters split", eq(
    [5, 6, 7, 9, 12].map(defaultGrouping),
    [[3, 2], [3, 3], [3, 2, 2], [3, 3, 3], [3, 3, 3, 3]]));
  check("a prime with no conventional reading stays flat", eq(defaultGrouping(11), [11]));

  check("a grouping that does not fit the bar is refused", eq(normaliseGrouping([3, 3], 7), [3, 2, 2]));
  check("a grouping that fits is kept", eq(normaliseGrouping([2, 2, 3], 7), [2, 2, 3]));

  const W = LEVEL_WEAK, G = LEVEL_GROUP, D = LEVEL_DOWNBEAT;
  check("7/8 is clicked 3+2+2, not flat",
    eq([...Array(7).keys()].map((i) => levelAt(i, 7, null)), [D, W, W, G, W, G, W]));
  check("a user grouping moves the group accents",
    eq([...Array(7).keys()].map((i) => levelAt(i, 7, [2, 2, 3])), [D, W, G, W, G, W, W]));
  check("4/4 is unchanged",
    eq([...Array(4).keys()].map((i) => levelAt(i, 4, null)), [D, W, W, W]));
  check("6/8 is felt in two groups of three",
    eq([...Array(6).keys()].map((i) => levelAt(i, 6, null)), [D, W, W, G, W, W]));
}

{
  // The count-in has to carry the same pulse the click is about to play.
  const W = LEVEL_WEAK, G = LEVEL_GROUP, D = LEVEL_DOWNBEAT;
  const { clicks } = computeCountIn(STEADY, [], { accentMode: 7 });
  check("count-in into 7/8 is grouped",
    JSON.stringify(clicks.map((c) => c.level)) === JSON.stringify([D, W, W, G, W, G, W]),
    JSON.stringify(clicks.map((c) => c.level)));

  const custom = computeCountIn(STEADY, [], { accentMode: 7, groups: [2, 2, 3] });
  check("count-in follows a user grouping",
    JSON.stringify(custom.clicks.map((c) => c.level)) === JSON.stringify([D, W, G, W, G, W, W]));

  // The old boolean is still exposed, so anything that only asks "is this the
  // 1" keeps working across the level change.
  check("accent stays a downbeat-only boolean",
    JSON.stringify(clicks.map((c) => c.accent)) ===
      JSON.stringify([true, false, false, false, false, false, false]));
}

console.log(`\n${pass}/${pass + fail} checks passed`);
process.exit(fail ? 1 : 0);
