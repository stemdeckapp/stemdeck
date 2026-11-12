// Pure logic behind recording a take (static/js/capture/takeMath.js,
// takeRecorder.buildSidecar, takeUpload.AudioPartQueue).
//
// The placement numbers are the contract with the backend's renderer: a frame
// stamped at context time c lands at song time
//   (c - roundTrip - startCtxTime - pipelineLatency) * rate + startOffset,
// and the nudge is NOT folded in here (the server applies it). Calibration is
// checked on synthetic captures with a known delay and noise.
//
// Run:  node tests/js/take-math.test.mjs

import {
  songTimeAtCtx, firstSampleSongSec, endSongSec, placementSec, perfMsAtCtx,
  estimateRoundTripSec, median, makeBurst, findBurst, measureRoundTrips,
  summariseCalibration, interleave, maxFramesPerPart, recordBlockReason,
  CAL_CLICKS, CAL_SPACING_SEC, PART_MAX_BYTES,
  keepUnplacedTake, estimatedMapping, KEEP_UNPLACED_MIN_SEC,
} from "../../static/js/capture/takeMath.js";
import { buildSidecar } from "../../static/js/capture/takeRecorder.js";
import { AudioPartQueue, uploadPart } from "../../static/js/capture/takeUpload.js";

let pass = 0;
let fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) { pass++; console.log(`PASS  ${name}`); }
  else { fail++; console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`); }
};
const approx = (a, b, eps = 1e-9) => Math.abs(a - b) <= eps;

// ─── Placement ───
{
  // Song started at ctx 10.0 from song 0, no worklet latency, tempo 1.
  const m = { startCtxTime: 10, startOffset: 0, rate: 1, pipelineLatencySeconds: 0 };
  check("frame at the start, no latency, is song 0", approx(songTimeAtCtx(10, m, 0), 0));
  check("round trip is subtracted (late input moves earlier)", approx(songTimeAtCtx(10.02, m, 0.02), 0));
  // Count-in: recording began 2 s before the song; frame 0 is at song -2.
  check("count-in frames are negative song time", approx(firstSampleSongSec(8, m, 0), -2));
  check("count-in plus round trip", approx(firstSampleSongSec(8, m, 0.025), -2.025));
  // Started from 30 s into the song.
  const m30 = { ...m, startOffset: 30 };
  check("start offset is added", approx(songTimeAtCtx(11, m30, 0), 31));
  // The tempo worklet delays what is heard by its latency.
  const mp = { ...m, pipelineLatencySeconds: 0.0029 };
  check("pipeline latency is subtracted", approx(songTimeAtCtx(10.0029, mp, 0), 0));
  check("pipeline and round trip together", approx(songTimeAtCtx(10.0329, mp, 0.03), 0));
  // Same formula as the engine's now(): at a time after the start, both agree.
  const engineNow = (c, mm) => Math.max(0, c - mm.startCtxTime - mm.pipelineLatencySeconds) * mm.rate + mm.startOffset;
  check("matches the engine's heard playhead after start", approx(songTimeAtCtx(14.5, mp, 0), engineNow(14.5, mp)));
  check("end song time from frame count", approx(endSongSec(-2, 48000 * 10, 48000), 8));
  check("placement applies the nudge (positive = earlier)", approx(placementSec(1.0, 20), 0.98));
  check("perf time of a ctx time", approx(perfMsAtCtx(5.5, { contextTime: 5, performanceTime: 1000 }), 1500));
  check("perf time is null without a timestamp", perfMsAtCtx(5, null) === null);
}

// ─── Estimate and median ───
{
  check("estimate sums output, base and input", approx(estimateRoundTripSec({ outputLatency: 0.01, baseLatency: 0.005, trackLatency: 0.01 }), 0.025));
  check("estimate defaults input to 10 ms", approx(estimateRoundTripSec({ baseLatency: 0.005 }), 0.015));
  check("estimate ignores junk", approx(estimateRoundTripSec({ outputLatency: NaN, baseLatency: -1 }), 0.01));
  check("median odd", median([3, 1, 2]) === 2);
  check("median even", median([4, 1, 3, 2]) === 2.5);
  check("median skips non-finite", median([1, NaN, 3]) === 2);
  check("median of nothing is NaN", Number.isNaN(median([])));
}

// ─── Calibration on synthetic captures ───
function synth({ rate = 48000, delaySec = 0.0234, noise = 0.01, gain = 0.5, missing = [], jitter = [], seed = 7 } = {}) {
  const burst = makeBurst(rate);
  const startFrame = 1000; // the capture begins at context frame 1000
  const t0 = startFrame + Math.round(0.3 * rate);
  const clickFrames = Array.from({ length: CAL_CLICKS }, (_, k) => t0 + Math.round(k * CAL_SPACING_SEC * rate));
  const len = Math.round(4 * rate);
  const sig = new Float32Array(len);
  let s = seed;
  const rnd = () => { s = (s * 1103515245 + 12345) & 0x7fffffff; return s / 0x7fffffff * 2 - 1; };
  for (let i = 0; i < len; i++) sig[i] = rnd() * noise;
  clickFrames.forEach((cf, k) => {
    if (missing.includes(k)) return;
    const at = cf - startFrame + Math.round((delaySec + (jitter[k] || 0)) * rate);
    for (let i = 0; i < burst.length && at + i < len; i++) sig[at + i] += burst[i] * gain;
  });
  return { sig, rate, startFrame, clickFrames, burst };
}

{
  const b = makeBurst(48000);
  check("burst is 2 ms", b.length === 96);
  check("burst is deterministic", makeBurst(48000)[10] === b[10]);
  const sig = new Float32Array(2000);
  sig.set(b, 700);
  const hit = findBurst(sig, b, 0, 1800);
  check("findBurst finds an exact copy", hit && hit.lag === 700, JSON.stringify(hit));
}
{
  const { sig, rate, startFrame, clickFrames, burst } = synth({ delaySec: 0.0234 });
  const rts = measureRoundTrips(sig, rate, startFrame, clickFrames, burst);
  check("all five clicks found", rts.every((r) => r !== null), JSON.stringify(rts));
  const res = summariseCalibration(rts);
  check("calibration accepted", res.ok, JSON.stringify(res));
  check("round trip within 1 sample of 23.4 ms", Math.abs(res.roundTripSec - 0.0234) <= 1 / rate + 1e-9, String(res.roundTripSec));
  check("reported in ms to 0.1", Math.abs(res.roundTripMs - 23.4) <= 0.05, String(res.roundTripMs));
  check("no drift on a steady loop", res.driftPpm === 0, String(res.driftPpm));
}
{
  const { sig, rate, startFrame, clickFrames, burst } = synth({ rate: 44100, delaySec: 0.0612, noise: 0.03, gain: 0.3 });
  const res = summariseCalibration(measureRoundTrips(sig, rate, startFrame, clickFrames, burst));
  check("44.1 kHz, noisier, 61.2 ms", res.ok && Math.abs(res.roundTripSec - 0.0612) <= 1 / rate + 1e-9, JSON.stringify(res));
}
{
  // One click lost and one far off: four agree, so it is still accepted.
  const { sig, rate, startFrame, clickFrames, burst } = synth({ missing: [2] });
  const res = summariseCalibration(measureRoundTrips(sig, rate, startFrame, clickFrames, burst));
  check("one missing click still calibrates", res.ok && res.agreeing === 4, JSON.stringify(res));
}
{
  const { sig, rate, startFrame, clickFrames, burst } = synth({ gain: 0 });
  const res = summariseCalibration(measureRoundTrips(sig, rate, startFrame, clickFrames, burst));
  check("silence is too quiet", !res.ok && res.reason === "too-quiet", JSON.stringify(res));
}
{
  // Spread far apart: no four agree within 2 ms.
  const res = summariseCalibration([0.020, 0.030, 0.040, 0.050, 0.060]);
  check("disagreeing clicks are inconsistent", !res.ok && res.reason === "inconsistent", JSON.stringify(res));
  const ok = summariseCalibration([0.0230, 0.0231, 0.0229, 0.0230, 0.0400]);
  check("an outlier is dropped", ok.ok && Math.abs(ok.roundTripSec - 0.0230) < 1e-9, JSON.stringify(ok));
  const drift = summariseCalibration([0.0230, 0.02301, 0.02302, 0.02303, 0.02304]);
  check("drift is the slope in ppm", drift.ok && drift.driftPpm === 17, String(drift.driftPpm));
}

// ─── PCM packing ───
{
  const l = Float32Array.from([1, 2, 3]);
  const r = Float32Array.from([-1, -2, -3]);
  const st = interleave([l, r], 3);
  check("stereo interleaves L R L R", st.join(",") === "1,-1,2,-2,3,-3");
  check("mono passes through", interleave([l], 2).join(",") === "1,2");
  check("frames per part keep under 4 MB", maxFramesPerPart(2) * 8 <= PART_MAX_BYTES && (maxFramesPerPart(2) + 1) * 8 > PART_MAX_BYTES);
  check("Record refuses other tempos", recordBlockReason({ hasEngine: true, rate: 0.75 }) === "tempo");
  check("Record needs the engine", recordBlockReason({ hasEngine: false, rate: 1 }) === "no-engine");
  check("Record allowed at 1x", recordBlockReason({ hasEngine: true, rate: 1 }) === null);
}

// ─── Sidecar ───
{
  const capture = {
    roundTripSeconds: 0.0234, calibrated: true, sampleRate: 48000, channels: 1,
    deviceLabel: "Scarlett 2i2", calibration: { driftPpm: 3 },
    estimate: { outputLatencyMs: 10, baseLatencyMs: 5.3, inputLatencyMs: null },
  };
  const mapping = { startCtxTime: 43.62, startOffset: 0, rate: 1, pipelineLatencySeconds: 0.0029, epoch: 4 };
  const sc = buildSidecar({
    label: "", mapping, ctxTimeAtFrame0: 41.27, perfMsAtFrame0: 123456.7, frames: 48000 * 60,
    capture, dropped: 0, video: null,
  });
  check("sidecar v1", sc.v === 1);
  check("first sample folds the round trip and pipeline",
    approx(sc.timeline.first_sample_song_sec, 41.27 - 0.0234 - 43.62 - 0.0029, 1e-6), String(sc.timeline.first_sample_song_sec));
  check("nudge starts at 0 and is not folded in", sc.latency.user_nudge_ms === 0);
  check("tempo is 1", sc.timeline.tempo === 1);
  check("count-in length", approx(sc.timeline.count_in_sec, 2.35, 1e-6), String(sc.timeline.count_in_sec));
  check("calibrated source", sc.latency.source === "calibrated");
  check("unknown input latency is absent, not null", !("input_latency_ms_reported" in sc.latency));
  check("no label key when empty", !("label" in sc));
  check("no video block without a camera", !("video" in sc));
  check("capture block", sc.capture.rate === 48000 && sc.capture.channels === 1 && sc.capture.device_label === "Scarlett 2i2");
  const withVideo = buildSidecar({ label: "Take 2", mapping, ctxTimeAtFrame0: 41.27, perfMsAtFrame0: 1, frames: 10, capture: { ...capture, calibrated: false }, dropped: 2, video: { mime: "video/webm" } });
  check("video block passes through", withVideo.video?.mime === "video/webm");
  check("estimated source when not calibrated", withVideo.latency.source === "estimated");
  check("label passes through", withVideo.label === "Take 2");
}

// ─── Upload queue against a fake server ───
async function uploads() {
  const log = [];
  let failNext = 0;
  let conflictOnce = false;
  let serverNext = 0;
  globalThis.fetch = async (url, opts) => {
    const m = /parts\/(\w+)\?seq=(\d+)/.exec(url);
    if (opts.method === "GET" && /\/parts$/.test(url)) {
      return new Response(JSON.stringify({ audio: { next_seq: serverNext }, camera: { next_seq: serverNext } }), { status: 200 });
    }
    if (!m) return new Response("{}", { status: 404 });
    const seq = Number(m[2]);
    const size = opts.body.byteLength;
    log.push({ seq, size });
    if (failNext > 0) { failNext--; return new Response("{}", { status: 503 }); }
    if (conflictOnce) {
      conflictOnce = false;
      serverNext = seq + 1; // the server stored it; the reply was lost
      return new Response(JSON.stringify({ detail: "unexpected part sequence" }), { status: 409 });
    }
    serverNext = seq + 1;
    return new Response(JSON.stringify({ seq, bytes: size, duplicate: false }), { status: 200 });
  };

  const q = new AudioPartQueue({ channels: 2, flushMs: 100000 });
  const block = new Float32Array(48000 * 2); // 1 s stereo, 384 KB
  q.push(block); // before the take id: held
  check("nothing sent before the take id", log.length === 0);
  q.setTake("e2e0deadbeef", "t0011aabb");
  for (let i = 0; i < 12; i++) q.push(block); // 13 s total, about 5 MB
  failNext = 1; // the next request fails once and is retried
  const sent = await q.drain();
  const seqs = log.map((l) => l.seq);
  check("every part under 4 MB", log.every((l) => l.size <= PART_MAX_BYTES), JSON.stringify(log));
  check("parts are a whole number of frames", log.every((l) => l.size % 8 === 0));
  check("all bytes acknowledged", sent === 13 * block.byteLength, `${sent}`);
  check("seq goes up by one, retry repeats the same seq",
    seqs.filter((v, i) => seqs.indexOf(v) === i).every((v, i) => v === i), seqs.join(","));

  log.length = 0;
  serverNext = 0;
  conflictOnce = true;
  const ack = await uploadPart("e2e0deadbeef", "t0011aabb", "camera", 0, new Uint8Array(10));
  check("a 409 for a part the server already has counts as acknowledged", ack.duplicate === true, JSON.stringify(ack));

  let threw = false;
  try { await uploadPart("e2e0deadbeef", "t0011aabb", "camera", 0, new Uint8Array(PART_MAX_BYTES + 1)); } catch { threw = true; }
  check("a part over 4 MB is refused before sending", threw);
}

// ─── A take that never read the engine's clock ───
{
  // Judged by the audio captured, never by the missing clock: a four minute
  // take whose engine went away used to be deleted as "too short".
  check("four minutes of audio are kept", keepUnplacedTake(4 * 60 * 48000, 48000));
  check("half a second is kept", keepUnplacedTake(KEEP_UNPLACED_MIN_SEC * 44100, 44100));
  check("a double click's few frames are not", !keepUnplacedTake(4096, 48000));
  check("no rate, nothing to judge: not kept", !keepUnplacedTake(48000, 0));

  // Frame 0 lands where the playhead stood, less the round trip, like a
  // take placed by the clock with no count-in.
  const m = estimatedMapping(12.5, 30, 0.02);
  check("estimated: frame 0 at the playhead", approx(firstSampleSongSec(12.5, m, 0.02), 30));
  check("estimated: later frames follow at tempo 1", approx(songTimeAtCtx(14.5, m, 0.02), 32));
  check("estimated mapping says so", m.estimated === true && m.epoch === null);
  check("a bad playhead falls back to the top", estimatedMapping(1, NaN, 0).startOffset === 0);
  check("a negative playhead is clamped to the top", estimatedMapping(1, -3, 0).startOffset === 0);
  const sc = buildSidecar({
    mapping: m, ctxTimeAtFrame0: 12.5, perfMsAtFrame0: 1000, frames: 48000 * 3,
    capture: { sampleRate: 48000, channels: 1, roundTripSeconds: 0.02, calibrated: false, estimate: {} },
    dropped: 0,
  });
  check("estimated sidecar: first sample at the playhead", approx(sc.timeline.first_sample_song_sec, 30, 1e-6), JSON.stringify(sc.timeline));
  check("estimated sidecar: no count-in invented", sc.timeline.count_in_sec === 0, JSON.stringify(sc.timeline));
  check("estimated sidecar: three seconds long", approx(sc.timeline.end_song_sec - sc.timeline.first_sample_song_sec, 3, 1e-6));
}

await uploads();

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
