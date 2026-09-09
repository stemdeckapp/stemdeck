// The clock behind both audio engines' tick.
//
// requestAnimationFrame is not delivered to a hidden tab, and a frame already
// pending when the tab hides is dropped rather than deferred. A loop built
// straight on rAF therefore has no callback left to notice it should do
// anything about it, and simply stops. In the chunked engine that stops chunk
// scheduling, so playback runs out its lookahead and goes silent about twelve
// seconds later with no error anywhere (#600).
//
// These checks pin the two halves of the fix: the switch to a timer while
// hidden, and the deregistration on dispose that keeps a torn-down engine from
// being restarted (and from being kept alive at all -- the closure holds every
// decoded buffer).

// Installed before importing the module under test: createTickLoop binds the
// document listener on first use, so the fake has to be in place by then.
const raf = { scheduled: [], cancelled: [] };
const timer = { scheduled: [], cleared: [] };
let nextId = 0;

const listeners = new Map();
globalThis.document = {
  hidden: false,
  addEventListener(type, fn) {
    if (!listeners.has(type)) listeners.set(type, []);
    listeners.get(type).push(fn);
  },
  removeEventListener(type, fn) {
    const l = listeners.get(type) || [];
    const i = l.indexOf(fn);
    if (i >= 0) l.splice(i, 1);
  },
};
const fire = (type) => { for (const fn of [...(listeners.get(type) || [])]) fn(); };

globalThis.requestAnimationFrame = (cb) => {
  const id = ++nextId;
  raf.scheduled.push({ id, cb });
  return id;
};
globalThis.cancelAnimationFrame = (id) => { raf.cancelled.push(id); };
globalThis.setTimeout = (cb, ms) => {
  const id = ++nextId;
  timer.scheduled.push({ id, cb, ms });
  return id;
};
globalThis.clearTimeout = (id) => { timer.cleared.push(id); };

const { createTickLoop, _liveLoopCount } = await import('../../static/js/tickLoop.js');

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

function reset() {
  raf.scheduled.length = 0;
  raf.cancelled.length = 0;
  timer.scheduled.length = 0;
  timer.cleared.length = 0;
  document.hidden = false;
}

{
  // The ordinary case, unchanged from what the engines did before.
  reset();
  const loop = createTickLoop(() => {});
  loop.schedule();
  check(
    'a visible tab schedules on requestAnimationFrame',
    raf.scheduled.length === 1 && timer.scheduled.length === 0,
  );
  loop.dispose();
}

{
  reset();
  document.hidden = true;
  const loop = createTickLoop(() => {});
  loop.schedule();
  check(
    'a hidden tab schedules on a timer instead',
    timer.scheduled.length === 1 && raf.scheduled.length === 0,
  );
  loop.dispose();
}

{
  // The regression this file exists for. Before the fix the pending frame was
  // silently dropped by the browser and nothing rescheduled, so the loop -- and
  // with it chunk scheduling -- was simply over.
  reset();
  const loop = createTickLoop(() => {});
  loop.schedule();
  const pending = raf.scheduled[0].id;

  document.hidden = true;
  fire('visibilitychange');

  check(
    'hiding the tab moves a running loop onto a timer',
    timer.scheduled.length === 1,
    `timers=${timer.scheduled.length} rafs=${raf.scheduled.length}`,
  );
  check(
    'the frame that will never fire is cancelled',
    raf.cancelled.includes(pending),
    `cancelled=${JSON.stringify(raf.cancelled)}`,
  );
  loop.dispose();
}

{
  // Cancelling has to match the clock that scheduled it. Handing a timeout id
  // to cancelAnimationFrame is not an error, it just does nothing, and the loop
  // keeps ticking after the engine believes it stopped it.
  reset();
  document.hidden = true;
  const loop = createTickLoop(() => {});
  loop.schedule();
  const id = timer.scheduled[0].id;
  loop.cancel();
  check(
    'a timer-scheduled loop is cancelled with clearTimeout',
    timer.cleared.includes(id) && raf.cancelled.length === 0,
    `cleared=${JSON.stringify(timer.cleared)} rafCancelled=${JSON.stringify(raf.cancelled)}`,
  );
  loop.dispose();
}

{
  // A paused engine must stay paused. Hiding the tab is not a reason to start
  // ticking again.
  reset();
  const loop = createTickLoop(() => {});
  loop.schedule();
  loop.cancel();
  document.hidden = true;
  fire('visibilitychange');
  check(
    'a stopped loop is not restarted by hiding the tab',
    timer.scheduled.length === 0,
    `timers=${timer.scheduled.length}`,
  );
  loop.dispose();
}

{
  // The leak. destroyPlayer() tears an engine down without pausing it, so
  // before the fix a destroyed engine's loop was still registered, still saw
  // `playing` as true, and was resurrected on the next tab switch -- against a
  // closed AudioContext, forever, holding all of that track's decoded audio.
  reset();
  const before = _liveLoopCount();
  const loop = createTickLoop(() => {});
  loop.schedule();
  loop.dispose();

  check('dispose deregisters the loop', _liveLoopCount() === before);

  document.hidden = true;
  fire('visibilitychange');
  check(
    'a disposed loop is never restarted',
    timer.scheduled.length === 0,
    `timers=${timer.scheduled.length}`,
  );
}

{
  // One listener for the whole page, however many engines have come and gone.
  reset();
  const loops = [createTickLoop(() => {}), createTickLoop(() => {}), createTickLoop(() => {})];
  check(
    'every loop shares a single document listener',
    (listeners.get('visibilitychange') || []).length === 1,
    `listeners=${(listeners.get('visibilitychange') || []).length}`,
  );
  for (const l of loops) l.dispose();
}

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
