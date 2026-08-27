// How much of a lane meter a real stem actually lights.
//
// The bug this exists to prevent came back as a complaint, not a failure: the
// meters worked, moved with the audio and were simply unreadable, because they
// were linear in amplitude. Nothing could catch that, because nothing asserted
// what fraction of the bar normal playback uses.
//
// So this measures exactly that, against the levels separated stems really sit
// at, and keeps the old linear formula around as the control. A scale that
// leaves the median lane in the bottom tenth of its meter fails here.
//
// Run:  node tests/js/vu-scale.test.mjs

import { VU_FLOOR_DB, vuLevel } from "../../static/js/vuScale.js";

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

const dbToRms = (db) => Math.pow(10, db / 20);
const pct = (rms) => vuLevel(rms) * 100;

// The linear scale this replaced, kept as the control so the comparison below
// is a measurement rather than an assertion about the past.
const linear = (rms) => Math.min(1, rms * 2.5) * 100;

// ── 1. The ends behave ──────────────────────────────────────────────────────
{
  check("silence reads empty", vuLevel(0) === 0);
  check("a negative or bogus level reads empty", vuLevel(-1) === 0 && vuLevel(NaN) === 0);
  check("full scale fills the bar", vuLevel(1) === 1);
  check("anything above full scale is clamped, not overflowed", vuLevel(4) === 1);
  check(
    "the floor is the zero point",
    Math.abs(vuLevel(dbToRms(VU_FLOOR_DB))) < 1e-9,
    `${pct(dbToRms(VU_FLOOR_DB)).toFixed(3)}%`,
  );
  check("below the floor stays empty", vuLevel(dbToRms(VU_FLOOR_DB - 20)) === 0);
}

// ── 2. It is monotonic ──────────────────────────────────────────────────────
// A meter that ever goes down as the audio gets louder is worse than no meter.
{
  let ok = true;
  let previous = -1;
  for (let db = VU_FLOOR_DB; db <= 0; db += 0.25) {
    const level = vuLevel(dbToRms(db));
    if (level < previous) ok = false;
    previous = level;
  }
  check("louder never draws shorter", ok);
}

// ── 3. Real stems use the bar ───────────────────────────────────────────────
// These are median RMS levels measured from a real StemDeck library: four
// tracks, the vocals, drums, bass and "other" stems of each. They are the
// levels the meter spends its time at, so they are the levels worth testing.
{
  const MEASURED = [
    ["drums, quiet mix", 0.0068],
    ["other, quiet mix", 0.0064],
    ["vocals, quiet mix", 0.02],
    ["bass, quiet mix", 0.021],
    ["vocals, loud mix", 0.082],
    ["drums, loud mix", 0.034],
    ["bass, loud mix", 0.136],
  ];

  let worstNew = 100;
  let worstOld = 100;
  let worstCase = "";
  for (const [label, rms] of MEASURED) {
    if (pct(rms) < worstNew) {
      worstNew = pct(rms);
      worstCase = label;
    }
    worstOld = Math.min(worstOld, linear(rms));
  }

  // The complaint was "less than 15% of the bar", and the old scale earned it.
  check(
    "the scale this replaced really did waste the bar",
    worstOld < 15,
    `quietest measured stem lit ${worstOld.toFixed(1)}% under the linear scale`,
  );
  check(
    "every measured stem now lights at least a quarter of its meter",
    worstNew >= 25,
    `${worstCase} lights ${worstNew.toFixed(1)}%`,
  );

  const median = MEASURED.map(([, rms]) => pct(rms)).sort((a, b) => a - b)[
    Math.floor(MEASURED.length / 2)
  ];
  check(
    "the median stem sits in the middle of the bar, not the bottom",
    median > 40 && median < 80,
    `${median.toFixed(1)}%`,
  );
}

// ── 4. It still leaves headroom ─────────────────────────────────────────────
// A scale that fixes "always empty" by always being full is no better. Normal
// material must not sit pinned at the top, and the loudest thing a stem can be
// must still be distinguishable from merely loud.
{
  check(
    "ordinary programme material leaves room above it",
    pct(0.1) < 80,
    `${pct(0.1).toFixed(1)}% at -20 dBFS`,
  );
  check(
    "a hot stem and a full-scale one are told apart",
    pct(1) - pct(0.35) > 10,
    `${pct(0.35).toFixed(1)}% vs ${pct(1).toFixed(1)}%`,
  );
  check(
    "nothing normal is clipped to full",
    pct(0.5) < 100,
    `${pct(0.5).toFixed(1)}% at -6 dBFS`,
  );
}

// ── 5. Quiet is still visibly quiet ─────────────────────────────────────────
// The point is a readable meter, not a flattering one: a stem 20 dB down on
// another has to look 20 dB down.
{
  const loud = pct(0.1);
  const quiet = pct(0.01);
  check(
    "a stem 20 dB down reads clearly lower",
    loud - quiet > 25,
    `${quiet.toFixed(1)}% vs ${loud.toFixed(1)}%`,
  );
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
