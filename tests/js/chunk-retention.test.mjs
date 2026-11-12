// Playback must not keep every chunk it has played alive.
//
// The chunked engine kept each AudioBufferSourceNode it scheduled in a list
// that only pause, seek and destroy emptied. A played node holds its five
// second buffer, so memory grew by one buffer per stem every five seconds and
// was all freed at Stop: 2.1 MB/s on a six stem song in Chromium, 1.3 GB over
// ten minutes, measured with CDP (Runtime.getHeapUsage backingStorageSize).
//
// This plays a two minute track against a fake clock and counts, after a real
// garbage collection, how many decoded buffers are still reachable. The fake
// context behaves like a real one here: it holds a source until the source
// has played, fires `onended`, and then lets go of it. Whatever is still
// alive after that is held by the engine. The 6 second e2e fixture is too
// short to show any of this, hence a node test.
//
// Run:  node tests/js/chunk-retention.test.mjs

import v8 from "node:v8";
import vm from "node:vm";

v8.setFlagsFromString("--expose-gc");
const gc = vm.runInNewContext("gc");

let passed = 0;
let failed = 0;
function check(name, condition, detail = "") {
  if (condition) { passed++; console.log(`PASS  ${name}`); }
  else { failed++; console.log(`FAIL  ${name}${detail ? `  -- ${detail}` : ""}`); }
}

const RATE = 8000;
const TRACK_SEC = 120;
const PLAY_SEC = 100;
const STEP_SEC = 0.1;
const CHUNK_SEC = 5;

class FakeParam { constructor(value = 0) { this.value = value; } }

class FakeNode {
  constructor(ctx) { this.ctx = ctx; }
  connect(destination) { return destination; }
  disconnect() {}
}

class FakeGain extends FakeNode {
  constructor(ctx) {
    super(ctx);
    this.gain = {
      value: 1,
      setTargetAtTime: (value) => { this.gain.value = value; },
      cancelScheduledValues() {}, setValueAtTime() {}, linearRampToValueAtTime() {},
    };
  }
}

class FakeAnalyser extends FakeNode {
  constructor(ctx) { super(ctx); this.fftSize = 1024; }
}

// Held by the context from start() until it has played, as Web Audio does.
class FakeSource extends FakeNode {
  constructor(ctx) {
    super(ctx);
    this.playbackRate = new FakeParam(1);
    this.buffer = null;
    this.onended = null;
  }

  start(when, offset = 0) {
    this.endsAt = when + (this.buffer.duration - offset) / this.playbackRate.value;
    this.ctx.playing.add(this);
  }

  stop() { this.endsAt = Math.min(this.endsAt ?? 0, this.ctx.currentTime); }
}

class FakeContext {
  constructor() {
    this.playing = new Set();
    this.buffers = [];
    this.currentTime = 0;
    this.sampleRate = 44100;
    this.state = "running";
    this.destination = new FakeNode(this);
    this.audioWorklet = { addModule: async () => {} };
  }

  createGain() { return new FakeGain(this); }
  createAnalyser() { return new FakeAnalyser(this); }
  createBufferSource() { return new FakeSource(this); }
  createBuffer(channels, length, sampleRate) {
    const data = Array.from({ length: channels }, () => new Float32Array(length));
    const buf = { duration: length / sampleRate, getChannelData: (channel) => data[channel] };
    this.buffers.push(new WeakRef(buf));
    return buf;
  }

  /** Advance the clock: sources that have played end and are let go. */
  advance(dt) {
    this.currentTime += dt;
    for (const s of [...this.playing]) {
      if (s.endsAt <= this.currentTime) {
        this.playing.delete(s);
        s.onended?.();
      }
    }
  }

  liveBuffers() { return this.buffers.filter((r) => r.deref()).length; }
  async resume() {}
  async close() {}
  addEventListener() {}
  removeEventListener() {}
}

class FakeWorkletNode extends FakeNode {
  constructor(ctx) {
    super(ctx);
    this.parameters = new Map([["tempo", new FakeParam(1)]]);
    this.port = { postMessage() {} };
    queueMicrotask(() => this.port.onmessage?.({ data: { type: "latency", frames: 6541 } }));
  }
}

