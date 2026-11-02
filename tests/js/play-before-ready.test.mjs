// Play pressed while a song's stems are still loading (found by
// tests/e2e/loop-transport.spec.mjs). The transport shows the song's length
// from its peaks before either engine has read its stems, so Play works in
// that window. The streaming engine then started with a duration of 0, took
// its first tick at position 0 for the end of the track, played nothing and
// fired onEnded, which in Karaoke moved the queue on to the next song.
//
// Both engines now hold a Play pressed before ready() and start once it has
// settled; only a known, positive duration has an end (playbackEnd.js).
//
// The stem downloads are held behind a gate here, which is what makes the
// loading window wide enough to press Play in every time.
//
// Run:  node tests/js/play-before-ready.test.mjs
//
// Against the engines before the fix this fails the "no onEnded" and "starts
// once loaded" checks for the streaming engine, and the "every stem" check
// for the full-decode engine, which started with only the stem decoded first.

import { reachedEnd } from "../../static/js/playbackEnd.js";

let pass = 0, fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) { pass++; console.log(`PASS  ${name}`); }
  else { fail++; console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`); }
};
process.on("unhandledRejection", (e) => { console.error("UNHANDLED:", e); process.exitCode = 1; });

// --------------------------------------------------------------------------
// reachedEnd
// --------------------------------------------------------------------------

check("end: position 0 of an unknown (0) duration is not an end", reachedEnd(0, 0) === false);
check("end: any position of a 0 duration is not an end", reachedEnd(5, 0) === false);
check("end: a negative duration has no end", reachedEnd(0, -1) === false);
check("end: NaN duration has no end", reachedEnd(1, NaN) === false);
check("end: Infinity duration has no end", reachedEnd(1e9, Infinity) === false);
check("end: before a known duration is not an end", reachedEnd(5.99, 6) === false);
check("end: at a known duration is the end", reachedEnd(6, 6) === true);
check("end: past a known duration is the end", reachedEnd(6.2, 6) === true);
check("end: NaN position is not an end", reachedEnd(NaN, 6) === false);

// --------------------------------------------------------------------------
// Fakes: a gated server and an AudioContext that records what was started
// --------------------------------------------------------------------------

const SR = 44100, CH = 2, SECONDS = 6;

function wav(seconds = SECONDS) {
  const audio = Buffer.alloc(Math.round(SR * seconds) * CH * 2);
  const fmt = Buffer.alloc(24);
  fmt.write("fmt ", 0, 4, "ascii");
  fmt.writeUInt32LE(16, 4);
  fmt.writeUInt16LE(1, 8);
  fmt.writeUInt16LE(CH, 10);
  fmt.writeUInt32LE(SR, 12);
  fmt.writeUInt32LE(SR * CH * 2, 16);
  fmt.writeUInt16LE(CH * 2, 20);
  fmt.writeUInt16LE(16, 22);
  const data = Buffer.alloc(8);
  data.write("data", 0, 4, "ascii");
  data.writeUInt32LE(audio.length, 4);
  const body = Buffer.concat([Buffer.from("WAVE", "ascii"), fmt, data, audio]);
  const riff = Buffer.alloc(8);
  riff.write("RIFF", 0, 4, "ascii");
  riff.writeUInt32LE(body.length, 4);
  return Buffer.concat([riff, body]);
}

const FILES = new Map();
let gate = Promise.resolve();
let release = () => {};
function hold() { gate = new Promise((r) => { release = r; }); }

globalThis.fetch = async (url, opts = {}) => {
  await gate;
  const file = FILES.get(url);
  if (!file) return { ok: false, status: 404, headers: { get: () => null }, arrayBuffer: async () => new ArrayBuffer(0) };
  const m = /bytes=(\d+)-(\d+)/.exec(opts.headers?.Range || "");
  const start = m ? Number(m[1]) : 0;
  const end = m ? Math.min(Number(m[2]), file.length - 1) : file.length - 1;
  const slice = file.subarray(start, end + 1);
  return {
    ok: true,
    status: m ? 206 : 200,
    headers: { get: (h) => (h === "Content-Range" ? `bytes ${start}-${end}/${file.length}` : null) },
    arrayBuffer: async () => slice.buffer.slice(slice.byteOffset, slice.byteOffset + slice.byteLength),
  };
};

let started = []; // every buffer source started: { when, offset }
const node = () => ({
  gain: { value: 1, setTargetAtTime() {}, cancelScheduledValues() {}, setValueAtTime() {}, linearRampToValueAtTime() {} },
  connect() {}, disconnect() {},
});
let decodeGate = Promise.resolve();
const decodeOrder = [];
class FakeCtx {
  constructor() { this.currentTime = 0; this.destination = {}; this.state = "running"; this.audioWorklet = null; this.sampleRate = SR; }
  addEventListener() {}
  removeEventListener() {}
  createGain() { return node(); }
  createAnalyser() { return { fftSize: 0, connect() {}, disconnect() {} }; }
  createBuffer(ch, len, rate) {
    return { numberOfChannels: ch, length: len, sampleRate: rate, duration: len / rate,
             getChannelData: () => new Float32Array(len) };
  }
  createBufferSource() {
    return { buffer: null, playbackRate: { value: 1 }, connect() {}, disconnect() {}, stop() {},
             start(when, offset) { started.push({ when, offset }); } };
  }
  // The full-decode engine's decoder: the first stem decodes at once, the
  // rest wait on decodeGate, which is the window a Play used to land in.
  async decodeAudioData(bytes) {
    const n = decodeOrder.push(bytes.byteLength);
    if (n > 1) await decodeGate;
    return this.createBuffer(CH, SR * SECONDS, SR);
  }
  async close() {}
}
globalThis.window = { AudioContext: FakeCtx };

const { createChunkedAudioEngine } = await import("../../static/js/chunkedAudioEngine.js");
const { createAudioEngine } = await import("../../static/js/audioEngine.js");

// Without requestAnimationFrame the tick loop runs on a 100 ms timer, so
// 350 ms is three ticks: the old bug ended the song on the first.
const ticks = () => new Promise((r) => setTimeout(r, 350));
const STEMS = [{ name: "vocals", url: "/s/vocals.wav" }, { name: "bass", url: "/s/bass.wav" }];
FILES.set("/s/vocals.wav", wav());
FILES.set("/s/bass.wav", wav());

function chunked(stems = STEMS) {
  const ev = { ended: 0 };
  const eng = createChunkedAudioEngine(stems, { onEnded: () => ev.ended++ });
  return { eng, ev };
}

// --------------------------------------------------------------------------
// Streaming (chunked) engine
// --------------------------------------------------------------------------

{
  started = [];
  hold();
  const { eng, ev } = chunked();
  const p = eng.play();
  await ticks();
  check("chunked: Play while loading fires no onEnded", ev.ended === 0, `ended ${ev.ended}`);
  check("chunked: Play while loading reads as pressed", eng.isPlaying() === true);
  check("chunked: the clock is not ready while it waits", eng.isClockReady() === false);
  check("chunked: nothing started while it waits", started.length === 0, `${started.length} sources`);
  release();
  const ok = await p;
  check("chunked: play() resolves true once loaded", ok === true, String(ok));
  check("chunked: starts once loaded, every stem", started.length === 2, `${started.length} sources`);
  check("chunked: the clock is ready after the start", eng.isClockReady() === true);
  await ticks();
  check("chunked: still no onEnded after the start", ev.ended === 0, `ended ${ev.ended}`);
  eng.destroy();
}

{
  started = [];
  hold();
  const { eng, ev } = chunked();
  const p = eng.play();
  eng.pause();
  check("chunked: Pause while waiting reads as paused", eng.isPlaying() === false);
  release();
  check("chunked: a paused wait resolves false", (await p) === false);
  await ticks();
  check("chunked: a paused wait starts nothing", started.length === 0, `${started.length} sources`);
  check("chunked: a paused wait fires no onEnded", ev.ended === 0);
  eng.destroy();
}

{
  started = [];
  hold();
  const { eng } = chunked();
  const first = eng.play();
  eng.pause();
  const second = eng.play();
  release();
  const [a, b] = await Promise.all([first, second]);
  check("chunked: Play, Pause, Play while loading: the stale wait resolves false", a === false);
  check("chunked: Play, Pause, Play while loading: the live one starts", b === true);
  check("chunked: Play, Pause, Play while loading: started once, not twice", started.length === 2, `${started.length} sources`);
  eng.destroy();
}

{
  started = [];
  hold();
  const { eng, ev } = chunked();
  const p = eng.play();
  eng.destroy(); // a song change
  release();
  check("chunked: a wait on a destroyed engine resolves false", (await p) === false);
  await ticks();
  check("chunked: a destroyed engine's wait starts nothing", started.length === 0);
  check("chunked: a destroyed engine's wait fires no onEnded", ev.ended === 0);
}

{
  started = [];
  hold();
  const { eng } = chunked();
  eng.seek(3); // the transport snaps to the loop start before it plays
  const p = eng.play();
  release();
  await p;
  check("chunked: a seek made while loading is where it starts", Math.abs(eng.getCurrentTime() - 3) < 1e-9, String(eng.getCurrentTime()));
  check("chunked: ...inside its chunk", started.every((s) => Math.abs(s.offset - 3) < 1e-9), JSON.stringify(started));
  eng.destroy();
}

{
  started = [];
  hold();
  const { eng } = chunked();
  eng.seek(100);
  release();
  await eng.ready;
  check("chunked: a seek past the end made while loading is clamped once known",
    eng.getCurrentTime() === eng.getDuration(), `${eng.getCurrentTime()} of ${eng.getDuration()}`);
  eng.destroy();
}

{
  // A load that fails still reaches the error path, and the waiting Play
  // ends quietly rather than spinning or ending the song.
  started = [];
  hold();
  const { eng, ev } = chunked([{ name: "vocals", url: "/s/missing.wav" }]);
  const p = eng.play();
  release();
  check("chunked: failed load, ready() is false", (await eng.ready) === false);
  check("chunked: failed load, a waiting Play resolves false", (await p) === false);
  check("chunked: failed load, the error is reported", eng.getLoadError() === "player.loadError.unreachable", eng.getLoadError());
  check("chunked: failed load, reads as not playing", eng.isPlaying() === false);
  await ticks();
  check("chunked: failed load, no onEnded", ev.ended === 0);
  check("chunked: failed load, nothing started", started.length === 0);
  eng.destroy();
}

{
  // Ready track: the same start as ever, at once, from the cached chunk.
  started = [];
  gate = Promise.resolve();
  const { eng, ev } = chunked();
  await eng.ready;
  await new Promise((r) => setTimeout(r, 50)); // chunk 0 lands in the cache
  const p = eng.play();
  check("chunked: a ready track starts synchronously", started.length === 2, `${started.length} sources`);
  check("chunked: a ready track resolves true", (await p) === true);
  check("chunked: a ready track fires no onEnded on start", ev.ended === 0);
  eng.destroy();
}

// --------------------------------------------------------------------------
// Full-decode engine
// --------------------------------------------------------------------------

{
  started = [];
  decodeOrder.length = 0;
  let releaseDecode;
  decodeGate = new Promise((r) => { releaseDecode = r; });
  gate = Promise.resolve();
  const ev = { ended: 0 };
  const eng = createAudioEngine(STEMS, { onEnded: () => ev.ended++ });
  // Let the first stem decode, so the old engine had one track and no others.
  await new Promise((r) => setTimeout(r, 50));
  const p = eng.play();
  check("fulldecode: Play while loading reads as pressed", eng.isPlaying() === true);
  check("fulldecode: nothing started while it waits", started.length === 0, `${started.length} sources`);
  releaseDecode();
  check("fulldecode: play() resolves true once loaded", (await p) === true);
  check("fulldecode: starts every stem, not just the first decoded", started.length === 2, `${started.length} sources`);
  await ticks();
  check("fulldecode: no onEnded", ev.ended === 0);
  eng.destroy();
}

{
  started = [];
  decodeOrder.length = 0;
  let releaseDecode;
  decodeGate = new Promise((r) => { releaseDecode = r; });
  const eng = createAudioEngine(STEMS, {});
  const p = eng.play();
  eng.pause();
  check("fulldecode: Pause while waiting reads as paused", eng.isPlaying() === false);
  releaseDecode();
  check("fulldecode: a paused wait resolves false", (await p) === false);
  check("fulldecode: a paused wait starts nothing", started.length === 0);
  eng.destroy();
}

{
  started = [];
  decodeOrder.length = 0;
  let releaseDecode;
  decodeGate = new Promise((r) => { releaseDecode = r; });
  const eng = createAudioEngine(STEMS, {});
  const p = eng.play();
  eng.destroy();
  releaseDecode();
  check("fulldecode: a wait on a destroyed engine resolves false", (await p) === false);
  check("fulldecode: a destroyed engine's wait starts nothing", started.length === 0);
}

{
  started = [];
  decodeOrder.length = 0;
  decodeGate = Promise.resolve();
  const ev = { ended: 0 };
  const eng = createAudioEngine([{ name: "vocals", url: "/s/missing.wav" }], { onEnded: () => ev.ended++ });
  const p = eng.play();
  check("fulldecode: failed load, a waiting Play resolves false", (await p) === false);
  check("fulldecode: failed load, the error is reported", eng.getLoadError() === "player.loadError.unreachable", eng.getLoadError());
  check("fulldecode: failed load, reads as not playing", eng.isPlaying() === false);
  check("fulldecode: failed load, no onEnded", ev.ended === 0);
  eng.destroy();
}

console.log(`\n${pass}/${pass + fail} checks passed`);
process.exit(fail ? 1 : 0);
