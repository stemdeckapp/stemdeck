// The drawn camera frames' age in Performance (static/js/video/frameAge.js):
// the median, its bounds, and the watch on a fake <video>.
//
// Run:  node tests/js/frame-age.test.mjs

import { medianAgeMs, watchFrameAge, MAX_AGE_MS } from '../../static/js/video/frameAge.js';

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

check('the median of the ages', medianAgeMs([40, 10, 30, 20, 50]) === 30);
check('an even count: the mean of the middle two', medianAgeMs([10, 20, 30, 40, 50, 60]) === 35);
check('fewer than five samples: no answer', medianAgeMs([10, 20, 30, 40]) === null);
check('negative, too large and not-a-number ages are left out',
  medianAgeMs([-5, NaN, MAX_AGE_MS + 1, 20, 20, 20, 20, 20]) === 20);
check('nothing usable: null', medianAgeMs(null) === null && medianAgeMs([]) === null);
check('to a tenth of a millisecond', medianAgeMs([12.345, 12.345, 12.345, 12.345, 12.345]) === 12.3);

// A fake <video>: frames are presented by hand, as the compositor would.
function fakeVideo() {
  let cb = null;
  let id = 0;
  return {
    cancelled: 0,
    requestVideoFrameCallback(fn) { cb = fn; return ++id; },
    cancelVideoFrameCallback() { this.cancelled++; cb = null; },
    present(captureTime) { const f = cb; cb = null; f?.(0, { captureTime }); },
  };
}

{
  const v = fakeVideo();
  const w = watchFrameAge(v);
  w.sample(100);
  check('no frame presented yet: no sample', w.ages.length === 0);
  for (let i = 0; i < 6; i++) {
    v.present(1000 + i * 33);
    w.sample(1000 + i * 33 + 45);
  }
  check('each draw samples the age of the frame on show', w.ages.length === 6 && w.ages.every((a) => a === 45));
  check('the median is the age', w.medianMs() === 45);
  w.stop();
  check('stop cancels the callback', v.cancelled === 1);
  w.sample(9999);
  check('no sample after stop', w.ages.length === 6);
}
{
  const v = fakeVideo();
  const w = watchFrameAge(v);
  v.present(undefined);
  w.sample(100);
  check('a frame without captureTime gives no sample', w.ages.length === 0);
}
{
  const w = watchFrameAge(null);
  w.sample(10);
  check('no camera: no samples, no answer', w.ages.length === 0 && w.medianMs() === null);
  w.stop();
  const plain = watchFrameAge({});
  plain.sample(10);
  check('an engine without requestVideoFrameCallback: no answer', plain.medianMs() === null);
}

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
