// Regression test for #358: the chunked engine used to read only the first 1 KB
// of a WAV looking for the `data` chunk, so any file whose writer put more
// metadata in front of it loaded with duration 0 and playback silently off.
//
// Not a parser unit test. It stubs fetch with a Range-honouring server and calls
// createChunkedAudioEngine(...).ready, so it covers the widening fetch loop, the
// parser and the failure reporting together, through the module's public API.
//
// Run:  node tests/js/wav-header.test.mjs
//
// Against the pre-fix engine this reports 25/38, with JUNK 4096, LIST 2 KB,
// JUNK 300 KB, the chained case and data-size-0 all loading at duration 0, and
// data-size-0xffffffff loading at 24347s.

import { createChunkedAudioEngine } from "../../static/js/chunkedAudioEngine.js";

// --------------------------------------------------------------------------
// WAV construction
// --------------------------------------------------------------------------

const SR = 44100, CH = 2, BITS = 16;
const SECONDS = 8;
const FRAMES = SR * SECONDS;

function chunk(id, body) {
  const pad = body.length & 1;
  const b = Buffer.alloc(8 + body.length + pad);
  b.write(id, 0, 4, "ascii");
  b.writeUInt32LE(body.length, 4);
  body.copy(b, 8);
  return b;
}

function fmtPlain(audioFormat = 1, bits = BITS) {
  const b = Buffer.alloc(16);
  b.writeUInt16LE(audioFormat, 0);
  b.writeUInt16LE(CH, 2);
  b.writeUInt32LE(SR, 4);
  b.writeUInt32LE(SR * CH * (bits >> 3), 8);
  b.writeUInt16LE(CH * (bits >> 3), 12);
  b.writeUInt16LE(bits, 14);
  return chunk("fmt ", b);
}

// WAVE_FORMAT_EXTENSIBLE: 40-byte fmt whose SubFormat GUID carries the real code.
function fmtExtensible(subFormat, bits) {
  const b = Buffer.alloc(40);
  b.writeUInt16LE(0xfffe, 0);
  b.writeUInt16LE(CH, 2);
  b.writeUInt32LE(SR, 4);
  b.writeUInt32LE(SR * CH * (bits >> 3), 8);
  b.writeUInt16LE(CH * (bits >> 3), 12);
  b.writeUInt16LE(bits, 14);
  b.writeUInt16LE(22, 16);       // cbSize
  b.writeUInt16LE(bits, 18);     // validBitsPerSample
  b.writeUInt32LE(3, 20);        // channelMask
  b.writeUInt16LE(subFormat, 24); // first field of the SubFormat GUID
  return chunk("fmt ", b);
}

function buildWav({ pre = [], fmt = fmtPlain(), bits = BITS, dataSizeOverride = null } = {}) {
  const bytesPerFrame = CH * (bits >> 3);
  const audio = Buffer.alloc(FRAMES * bytesPerFrame);
  for (let i = 0; i < FRAMES; i++) {
    if (bits === 16) audio.writeInt16LE(((i % 100) - 50) * 100, i * bytesPerFrame);
    else audio.writeFloatLE(0.1, i * bytesPerFrame);
  }
  const dataHdr = Buffer.alloc(8);
  dataHdr.write("data", 0, 4, "ascii");
  dataHdr.writeUInt32LE(dataSizeOverride ?? audio.length, 4);

  const body = Buffer.concat([Buffer.from("WAVE", "ascii"), fmt, ...pre, dataHdr, audio]);
  const riff = Buffer.alloc(8);
  riff.write("RIFF", 0, 4, "ascii");
  riff.writeUInt32LE(body.length, 4);
  return Buffer.concat([riff, body]);
}

const junk = (n) => chunk("JUNK", Buffer.alloc(n));
const listInfo = (n) =>
  chunk("LIST", Buffer.concat([Buffer.from("INFO", "ascii"), chunk("ICMT", Buffer.alloc(n))]));

// --------------------------------------------------------------------------
// Fake Range server + AudioContext
// --------------------------------------------------------------------------

const FILES = new Map();
let requests = [];

globalThis.fetch = async (url, opts = {}) => {
  const file = FILES.get(url);
  if (!file) return { ok: false, status: 404, headers: { get: () => null }, arrayBuffer: async () => new ArrayBuffer(0) };
  const m = /bytes=(\d+)-(\d+)/.exec(opts.headers?.Range || "");
  if (!m) throw new Error("test server requires a Range header");
  const start = Number(m[1]);
  const end = Math.min(Number(m[2]), file.length - 1);
  requests.push({ url, start, end });
  const slice = file.subarray(start, end + 1);
  return {
    ok: true,
    status: 206,
    headers: { get: (h) => (h === "Content-Range" ? `bytes ${start}-${end}/${file.length}` : null) },
    arrayBuffer: async () => slice.buffer.slice(slice.byteOffset, slice.byteOffset + slice.byteLength),
  };
};

