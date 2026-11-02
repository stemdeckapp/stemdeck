// Why a song cannot play, in the language the person chose. Both engines give
// the reason a load failed (getLoadError) and player.js puts it on screen. It
// was an English literal in every language. Each engine now gives an i18n key
// for each cause, and every table has its own words for it.
//
// The engines do not translate it themselves: the phone page shares them and
// never loads the string tables, which are over a megabyte.
//
// Run:  node tests/js/engine-load-error.test.mjs

import { readFileSync } from "node:fs";

let pass = 0, fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) { pass++; console.log(`PASS  ${name}`); }
  else { fail++; console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`); }
};

const SR = 44100, CH = 2;

// A RIFF/WAVE file with `seconds` of silent PCM16.
function wav(seconds) {
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

const FILES = new Map([
  ["/s/empty.wav", wav(0)],
  ["/s/garbage.wav", Buffer.alloc(4096, 0x41)],
]);

globalThis.fetch = async (url, opts = {}) => {
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

const node = () => ({
  gain: { value: 1, setTargetAtTime() {}, cancelScheduledValues() {}, setValueAtTime() {}, linearRampToValueAtTime() {} },
  connect() {}, disconnect() {},
});
class FakeCtx {
  constructor() { this.currentTime = 0; this.destination = {}; this.state = "running"; this.audioWorklet = null; this.sampleRate = SR; }
  addEventListener() {}
  removeEventListener() {}
  createGain() { return node(); }
  createAnalyser() { return { fftSize: 0, connect() {}, disconnect() {} }; }
  // The browser decoder refuses anything that is not a RIFF file.
  async decodeAudioData(bytes) {
    if (Buffer.from(bytes, 0, 4).toString("ascii") !== "RIFF") throw new Error("EncodingError");
    return { numberOfChannels: CH, length: 0, sampleRate: SR, duration: 0, getChannelData: () => new Float32Array(0) };
  }
  async close() {}
}
globalThis.window = { AudioContext: FakeCtx };

const { TRANSLATIONS, LANGUAGES } = await import("../../static/js/i18n.js");
const { createChunkedAudioEngine } = await import("../../static/js/chunkedAudioEngine.js");
const { createAudioEngine } = await import("../../static/js/audioEngine.js");

async function reason(create, stems) {
  const eng = create(stems);
  const ok = await eng.ready;
  const key = eng.getLoadError();
  eng.destroy();
  return { ok, key };
}

const cases = [
  ["streaming", createChunkedAudioEngine, "no stems", [], "player.loadError.noStems"],
  ["streaming", createChunkedAudioEngine, "a stem that does not download", [{ name: "vocals", url: "/s/missing.wav" }], "player.loadError.unreachable"],
  ["streaming", createChunkedAudioEngine, "a stem it cannot read", [{ name: "vocals", url: "/s/garbage.wav" }], "player.loadError.undecodable"],
  ["streaming", createChunkedAudioEngine, "a stem with no audio", [{ name: "vocals", url: "/s/empty.wav" }], "player.loadError.noAudio"],
  ["full-decode", createAudioEngine, "no stems", [], "player.loadError.noStems"],
  ["full-decode", createAudioEngine, "a stem that does not download", [{ name: "vocals", url: "/s/missing.wav" }], "player.loadError.unreachable"],
  ["full-decode", createAudioEngine, "a stem it cannot read", [{ name: "vocals", url: "/s/garbage.wav" }], "player.loadError.undecodable"],
];

for (const [engine, create, what, stems, key] of cases) {
  const got = await reason(create, stems);
  check(`${engine}: ${what} does not load`, got.ok === false);
  check(`${engine}: ${what} gives ${key}`, got.key === key, `got ${JSON.stringify(got.key)}`);
}

// Every language has its own words for each cause (pt-PT falls through to pt).
const english = TRANSLATIONS.en;
for (const key of new Set(cases.map((c) => c[4]))) {
  check(`en has ${key}`, typeof english[key] === "string" && english[key].length > 0);
  for (const { code } of LANGUAGES) {
    if (code === "en" || code === "pt-PT") continue;
    const text = TRANSLATIONS[code][key];
    check(`${code} translates ${key}`, typeof text === "string" && text.length > 0 && text !== english[key], JSON.stringify(text));
  }
}

for (const file of ["audioEngine.js", "chunkedAudioEngine.js"]) {
  const src = readFileSync(new URL(`../../static/js/${file}`, import.meta.url), "utf8");
  check(`${file} does not load the string tables`, !/from\s+["']\.\/i18n\.js["']/.test(src));
}

console.log(`\n${pass}/${pass + fail} checks passed`);
process.exit(fail ? 1 : 0);
