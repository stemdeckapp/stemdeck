// The sound of a lane's MIDI notes, computed in JS (static/js/midiRender.js):
// a note sounds at its pitch from its start, decays, is silent after its
// release, chords stay under clipping, Bass is low-passed, and the level is
// matched to the stem (midiNotesView.levelScale). Pure, so it runs in node.
//
// Run:  node tests/js/midi-render.test.mjs

import { envelope, fitLevel, lowpassInPlace, renderVoice } from "../../static/js/midiRender.js";
import { levelOf, levelScale, voiceFor } from "../../static/js/midiNotesView.js";

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

const RATE = 8000;
const rms = (x, a, b) => {
  let s = 0;
  for (let i = a; i < b; i++) s += x[i] * x[i];
  return Math.sqrt(s / Math.max(1, b - a));
};
/** The pitch of a periodic stretch, by its strongest autocorrelation lag. */
const hzByLag = (x, a, b, minHz = 30, maxHz = 500) => {
  let best = 0;
  let bestLag = 0;
  for (let lag = Math.floor(RATE / maxHz); lag <= Math.ceil(RATE / minHz); lag++) {
    let s = 0;
    for (let i = a; i + lag < b; i++) s += x[i] * x[i + lag];
    if (s > best) { best = s; bestLag = lag; }
  }
  return bestLag ? RATE / bestLag : 0;
};
/** Rising zero crossings a second: the pitch of a clean tone. */
const hzOf = (x, a, b) => {
  let n = 0;
  for (let i = a + 1; i < b; i++) if (x[i - 1] <= 0 && x[i] > 0) n++;
  return (n * RATE) / (b - a);
};

const keys = voiceFor("piano");
const bass = voiceFor("bass");

let x = await renderVoice([[1, 2, 69, 127]], { ...keys, partials: [{ type: "sine", ratio: 1, gain: 1 }] }, { duration: 3, rate: RATE });
check("the buffer is the song long", x.length === 3 * RATE, String(x.length));
check("silent before the note", rms(x, 0, RATE - 10) === 0);
check("sounding during it", rms(x, RATE + 100, 2 * RATE - 100) > 0.01);
const hz = hzOf(x, RATE + 400, 2 * RATE - 400);
check("at its pitch (A4, 440 Hz)", Math.abs(hz - 440) < 3, String(hz));
check("silent well after its release", rms(x, 2 * RATE + Math.ceil(keys.release * 2 * RATE), 3 * RATE) < 1e-4);
check("no NaN", x.every((v) => v === v));

const env = envelope(RATE, keys, 1, 0.5, RATE);
check("the envelope rises in the attack", env[0] < env[Math.round(keys.attack * RATE) - 1]);
check("reaches the peak", Math.abs(Math.max(...env) - 1) < 1e-6);
check("decays towards the sustain while held", env[Math.round(0.45 * RATE)] < 1 && env[Math.round(0.45 * RATE)] > keys.sustain - 0.05);
check("falls after the release starts", env[RATE - 1] < env[Math.round(0.5 * RATE)] * 0.05);

const chord = [60, 64, 67, 71, 74].map((p) => [0.1, 1.5, p, 127]);
x = await renderVoice(chord, keys, { duration: 2, rate: RATE });
let peak = 0;
for (const v of x) peak = Math.max(peak, Math.abs(v));
check("a five-note chord at full velocity can pass 1 on its own", peak > 0.98, String(peak));
fitLevel(x);
peak = 0;
for (const v of x) peak = Math.max(peak, Math.abs(v));
check("fitLevel keeps it under a peak of 0.98", peak <= 0.98 + 1e-6, String(peak));
const quiet = new Float32Array(RATE).map((_, i) => Math.sin(i / 5) * 0.4);
check("and puts it at the stem's level", Math.abs(fitLevel(quiet, 0.05) - levelScale(levelOf(new Float32Array(RATE).map((_, i) => Math.sin(i / 5) * 0.4)).rms, 0.05, 0.4)) < 1e-9);

x = await renderVoice([[0, 1, 36, 100]], bass, { duration: 1.5, rate: RATE });
check("Bass renders", rms(x, 400, RATE - 400) > 0.01);
const hzBass = hzByLag(x, 800, RATE - 400);
check("at its fundamental (C2, 65.4 Hz)", Math.abs(hzBass - 65.4) < 3, String(hzBass));

const noise = new Float32Array(RATE).map((_, i) => (i % 2 ? 1 : -1));
lowpassInPlace(noise, 200, RATE);
check("the low-pass takes the top of the spectrum away", rms(noise, 400, RATE) < 0.05);

let calls = 0;
await renderVoice([[0, 0.1, 60, 90], [0.2, 0.3, 62, 90]], keys, { duration: 1, rate: RATE, onNote: () => { calls++; } });
check("onNote is called once a note", calls === 2);
x = await renderVoice([[0, 0.5, 60, 0]], keys, { duration: 1, rate: RATE });
check("a velocity 0 note is silent", rms(x, 0, x.length) === 0);

// ─── Matching the stem's level ───

check("no reference: unchanged", levelScale(0.1, 0, 0.5) === 1);
check("a silent rendering: unchanged", levelScale(0, 0.1, 0) === 1);
check("louder than the stem: turned down to it", levelScale(0.16, 0.025, 0.8) === 0.1563);
check("quieter: turned up, under a peak of 0.98", levelScale(0.01, 0.1, 0.5) === 1.96);
check("never more than 4", levelScale(0.001, 1, 0.01) === 4);
check("never under 0.05", levelScale(1, 0.0001, 1) === 0.05);
const lv = levelOf(new Float32Array([0.5, -0.5, 0.5, -0.5]), 1);
check("levelOf gives RMS and peak", Math.abs(lv.rms - 0.5) < 1e-9 && lv.peak === 0.5);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