const node = () => ({
  gain: { value: 1, setTargetAtTime() {} },
  connect() {}, disconnect() {},
});
class FakeCtx {
  constructor() { this.currentTime = 0; this.destination = {}; this.state = "running"; this.audioWorklet = null; }
  createGain() { return node(); }
  createAnalyser() { return { fftSize: 0, connect() {}, disconnect() {} }; }
  createBuffer(ch, len, rate) {
    return { numberOfChannels: ch, length: len, sampleRate: rate, duration: len / rate,
             getChannelData: () => new Float32Array(len) };
  }
  createBufferSource() {
    return { buffer: null, playbackRate: { value: 1 }, connect() {}, start() {}, stop() {}, disconnect() {} };
  }
  async close() {}
}
globalThis.window = { AudioContext: FakeCtx };
process.on("unhandledRejection", (e) => { console.error("UNHANDLED:", e); process.exitCode = 1; });

// --------------------------------------------------------------------------
// Cases
// --------------------------------------------------------------------------

const EXPECTED_DUR = SECONDS;
const cases = [
  ["canonical 44-byte header",        buildWav(),                                       true],
  ["WAVE_FORMAT_EXTENSIBLE pcm16",    buildWav({ fmt: fmtExtensible(1, 16) }),          true],
  ["small LIST/INFO (200 B)",         buildWav({ pre: [listInfo(200)] }),               true],
  ["JUNK 512 B",                      buildWav({ pre: [junk(512)] }),                   true],
  // The two layouts that previously disabled playback outright (#358).
  ["JUNK 4096 B",                     buildWav({ pre: [junk(4096)] }),                  true],
  ["LIST/INFO 2 KB",                  buildWav({ pre: [listInfo(2048)] }),              true],
  // Multi-round-trip, and several metadata chunks in a row.
  ["JUNK 300 KB",                     buildWav({ pre: [junk(300 * 1024)] }),            true],
  ["JUNK+LIST+JUNK chained",          buildWav({ pre: [junk(4096), listInfo(9000), junk(70000)] }), true],
  // Sizes a writer left unpatched: both used to yield a nonsense duration.
  ["data size 0 (streamed)",          buildWav({ dataSizeOverride: 0 }),                true],
  ["data size 0xffffffff",            buildWav({ dataSizeOverride: 0xffffffff }),       true],
  // Sample formats. float32 decodes; the EXTENSIBLE variant only does so
  // because the real format code is read out of the SubFormat GUID -- without
  // that it reads as 0xfffe and is rejected here.
  ["float32",                         buildWav({ fmt: fmtPlain(3, 32), bits: 32 }),     true],
  ["EXTENSIBLE float32",              buildWav({ fmt: fmtExtensible(3, 32), bits: 32 }), true],
  // Rejections.
  // Neither depth can be turned into samples, and accepting them was worse than
  // rejecting: the header measured fine, so every chunk came back empty and the
  // scheduler re-fetched the same range on every frame. Rejected, they fall
  // through to the full-decode engine, whose decoder handles both.
  ["24-bit PCM",                      buildWav({ fmt: fmtPlain(1, 24), bits: 24 }),     false],
  ["32-bit integer PCM",              buildWav({ fmt: fmtPlain(1, 32), bits: 32 }),     false],
  ["not a RIFF file",                 Buffer.alloc(4096, 0x41),                         false],
  ["chunk size runs past EOF",        (() => { const w = buildWav({ pre: [junk(64)] });
                                               w.writeUInt32LE(0x7fffffff, 16); return w; })(), false],
];

let pass = 0, fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) { pass++; console.log(`PASS  ${name}`); }
  else { fail++; console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`); }
};

for (const [name, buf, shouldLoad] of cases) {
  FILES.clear(); requests = [];
  const url = `/stems/${name.replace(/\W+/g, "_")}.wav`;
  FILES.set(url, buf);

  const eng = createChunkedAudioEngine([{ name: "vocals", url }]);
  const ok = await eng.ready;

  if (shouldLoad) {
    const dur = eng.getDuration();
    const hdrReqs = requests.filter((r) => r.start === 0).length;
    check(`${name}: loads`, ok === true, `getLoadError=${JSON.stringify(eng.getLoadError())}`);
    check(`${name}: duration ~${EXPECTED_DUR}s`, Math.abs(dur - EXPECTED_DUR) < 0.05, `got ${dur}`);
    check(`${name}: <=4 header requests`, hdrReqs <= 4, `made ${hdrReqs}`);
  } else {
    check(`${name}: rejected`, ok === false, `duration=${eng.getDuration()}`);
    check(`${name}: reports a reason`, typeof eng.getLoadError() === "string" && eng.getLoadError().length > 0);
  }
  eng.destroy();
}

// Unreachable stems must be distinguishable from unreadable ones.
FILES.clear(); requests = [];
{
  const eng = createChunkedAudioEngine([{ name: "vocals", url: "/missing.wav" }]);
  const ok = await eng.ready;
  check("404 stem: rejected", ok === false);
  check("404 stem: says 'could not load', not 'format'",
    /could not load/i.test(eng.getLoadError() || ""), eng.getLoadError());
  eng.destroy();
}
{
  const eng = createChunkedAudioEngine([]);
  const ok = await eng.ready;
  check("no stems: rejected", ok === false);
  check("no stems: distinct message", /no stem files/i.test(eng.getLoadError() || ""), eng.getLoadError());
  eng.destroy();
}

console.log(`\n${pass}/${pass + fail} checks passed`);
process.exit(fail ? 1 : 0);
