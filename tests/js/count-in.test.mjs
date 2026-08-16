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

import { computeCountIn } from "../../static/js/metronome.js";

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

console.log(`\n${pass}/${pass + fail} checks passed`);
process.exit(fail ? 1 : 0);
