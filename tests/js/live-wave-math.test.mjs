// Pure logic behind the live waveform of a take being recorded
// (static/js/capture/liveWaveMath.js): min/max per point in song time, and
// the bar geometry the lane shares with the server's waveform.
//
// The grids here are chosen to be exact in binary (a point of 0.125 s, a rate
// of 16 Hz, two samples per point), so every expected value is exact.
//
// Run:  node tests/js/live-wave-math.test.mjs

import {
  createPeaks, addBlock, takeDirty, barCount, barStartPoint, barEndPoint, barsForPoints,
  barAmplitude, barRect, liveNorm, songSecToX, serverPointSec, parseWavFormat, PEAK_POINTS, MIN_BARS,
} from "../../static/js/capture/liveWaveMath.js";
import { songTimeAtCtx } from "../../static/js/capture/takeMath.js";

let pass = 0;
let fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) { pass++; console.log(`PASS  ${name}`); }
  else { fail++; console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`); }
};
const f32 = (a) => Float32Array.from(a);
const same = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);
const SR = 16;

// ─── Slicing: a click ───
{
  // 1 s of song, 8 points of 0.125 s, 2 samples per point.
  const p = createPeaks(1, 1, 8);
  check("point length is duration / points", p.pointSec === 0.125);
  const x = new Float32Array(16);
  x[5] = 0.75; // point 2
  x[11] = -0.5; // point 5
  const placed = addBlock(p, [x], 0, SR);
  check("every sample inside the song is placed", placed === 16);
  check("click lands in its point's max", p.max[0][2] === 0.75 && p.min[0][2] === 0);
  check("negative click lands in its point's min", p.min[0][5] === -0.5 && p.max[0][5] === 0);
  check("silent points stay [0, 0]", [0, 1, 3, 4, 6, 7].every((j) => p.min[0][j] === 0 && p.max[0][j] === 0));
  check("the level is the loudest |sample|", p.level === 0.75);
  check("every point is filled", p.filledTo === 8);
  check("the take ends at song 1.0", p.lastSongSec === 1);
}

// ─── Slicing: a sine, exact per point ───
{
  // 2 Hz at 16 Hz: 8 samples a period. Float32 values, as the worklet sends.
  const s = f32(Array.from({ length: 16 }, (_, k) => Math.sin((2 * Math.PI * 2 * k) / SR)));
  const p = createPeaks(1, 1, 8);
  addBlock(p, [s], 0, SR);
  const wantMin = [];
  const wantMax = [];
  for (let j = 0; j < 8; j++) {
    wantMin.push(Math.min(0, s[2 * j], s[2 * j + 1]));
    wantMax.push(Math.max(0, s[2 * j], s[2 * j + 1]));
  }
  check("sine: min per point is exact", same([...p.min[0]], f32(wantMin)), `${[...p.min[0]]}`);
  check("sine: max per point is exact", same([...p.max[0]], f32(wantMax)), `${[...p.max[0]]}`);
  // Point 1 holds samples 2 and 3: sin(pi/2) = 1 and sin(3pi/4).
  check("sine: the crest is in point 1", p.max[0][1] === 1);
  check("sine: the trough is in point 3", p.min[0][3] === -1);
}

// ─── Slicing: one block split over two calls gives the same points ───
{
  const s = f32(Array.from({ length: 16 }, (_, k) => Math.sin((2 * Math.PI * 3 * k) / SR) * 0.8));
  const whole = createPeaks(1, 1, 8);
  addBlock(whole, [s], 0, SR);
  const split = createPeaks(1, 1, 8);
  addBlock(split, [s.subarray(0, 7)], 0, SR);
  addBlock(split, [s.subarray(7)], 7 / SR, SR);
  check("batches split mid-point give the same min", same([...whole.min[0]], [...split.min[0]]));
  check("batches split mid-point give the same max", same([...whole.max[0]], [...split.max[0]]));
}

// ─── Song-time placement ───
{
  // A count-in: the block starts at song -0.25, so its first 4 samples (the
  // count-in) are not drawn and sample 4 is song 0.
  const p = createPeaks(1, 1, 8);
  const x = new Float32Array(8);
  x[3] = 0.9; // song -0.0625: dropped
  x[4] = 0.4; // song 0: point 0
  x[7] = -0.3; // song 0.1875: point 1
  const placed = addBlock(p, [x], -0.25, SR);
  check("count-in samples are not placed", placed === 4);
  check("sample at song 0 is point 0", p.max[0][0] === Math.fround(0.4));
  check("the dropped count-in click is not in the level", p.level === Math.fround(0.4));
  check("song 0.1875 is point 1", p.min[0][1] === Math.fround(-0.3));
  check("drawn extent covers points 0 and 1", p.filledTo === 2);
  check("last song time is the block end", p.lastSongSec === 0.25);
}
{
  // Recording started from 0.5 s in: the first point drawn is point 4, and
  // nothing earlier counts as filled.
  const p = createPeaks(1, 1, 8);
  const x = new Float32Array(4).fill(0.25);
  addBlock(p, [x], 0.5, SR);
  check("start offset places the first sample at point 4", p.max[0][4] === 0.25 && p.max[0][3] === 0);
  check("filledTo is one past the last point", p.filledTo === 6);
  check("bars before the take start are blank", barAmplitude(p, 0, 4) === 0);
  check("bars past the take are blank (-1)", barAmplitude(p, 3, 4) === -1);
}
{
  // Past the end of the song: placed up to the end, then stops.
  const p = createPeaks(1, 1, 8);
  const placed = addBlock(p, [new Float32Array(8).fill(0.1)], 0.75, SR);
  check("samples past the song end are not placed", placed === 4);
  check("last song time is clamped to the song end", p.lastSongSec === 1);
}
{
  // The engine mapping that places the take (takeMath.songTimeAtCtx): song
  // started at ctx 10, round trip 1/16 s. A batch stamped at context frame
  // 160 (ctx 10.0) starts at song -0.0625: its first sample is dropped.
  const m = { startCtxTime: 10, startOffset: 0, rate: 1, pipelineLatencySeconds: 0 };
  const start = songTimeAtCtx(160 / SR, m, 1 / SR);
  check("batch start from the engine mapping", start === -0.0625);
  const p = createPeaks(1, 1, 8);
  const x = new Float32Array(4);
  x[1] = 0.6;
  addBlock(p, [x], start, SR);
  check("the sample one round trip late lands on song 0", p.max[0][0] === Math.fround(0.6));
}

// ─── Channels ───
{
  const p = createPeaks(1, 2, 8);
  const l = new Float32Array(4);
  const r = new Float32Array(4);
  l[0] = 0.2;
  r[0] = -0.9;
  addBlock(p, [l, r], 0, SR);
  check("each channel has its own points", p.max[0][0] === Math.fround(0.2) && p.min[1][0] === Math.fround(-0.9) && p.min[0][0] === 0);
  check("the level follows channel 0 (the server's peaks are channel 0)", p.level === Math.fround(0.2));
}

// ─── Dirty range ───
{
  const p = createPeaks(1, 1, 8);
  const out = [0, 0];
  check("nothing dirty at first", takeDirty(p, out) === null);
  addBlock(p, [new Float32Array(3)], 0.25, SR);
  check("dirty range is the points written", takeDirty(p, out) === out && out[0] === 2 && out[1] === 3);
  check("dirty range resets after reading", takeDirty(p, out) === null);
}

// ─── Bars, the same grid as player.js barsWaveformSvg ───
{
  check("bar count is width / 5", barCount(1000) === 200);
  check("bar count has a floor", barCount(100) === MIN_BARS);
  check("bar count before layout", barCount(0) === 220);
  check("bar 1 of 4 over 8 points is points 2..3", barStartPoint(1, 4, 8) === 2 && barEndPoint(1, 4, 8) === 4);
  check("more bars than points: each bar has at least one point", barEndPoint(5, 16, 8) === 3 && barStartPoint(5, 16, 8) === 2);

  const p = createPeaks(1, 1, 8);
  const x = new Float32Array(16);
  x[2] = 0.3; // point 1, bar 0
  x[5] = -0.6; // point 2, bar 1
  addBlock(p, [x], 0, SR);
  check("bar amplitude is max(-min, max) of its points", barAmplitude(p, 0, 4) === f32([0.3])[0] && barAmplitude(p, 1, 4) === f32([0.6])[0]);

  const r = [0, 0, 0, 0];
  barRect(1, 4, 20, 48, 0.5, 2, r);
  check("bar x is (i + 0.2) slots, width 0.6 of a slot", r[0] === 6 && r[2] === 3);
  check("full-scale bar is 44 of 48, centred", r[1] === 2 && r[3] === 44);
  barRect(0, 4, 20, 96, 0, 2, r);
  check("a silent bar keeps the 0.7 unit baseline", r[3] === 1.4 && r[1] === 47.3);
  barRect(0, 4, 20, 48, 0.25, 2, r);
  check("half scale is 22 units", r[3] === 22 && r[1] === 13);
  barRect(0, 4, 20, 48, 2, 2, r);
  check("over full scale is clamped", r[3] === 44);
}

// ─── Every changed point is redrawn ───
{
  let missed = 0;
  const out = [0, 0];
  for (const bars of [7, 40, 333, 2999, 3000, 4100]) {
    for (let lo = 0; lo < PEAK_POINTS; lo += 97) {
      const hi = Math.min(PEAK_POINTS - 1, lo + (lo % 5));
      barsForPoints(lo, hi, bars, PEAK_POINTS, out);
      for (let i = 0; i < bars; i++) {
        const s = barStartPoint(i, bars, PEAK_POINTS);
        const e = barEndPoint(i, bars, PEAK_POINTS);
        const touches = s <= hi && e > lo;
        if (touches && (i < out[0] || i > out[1])) missed++;
      }
    }
  }
  check("barsForPoints covers every bar holding a changed point", missed === 0, `${missed} missed`);
}

// ─── Normalisation and x ───
{
  check("norm is over the loudest of the other lanes and the take", liveNorm(0.5, 0.25) === 2 && liveNorm(0.25, 0.5) === 2);
  check("norm is 0 when everything is silent", liveNorm(0, 0) === 0);
  check("song time to x is linear over the lane", songSecToX(1.5, 6, 1200) === 300);
}

// ─── The server's grid ───
{
  // The e2e fixture: 6 s at 44.1 kHz is 264600 frames, blocks of 88 (not 88.2).
  check("server point is floor(frames / points) samples", serverPointSec(264600, 44100) === 88 / 44100);
  check("a short file still has one-sample points", serverPointSec(100, 48000) === 1 / 48000);
  check("no format, no grid", serverPointSec(0, 44100) === 0);
  // On that grid, song 3.0 s is point 1503, not 1500.
  const p = createPeaks(6, 1, PEAK_POINTS, serverPointSec(264600, 44100));
  addBlock(p, [Float32Array.of(0.5)], 3, 44100);
  check("song 3 s lands where the server puts it", p.max[0][1503] === 0.5);
}
{
  const wav = (extra) => {
    const buf = new ArrayBuffer(44 + extra);
    const v = new DataView(buf);
    const put = (off, str) => { for (let i = 0; i < 4; i++) v.setUint8(off + i, str.charCodeAt(i)); };
    put(0, "RIFF"); put(8, "WAVE");
    put(12, "fmt "); v.setUint32(16, 16, true);
    v.setUint16(20, 1, true); v.setUint16(22, 2, true);
    v.setUint32(24, 48000, true); v.setUint32(28, 192000, true);
    v.setUint16(32, 4, true); v.setUint16(34, 16, true);
    let off = 36;
    if (extra) { put(off, "LIST"); v.setUint32(off + 4, extra - 8, true); off += extra; }
    put(off, "data"); v.setUint32(off + 4, 4 * 1000, true);
    return buf;
  };
  const a = parseWavFormat(wav(0));
  check("WAV format: rate and frames", a && a.sampleRate === 48000 && a.frames === 1000, JSON.stringify(a));
  const b = parseWavFormat(wav(26));
  check("WAV format: a chunk before data is skipped", b && b.sampleRate === 48000 && b.frames === 1000, JSON.stringify(b));
  check("not a WAV", parseWavFormat(new ArrayBuffer(64)) === null);
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
