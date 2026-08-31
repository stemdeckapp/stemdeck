// Adjusting an existing loop region instead of redrawing it (#538).
//
// Only the clamping is tested: an edge crossing its partner, and a region
// pushed against either end of the track. That is where this breaks; the
// pointer plumbing is not something a node test can say anything useful about.

import { loopDragResult } from '../../static/js/loopRegion.js';

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

const near = (a, b) => Math.abs(a - b) < 1e-9;
const DURATION = 100;
// A selection from 20s to 30s, grabbed at 25s.
const base = { grabTime: 25, fromStart: 20, fromEnd: 30, duration: DURATION, minLoop: 0.2 };

// ─── dragging the start edge ───

{
  const r = loopDragResult({ ...base, mode: 'start', pointerTime: 22 });
  check('start edge moves alone', near(r.start, 22) && near(r.end, 30),
    `got ${r.start}..${r.end}`);
}

{
  // The whole point of the request: nudging one edge must not disturb the other.
  const r = loopDragResult({ ...base, mode: 'start', pointerTime: 5 });
  check('start can be dragged earlier without moving the end', near(r.end, 30));
}

{
  const r = loopDragResult({ ...base, mode: 'start', pointerTime: 45 });
  check('start cannot cross the end', near(r.start, 30 - 0.2) && near(r.end, 30),
    `got ${r.start}..${r.end}`);
}

{
  const r = loopDragResult({ ...base, mode: 'start', pointerTime: -10 });
  check('start clamps at zero', near(r.start, 0));
}

// ─── dragging the end edge ───

{
  const r = loopDragResult({ ...base, mode: 'end', pointerTime: 40 });
  check('end edge moves alone', near(r.start, 20) && near(r.end, 40),
    `got ${r.start}..${r.end}`);
}

{
  const r = loopDragResult({ ...base, mode: 'end', pointerTime: 10 });
  check('end cannot cross the start', near(r.start, 20) && near(r.end, 20 + 0.2),
    `got ${r.start}..${r.end}`);
}

{
  const r = loopDragResult({ ...base, mode: 'end', pointerTime: 500 });
  check('end clamps at the track length', near(r.end, DURATION));
}

// ─── moving the whole region ───

{
  const r = loopDragResult({ ...base, mode: 'move', pointerTime: 35 });
  check('move shifts both edges by the same amount',
    near(r.start, 30) && near(r.end, 40), `got ${r.start}..${r.end}`);
}

{
  const r = loopDragResult({ ...base, mode: 'move', pointerTime: 0 });
  check('move stops at the start of the track without squashing',
    near(r.start, 0) && near(r.end, 10), `got ${r.start}..${r.end}`);
}

{
  const r = loopDragResult({ ...base, mode: 'move', pointerTime: 1000 });
  check('move stops at the end of the track without squashing',
    near(r.start, 90) && near(r.end, DURATION), `got ${r.start}..${r.end}`);
}

{
  // Length is the invariant a move must never change.
  for (const pointerTime of [-500, 0, 12, 25, 63, 99, 500]) {
    const r = loopDragResult({ ...base, mode: 'move', pointerTime });
    if (!near(r.end - r.start, 10)) {
      check(`move preserves length at ${pointerTime}`, false, `got ${r.end - r.start}`);
      break;
    }
  }
  check('move preserves length everywhere', true);
}

{
  // A press with no travel must leave the region exactly where it was, or a
  // click-to-seek inside the selection would nudge it.
  const r = loopDragResult({ ...base, mode: 'move', pointerTime: base.grabTime });
  check('a move of zero changes nothing', near(r.start, 20) && near(r.end, 30));
}

console.log(`\n${passed}/${passed + failed} checks passed`);
process.exit(failed === 0 ? 0 : 1);
