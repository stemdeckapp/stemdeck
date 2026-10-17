// Putting the place back after the open song reloads (reloadPlaceCore.js).
//
// A stem change while playing used to stop playback and rewind; and the
// playhead was forced back to the saved time for a second and a half after it
// held, so Play, a seek or turning the loop off right after was undone.
import { newRestore, restoreStep, SETTLE_MS, MAX_SEEKS } from '../../static/js/reloadPlaceCore.js';

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

/**
 * A fake transport the actions act on, stepped every 100 ms. A fresh engine
 * comes up at 0, paused.
 */
function run(at, { upAt = 500, steps = 40, user = () => {}, ignoreSeeks = 0 } = {}) {
  const p = newRestore(at);
  const tr = { time: 0, playing: false, loopOn: false, seeks: 0, plays: 0, loops: 0 };
  const log = [];
  for (let i = 0; i < steps && !p.done; i++) {
    const nowMs = i * 100;
    if (tr.playing) tr.time += 0.1;
    user(i, tr, p);
    const acts = restoreStep(p, { nowMs, engineUp: nowMs >= upAt, time: tr.time, playing: tr.playing, loopOn: tr.loopOn });
    for (const a of acts) {
      log.push(`${nowMs}:${a}`);
      if (a === 'seek') { tr.seeks++; if (tr.seeks > ignoreSeeks) tr.time = p.t; }
      if (a === 'play') { tr.playing = true; tr.plays++; }
      if (a === 'loop') { tr.loopOn = true; tr.loops++; }
    }
  }
  return { p, tr, log };
}

{
  const { p, tr, log } = run({ t: 42, playing: true, loop: null });
  check('playing at the change: plays again at the saved place', tr.playing && Math.abs(tr.time - 42) < 0.2 && p.done, JSON.stringify({ tr, log }));
  const playAt = Number((log.find((x) => x.endsWith(':play')) || '0:').split(':')[0]);
  check('... the seek as the engine comes up, the play once it settled', log[0] === '500:seek' && playAt >= 500 + SETTLE_MS, log.join(' '));
  check('... with one seek and one play', tr.seeks === 1 && tr.plays === 1, JSON.stringify(tr));
}
{
  const { tr, p } = run({ t: 42, playing: false, loop: null });
  check('paused at the change: stays paused at the saved place', !tr.playing && tr.time === 42 && p.done);
}
{
  const { tr } = run({ t: 45, playing: true, loop: { start: 40, end: 50 } });
  check('the loop comes back, and play resumes inside it, not at its start', tr.loopOn && tr.loops === 1 && tr.playing && tr.time >= 45 && tr.time < 45.3, JSON.stringify(tr));
}
{
  // Play pressed right after the engine is up.
  const { tr, p, log } = run({ t: 42, playing: false, loop: null }, {
    user: (i, t) => { if (i * 100 === 600) t.playing = true; },
  });
  const timeAfter = tr.time;
  check('Play pressed right after the engine is up: from the place, then it is theirs', p.done && tr.seeks === 1 && tr.playing && tr.time >= 42, log.join(' '));
  // Keep stepping the transport: nothing more happens.
  const more = restoreStep(p, { nowMs: 5000, engineUp: true, time: timeAfter + 3, playing: true, loopOn: false });
  check('... and nothing snaps back afterwards', more.length === 0);
}
{
  // Play pressed after the restore held: the restore is already over.
  const { p, tr } = run({ t: 42, playing: false, loop: null });
  tr.playing = true;
  const acts = [];
  for (let ms = 2000; ms < 4000; ms += 100) acts.push(...restoreStep(p, { nowMs: ms, engineUp: true, time: 42 + (ms - 2000) / 1000, playing: true, loopOn: false }));
  check('Play right after the reload is never pulled back', acts.length === 0, acts.join(' '));
}
{
  // A seek or loop change of theirs before the restore ran: left alone.
  const { tr, p } = run({ t: 42, playing: true, loop: { start: 40, end: 50 } }, {
    user: (i, t, st) => { if (i * 100 === 600) { st.userActed = true; t.time = 90; } },
  });
  check('a seek of theirs during the settle wins', p.done && tr.time === 90 && tr.seeks === 1 && !tr.playing, JSON.stringify(tr));
}
{
  // A click on Play: the press is seen a step before the play lands.
  const { tr, p } = run({ t: 42, playing: false, loop: null }, {
    user: (i, t, st) => { if (i * 100 === 600) st.userActed = true; if (i * 100 === 700) t.playing = true; },
  });
  check('a click on Play right after the reload plays from the place', p.done && tr.time >= 42 && tr.time < 42.3, JSON.stringify(tr));
}
{
  // An engine that ignores the first seek gets another, then holds.
  const { tr, p } = run({ t: 42, playing: true, loop: null }, { ignoreSeeks: 1 });
  check('a seek that did not hold is tried again', p.done && tr.seeks === 2 && tr.playing && Math.abs(tr.time - 42) < 0.2, JSON.stringify(tr));
}
{
  // An engine that never moves: given up after MAX_SEEKS, not forever.
  const { tr, p } = run({ t: 42, playing: true, loop: null }, { ignoreSeeks: 99 });
  check('gives up after MAX_SEEKS seeks', p.done && tr.seeks === MAX_SEEKS && !tr.playing, JSON.stringify(tr));
}
{
  const { tr, p } = run({ t: 0, playing: true, loop: null });
  check('at 0, no seek is needed: plays again', p.done && tr.seeks === 0 && tr.playing);
}
{
  const { p, log } = run({ t: 42, playing: true, loop: null }, { upAt: 999999, steps: 30 });
  check('no new engine: nothing is done', !p.done && log.length === 0);
}

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
