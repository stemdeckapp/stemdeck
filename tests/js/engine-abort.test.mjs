// A full-decode engine that is destroyed while its stems download (a song or
// a version opened again before the first load ended) stops downloading them
// and decodes nothing. Every stem used to download and decode in full and be
// thrown away after, so two quick switches ran two whole loads at once.
//
// Run:  node tests/js/engine-abort.test.mjs

let pass = 0, fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) { pass++; console.log(`PASS  ${name}`); }
  else { fail++; console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`); }
};
process.on("unhandledRejection", (e) => { console.error("UNHANDLED:", e); process.exitCode = 1; });

// A server whose answers (the headers, then the body) wait until released,
// and that honours an abort the way a browser does: the pending fetch rejects
// with an AbortError.
const gate = () => { let open; const p = new Promise((r) => { open = r; }); return { p, open }; };
let headers = gate();
let body = gate();
const signals = [];
globalThis.fetch = (url, opts = {}) => new Promise((resolve, reject) => {
  const signal = opts.signal;
  signals.push(signal);
  const abort = () => reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
  if (signal?.aborted) return abort();
  signal?.addEventListener("abort", abort);
  headers.p.then(() => resolve({ ok: true, status: 200, arrayBuffer: () => body.p.then(() => new ArrayBuffer(16)) }));
});

let decoded = 0;
const node = () => ({
  gain: { value: 1, setTargetAtTime() {}, cancelScheduledValues() {}, setValueAtTime() {}, linearRampToValueAtTime() {} },
  connect() {}, disconnect() {},
});
class FakeCtx {
  constructor() { this.currentTime = 0; this.destination = {}; this.state = "running"; this.audioWorklet = null; this.sampleRate = 44100; }
  addEventListener() {}
  removeEventListener() {}
  createGain() { return node(); }
  createAnalyser() { return { fftSize: 0, connect() {}, disconnect() {} }; }
  async decodeAudioData() {
    decoded++;
    return { numberOfChannels: 2, length: 44100, sampleRate: 44100, duration: 1, getChannelData: () => new Float32Array(44100) };
  }
  async close() { this.state = "closed"; }
}
globalThis.window = { AudioContext: FakeCtx };

const { createAudioEngine } = await import("../../static/js/audioEngine.js");
const STEMS = [{ name: "vocals", url: "/s/vocals.wav" }, { name: "bass", url: "/s/bass.wav" }];
const settle = () => new Promise((r) => setTimeout(r, 0));

// Destroyed before any stem answered.
const early = createAudioEngine(STEMS);
await settle();
check("every stem download can be aborted", signals.length === 2 && signals.every((s) => s && typeof s.aborted === "boolean"));
early.destroy();
check("destroy() aborts the downloads still running", signals.every((s) => s?.aborted === true));
headers.open();
body.open();
check("the destroyed engine does not load", (await early.ready) === false);
check("nothing is decoded for it", decoded === 0, `decoded ${decoded}`);

// Destroyed while the bodies were still arriving.
headers = gate();
body = gate();
const late = createAudioEngine(STEMS);
headers.open();
await settle();
late.destroy();
body.open();
await late.ready;
check("bytes that arrive after destroy() are not decoded", decoded === 0, `decoded ${decoded}`);

// An engine left alone still loads every stem.
const live = createAudioEngine(STEMS);
check("an engine that is not destroyed loads", (await live.ready) === true && decoded === 2, `decoded ${decoded}`);
live.destroy();

console.log(`\n${pass}/${pass + fail} checks passed`);
process.exit(fail ? 1 : 0);
