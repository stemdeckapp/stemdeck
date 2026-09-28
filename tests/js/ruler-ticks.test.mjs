// The timeline ruler's labelled-tick spacing (static/js/rulerTicks.js).
//
// No e2e can say anything about this: the fixture track is 6 seconds long, so
// every long-track step below would pass against it whatever the code did. An
// hour-long video once got a label every minute, 59 of them piled on top of
// each other; a song has to keep the 15/30/60 second steps it always had.
//
// Run:  node tests/js/ruler-ticks.test.mjs

import { baseTickStep, tickStepAt, TICK_LADDER, MAX_BASE_TICKS, MIN_TICK_PX } from '../../static/js/rulerTicks.js';

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

// Labels buildRuler draws for a step: 0, step, 2*step, ... up to the end.
const labels = (duration, step) => Math.floor(duration / step) + 1;

// ─── 1x: the step by duration alone ───

for (const [name, duration, want] of [
  ['a 30 s clip: every 15 s', 30, 15],
  ['just under 90 s: still 15 s', 89, 15],
  ['90 s: every 30 s', 90, 30],
  ['just under 5 min: every 30 s', 299, 30],
  ['a 5 min song: every minute', 300, 60],
  ['a 12 min track: still every minute, 12 of them', 720, 60],
  ['just past 12 min: every 2 min', 721, 120],
  ['a 59 min video: every 5 min, not every minute', 59 * 60, 300],
  ['an hour: every 5 min', 3600, 300],
  ['two hours: every 10 min', 7200, 600],
  ['twelve hours: every hour', 12 * 3600, 3600],
  ['past the top of the ladder: the top step', 13 * 3600, 3600],
]) {
  const got = baseTickStep(duration);
  check(name, got === want, `baseTickStep(${duration}) = ${got}, want ${want}`);
}

// Every duration up to the ladder's reach gets at most MAX_BASE_TICKS steps.
{
  let worst = { duration: 0, count: 0 };
  for (let duration = 1; duration <= 3600 * MAX_BASE_TICKS; duration += 7) {
    const count = duration / baseTickStep(duration);
    if (count > worst.count) worst = { duration, count };
  }
  check(
    `never more than ${MAX_BASE_TICKS} steps across the track at 1x`,
    worst.count <= MAX_BASE_TICKS,
    `${worst.duration}s gives ${worst.count}`,
  );
}

check('a 59 min video draws 12 labels, not 60', labels(59 * 60, baseTickStep(59 * 60)) === 12);
check('the step is always on the ladder', [30, 89, 300, 721, 3540, 50000].every((d) => TICK_LADDER.includes(baseTickStep(d))));

// ─── zoom subdivides; nothing else does ───

check('at 1x the width is ignored', tickStepAt(59 * 60, 100000, 1) === 300);
check('with no width known, the base step', tickStepAt(59 * 60, 0, 4) === 300);
check('with no duration, the base step', tickStepAt(0, 4000, 4) === baseTickStep(0));
{
  // 59 minutes over 4000 px: 1.13 px a second, so 120 s is the first step at
  // least MIN_TICK_PX wide.
  const step = tickStepAt(59 * 60, 4000, 4);
  check('a zoomed-in hour subdivides to 2 min', step === 120, String(step));
  check('and the labels stay at least MIN_TICK_PX apart', step * (4000 / (59 * 60)) >= MIN_TICK_PX);
}
check('a zoomed-in 30 s clip goes down to 1 s', tickStepAt(30, 5000, 5) === 1);
check('zoom never makes the step coarser than 1x', tickStepAt(59 * 60, 800, 2) === 300);

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