let pendingTick = null;
globalThis.window = { AudioContext: FakeContext };
globalThis.AudioWorkletNode = FakeWorkletNode;
globalThis.requestAnimationFrame = (cb) => { pendingTick = cb; return 1; };
globalThis.cancelAnimationFrame = () => { pendingTick = null; };

function wavBytes(seconds) {
  const dataBytes = seconds * RATE * 2;
  const bytes = new ArrayBuffer(44 + dataBytes);
  const view = new DataView(bytes);
  const text = (offset, value) => {
    for (let i = 0; i < value.length; i++) view.setUint8(offset + i, value.charCodeAt(i));
  };
  text(0, "RIFF");
  view.setUint32(4, bytes.byteLength - 8, true);
  text(8, "WAVE");
  text(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, RATE, true);
  view.setUint32(28, RATE * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  text(36, "data");
  view.setUint32(40, dataBytes, true);
  return new Uint8Array(bytes);
}

const WAV = wavBytes(TRACK_SEC);
globalThis.fetch = async (_url, options = {}) => {
  const match = /bytes=(\d+)-(\d+)/.exec(options.headers?.Range || "");
  const start = match ? Number(match[1]) : 0;
  const end = match ? Math.min(Number(match[2]), WAV.length - 1) : WAV.length - 1;
  const slice = WAV.slice(start, end + 1);
  return {
    ok: true,
    status: match ? 206 : 200,
    headers: { get: (name) => (name === "Content-Range" ? `bytes ${start}-${end}/${WAV.length}` : null) },
    arrayBuffer: async () => slice.buffer,
  };
};

const { createChunkedAudioEngine } = await import("../../static/js/chunkedAudioEngine.js");

const turn = () => new Promise((resolve) => setImmediate(resolve));
const settle = async () => { for (let i = 0; i < 5; i++) await turn(); };
// A WeakRef stays alive to the end of the job that read it: collect in a
// later turn, then count in a later one still.
const collected = async (ctx) => { await turn(); gc(); await turn(); gc(); await turn(); return ctx.liveBuffers(); };

const STEMS = ["vocals", "drums", "bass"];
const ctx = new FakeContext();
const engine = createChunkedAudioEngine(STEMS.map((name) => ({ name, url: `/${name}.wav` })), { context: ctx });
check("engine becomes ready", await engine.ready);
await engine.play();

const samples = [];
while (ctx.currentTime < PLAY_SEC) {
  ctx.advance(STEP_SEC);
  const tick = pendingTick;
  pendingTick = null;
  tick?.();
  await settle();
  if (Math.abs(ctx.currentTime % 20) < STEP_SEC / 2 && ctx.currentTime > 1) {
    samples.push([Math.round(ctx.currentTime), await collected(ctx)]);
  }
}
const created = ctx.buffers.length;
const live = await collected(ctx);
console.log(`buffers made ${created}, alive after ${PLAY_SEC} s: ${live}; over time ${JSON.stringify(samples)}`);

check("playback made a buffer per stem per chunk", created >= STEMS.length * (PLAY_SEC / CHUNK_SEC));
// What may stay: the previous and current chunk in the cache, the chunks the
// 12 s lookahead has queued, and the prefetched next one. About six chunks.
const bound = STEMS.length * 7;
check(`at most ${bound} buffers alive while playing`, live <= bound, `${live} alive`);
check("the live count does not grow with playing time",
  samples.at(-1)[1] <= samples[0][1] + STEMS.length, JSON.stringify(samples));

engine.pause();
ctx.advance(STEP_SEC); // stopped sources end
const afterPause = await collected(ctx);
// Paused, only the chunk cache stays (kept for the resume): previous,
// current and the prefetched ones.
check("pause lets go of what was queued", afterPause <= STEMS.length * 4, `${afterPause} alive`);
engine.destroy();

console.log(`\n${passed}/${passed + failed} checks passed`);
process.exit(failed === 0 ? 0 : 1);
