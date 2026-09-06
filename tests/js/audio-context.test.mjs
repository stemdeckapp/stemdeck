// The rate the audio graph runs at.
//
// Every stem is 44.1 kHz, so a context inheriting a 192 kHz output device does
// several times the DSP and holds four times the buffers for nothing (#578).
// These checks pin down that the cap only ever fires above 48 kHz, and that a
// browser refusing the requested rate still gets a working context.
import { createPlaybackContext } from '../../static/js/audioContext.js';

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

/** A stand-in AudioContext that records how each instance was constructed. */
function fakeCtor(deviceRate, { probeThrows = false, cappedThrows = false } = {}) {
  const built = [];
  class FakeCtx {
    constructor(options) {
      built.push(options);
      if (probeThrows && built.length === 1) throw new Error('no audio device');
      if (cappedThrows && options?.sampleRate) throw new Error('rate not available');
      this.options = options;
      // The real constructor grants the rate it is asked for, or the device's.
      this.sampleRate = options?.sampleRate || deviceRate;
      this.closed = false;
    }

    close() { this.closed = true; return Promise.resolve(); }
  }
  return { FakeCtx, built };
}

// Below the cap the graph is left exactly as it is today. Pinning here would
// only add a resample that nothing asked for.
for (const rate of [44100, 48000]) {
  const { FakeCtx, built } = fakeCtor(rate);
  const ctx = createPlaybackContext(FakeCtx);
  check(
    `a ${rate} Hz device is left alone`,
    built.length === 2 && built[1] === undefined && ctx.sampleRate === rate,
    `built ${JSON.stringify(built)} at ${ctx.sampleRate}`,
  );
}

// Above it, the device is asking for more resolution than the material has.
for (const rate of [88200, 96000, 176400, 192000]) {
  const { FakeCtx, built } = fakeCtor(rate);
  const ctx = createPlaybackContext(FakeCtx);
  check(
    `a ${rate} Hz device is capped to 48000`,
    built[1]?.sampleRate === 48000 && ctx.sampleRate === 48000,
    `built ${JSON.stringify(built)} at ${ctx.sampleRate}`,
  );
}

{
  // The probe exists only to read the device rate, and nothing is ever routed
  // into it. Leaving it open would hold a hardware stream open for the session.
  const { FakeCtx } = fakeCtor(192000);
  let probe = null;
  const Wrapped = class extends FakeCtx {
    constructor(options) { super(options); if (!probe) probe = this; }
  };
  createPlaybackContext(Wrapped);
  check('the probe context is closed again', probe?.closed === true);
}

{
  // A browser that will not build a context at all is not a reason to give up
  // on playback: the device's own rate is what we would have used anyway.
  const { FakeCtx, built } = fakeCtor(192000, { probeThrows: true });
  const ctx = createPlaybackContext(FakeCtx);
  check(
    'a failed probe falls back to the plain constructor',
    ctx.sampleRate === 192000 && built.length === 2 && built[1] === undefined,
    `built ${JSON.stringify(built)}`,
  );
}

{
  // Safari has historically refused rates its device cannot produce. Playing at
  // the device's rate is worse than capping and far better than not playing.
  const { FakeCtx, built } = fakeCtor(192000, { cappedThrows: true });
  const ctx = createPlaybackContext(FakeCtx);
  check(
    'a refused rate falls back to the plain constructor',
    ctx.sampleRate === 192000 && built.length === 3 && built[2] === undefined,
    `built ${JSON.stringify(built)}`,
  );
}

check('no constructor means no context', createPlaybackContext(null) === null);

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
