// The global key a song opens at, read from its saved record (#781).
//
// A record saved since the key was remembered says so itself. One saved by
// 0.20 has only the lanes, which the global control moved together, so the
// key is read back from them rather than shown as 0 over a transposed track.
//
// Run:  node tests/js/saved-global-pitch.test.mjs

import { savedGlobalPitch } from '../../static/js/pitchBus.js';

let passed = 0;
let failed = 0;

function check(name, actual, expected) {
  if (Object.is(actual, expected)) {
    passed++;
    console.log(`PASS  ${name}`);
  } else {
    failed++;
    console.log(`FAIL  ${name}  -- expected ${expected}, got ${actual}`);
  }
}

const lane = (pitch) => ({ volume: 1, muted: false, soloed: false, pitch });
const LANES = ['vocals', 'drums', 'bass', 'other'];

check('a record that holds the key is read as it is',
  savedGlobalPitch({ __global: { pitch: -2, speed: 1 }, vocals: lane(-2), bass: lane(-2) }, LANES), -2);
check('the saved key wins over the lanes, which may be moved on their own',
  savedGlobalPitch({ __global: { pitch: 1 }, vocals: lane(4), bass: lane(1), other: lane(1) }, LANES), 1);
check('a saved key out of range is clamped',
  savedGlobalPitch({ __global: { pitch: -11 } }, LANES), -6);
check('a saved key of 0 is 0, whatever the lanes say',
  savedGlobalPitch({ __global: { pitch: 0 }, vocals: lane(3), bass: lane(3) }, LANES), 0);

check('a 0.20 record: the key the lanes were moved to',
  savedGlobalPitch({ vocals: lane(-2), drums: lane(0), bass: lane(-2), other: lane(-2) }, LANES), -2);
check('drums never count, they are never moved',
  savedGlobalPitch({ vocals: lane(3), drums: lane(0), bass: lane(3), other: lane(3) }, LANES), 3);
check('one lane moved on its own does not outvote the rest',
  savedGlobalPitch({ vocals: lane(1), bass: lane(-2), other: lane(-2) }, LANES), -2);
check('a tie goes to the key nearer the original',
  savedGlobalPitch({ vocals: lane(3), bass: lane(-1) }, ['vocals', 'bass']), -1);
check('lanes the song does not have are not counted',
  savedGlobalPitch({ vocals: lane(-2), guitar: lane(5), piano: lane(5) }, LANES), -2);
check('no record is the original key', savedGlobalPitch(null, LANES), 0);
check('an empty record is the original key', savedGlobalPitch({}, LANES), 0);
check('a record whose lanes carry no key is the original key',
  savedGlobalPitch({ vocals: { volume: 1, muted: true } }, LANES), 0);
check('no lane names is the original key',
  savedGlobalPitch({ vocals: lane(-2) }, undefined), 0);

console.log(`\n${passed}/${passed + failed} checks passed`);
process.exit(failed === 0 ? 0 : 1);
