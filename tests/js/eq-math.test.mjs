// eqMath.js (schema, coefficients, wire form) and fxChain.js (the slot's
// wiring, ramps and crossfades) without a browser. The browser's own filters
// are checked against the same golden file in tests/e2e/eq-engine.spec.mjs,
// and the Python export maths in tests/test_fx_filters.py.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  EQ_LIMITS, bandSections, encodeFxParam, eqIsFlat, eqResponseDb, eqToWire, flatEq, normalizeEq,
} from "../../static/js/eqMath.js";
import { createFxSlot, createEngineFx } from "../../static/js/fxChain.js";

let passed = 0;
let failed = 0;
function check(name, condition, detail = "") {
  if (condition) { passed++; console.log(`PASS  ${name}`); }
  else { failed++; console.log(`FAIL  ${name}${detail ? `  -- ${detail}` : ""}`); }
}

const here = path.dirname(fileURLToPath(import.meta.url));
const golden = JSON.parse(fs.readFileSync(path.join(here, "..", "fixtures", "eq_golden.json"), "utf8"));

// ── Maths against Chromium's nodes ──
for (const rate of golden.rates) {
  let worst = 0;
  let worstCase = "";
  for (const [name, raw] of Object.entries(golden.cases)) {
    const ours = eqResponseDb(normalizeEq(raw), golden.freqs, rate);
    const theirs = golden.chromium[rate][name];
    for (let i = 0; i < ours.length; i++) {
      const d = Math.abs(ours[i] - theirs[i]);
      if (d > worst) { worst = d; worstCase = name; }
    }
  }
  check(`eqMath matches Chromium's nodes at ${rate} Hz (worst ${worst.toExponential(2)} dB, ${worstCase})`, worst < 1e-3);
}

// ── Normalisation ──
{
  const n = normalizeEq({ params: { out: 99, bands: [
    { type: "peaking", f: 1e9, g: -500, q: 0 },
    null,
    { type: "nope" },
    { type: "lowpass", f: 1234.56, slope: 20 },
    { type: "bell", f: NaN, g: "3" },
  ] } });
  check("output gain is clamped", n.params.out === EQ_LIMITS.outMax);
  check("aliases map to the v1 names", n.params.bands[0].type === "bell" && n.params.bands[1].type === "highcut");
  check("unknown bands are dropped", n.params.bands.length === 3);
  check("numbers are clamped into range", n.params.bands[0].f === 20000 && n.params.bands[0].g === -30 && n.params.bands[0].q === 0.025);
  check("frequency is rounded to 0.1 Hz", n.params.bands[1].f === 1234.6);
  check("a slope snaps to the nearest offered one", n.params.bands[1].slope === 24);
  check("non-numbers fall back to defaults", n.params.bands[2].f === 1000 && n.params.bands[2].g === 0);
  check("a newer version reads as flat", normalizeEq({ id: "eq", v: 2, params: { bands: [{ type: "bell", g: 6 }] } }).params.bands.length === 0);
  check("another effect id reads as flat", normalizeEq({ id: "master", params: {} }).params.bands.length === 0);
  const many = normalizeEq({ params: { bands: Array.from({ length: 30 }, () => ({ type: "bell", g: 1 })) } });
  check("at most maxBands bands", many.params.bands.length === EQ_LIMITS.maxBands);
  const ui = normalizeEq({ params: { ui: { panel: "bass", knobs: [1, 2] } } });
  check("panel state is kept", ui.params.ui?.panel === "bass");
  const big = normalizeEq({ params: { ui: { s: "x".repeat(EQ_LIMITS.maxUiChars) } } });
  check("oversized panel state is dropped", big.params.ui === undefined);
}

// ── Sections ──
{
  const s = bandSections({ on: true, type: "lowcut", f: 50, g: 0, q: 0.707, slope: 48 });
  check("48 dB/oct is four highpass sections", s.length === 4 && s.every((x) => x.type === "highpass"));
  check("bands at 0 dB add no section", bandSections({ on: true, type: "bell", f: 100, g: 0, q: 1, slope: 12 }).length === 0);
  check("a band switched off adds no section", bandSections({ on: false, type: "notch", f: 100, g: 0, q: 1, slope: 12 }).length === 0);
  check("tilt is two shelves", bandSections({ on: true, type: "tilt", f: 800, g: 4, q: 1, slope: 12 }).map((x) => `${x.type}${x.g}`).join() === "lowshelf-2,highshelf2");
  const [atF] = eqResponseDb(normalizeEq({ params: { bands: [{ type: "highcut", f: 2000, slope: 36 }] } }), [2000], 48000);
  check("a Butterworth cut is 3 dB down at its frequency", Math.abs(atF + 3.01) < 0.02, `${atF}`);
}

// ── Flatness and the wire ──
{
  check("flatEq is flat", eqIsFlat(flatEq()));
  const boost = normalizeEq({ params: { bands: [{ type: "bell", f: 1000, g: 3 }, { type: "bell", f: 200, g: 0 }] } });
  check("a boost is not flat", !eqIsFlat(boost));
  check("a bypassed boost is flat", eqIsFlat({ ...boost, bypass: true }));
  check("output gain alone is not flat", !eqIsFlat(normalizeEq({ params: { out: -3 } })));
  check("the wire carries only bands that do something", JSON.stringify(eqToWire(boost)) === '{"id":"eq","out":0,"b":[["bell",1000,3,0.707,12]]}');
  check("no fx param when nothing changes the sound", encodeFxParam({ vocals: flatEq(), bass: { ...boost, bypass: true } }, flatEq()) === "");
  const param = encodeFxParam({ vocals: boost }, normalizeEq({ params: { out: -1 } }));
  const decoded = JSON.parse(Buffer.from(param, "base64url").toString("utf8"));
  check("fx param is base64url JSON", /^[A-Za-z0-9_-]+$/.test(param) && decoded.v === 1);
  check("fx param has the lane and the master", !!decoded.lanes.vocals && decoded.master[0].out === -1);
}

// ── The slot, on a fake graph ──
class Param {
  constructor(v) { this.value = v; this.events = []; }
  cancelScheduledValues() { this.events.push("cancel"); }
  cancelAndHoldAtTime() { this.events.push("hold"); }
  setValueAtTime(v) { this.value = v; }
  linearRampToValueAtTime(v, t) { this.events.push(["ramp", v, t]); this.value = v; }
}
class Node {
  constructor(ctx, kind) { this.ctx = ctx; this.kind = kind; this.out = []; ctx.nodes.push(this); }
  connect(d) { this.out.push(d); return d; }
  disconnect(d) {
    if (d === undefined) { this.out = []; return; }
    const i = this.out.indexOf(d);
    if (i < 0) throw new Error("InvalidAccessError");
    this.out.splice(i, 1);
  }
}
class Ctx {
  constructor() { this.nodes = []; this.currentTime = 1; this.sampleRate = 48000; this.destination = new Node(this, "dest"); }
  createGain() { const n = new Node(this, "gain"); n.gain = new Param(1); return n; }
  createBiquadFilter() {
    const n = new Node(this, "biquad");
    n.type = "lowpass"; n.frequency = new Param(350); n.Q = new Param(1); n.gain = new Param(0);
    return n;
  }
  createAnalyser() { return new Node(this, "analyser"); }
}
{
  const ctx = new Ctx();
  const slot = createFxSlot(ctx);
  const dryOf = () => slot.input.out.find((n) => n.kind === "gain");
  check("an empty slot is input -> dry -> output", dryOf()?.out[0] === slot.output && slot.sectionCount() === 0);
  const eqA = normalizeEq({ params: { out: 2, bands: [{ type: "bell", f: 1000, g: 6, q: 2 }, { type: "lowcut", f: 80, slope: 24 }] } });
  slot.setEq(eqA, { immediate: true });
  check("an EQ builds one biquad per section", slot.sectionCount() === 3);
  const first = slot.input.out.find((n) => n.kind === "biquad");
  check("the first biquad has the band's values", first.type === "peaking" && first.frequency.value === 1000 && first.gain.value === 6 && first.Q.value === 2);
  check("dry is off and the output carries out gain", dryOf().gain.value === 0 && Math.abs(slot.output.gain.value - 10 ** (2 / 20)) < 1e-12);

  const eqB = normalizeEq({ params: { out: 2, bands: [{ type: "bell", f: 1500, g: 3, q: 2 }, { type: "lowcut", f: 90, slope: 24 }] } });
  slot.setEq(eqB);
  check("same structure: the same nodes, ramped", slot.input.out.find((n) => n.kind === "biquad") === first
    && first.frequency.events.some((e) => Array.isArray(e) && e[1] === 1500));
  check("a ramp ends on the exact value", first.frequency.value === 1500 && first.gain.value === 3);

  slot.setEq({ ...eqB, bypass: true });
  check("bypass: dry back to exactly 1, output gain exactly 1", dryOf().gain.value === 1 && slot.output.gain.value === 1);
  check("bypass keeps the filters connected", slot.sectionCount() === 3 && slot.input.out.includes(first));

  const eqC = normalizeEq({ params: { bands: [{ type: "highcut", f: 5000, slope: 48 }] } });
  slot.setEq(eqC);
  check("a new structure builds a new branch", slot.sectionCount() === 4);
  check("the old branch stays connected while it fades", slot.input.out.includes(first));
  check("the old branch fades to 0", ctx.nodes.filter((n) => n.kind === "gain").some((g) => g.gain.events?.some((e) => Array.isArray(e) && e[1] === 0)));
  slot.setEq(flatEq(), { immediate: true });
  check("flat with immediate drops every filter", slot.sectionCount() === 0 && !slot.input.out.some((n) => n.kind === "biquad"));
  check("flat: dry 1, output 1", dryOf().gain.value === 1 && slot.output.gain.value === 1);
  slot.dispose();
}
{
  const ctx = new Ctx();
  const sources = [{ controlName: "original" }, { controlName: "original" }, { controlName: "vocals" }];
  const fx = createEngineFx(ctx, (name) => sources.filter((s) => s.controlName === name));
  const eq = normalizeEq({ params: { bands: [{ type: "bell", f: 300, g: -4 }] } });
  fx.setLaneEq("original", eq);
  // Sources decoded after the EQ was set still get it.
  for (const s of sources) fx.attachSource(s, ctx.createGain(), ctx.createAnalyser());
  check("a late source gets the lane's EQ", sources[0].fx.sectionCount() === 1 && sources[1].fx.sectionCount() === 1);
  check("other lanes stay flat", sources[2].fx.sectionCount() === 0);
  const tap = fx.getLaneTap("original", "post", { fftSize: 2048 });
  check("one tap per lane, fed by every source of the lane", sources[0].fx.output.out.includes(tap) && sources[1].fx.output.out.includes(tap));
  check("asking again returns the same tap", fx.getLaneTap("original", "post") === tap);
  fx.releaseLaneTap("original", "post");
  check("releasing disconnects it", !sources[0].fx.output.out.includes(tap));
  check("the master slot ends at the destination", fx.masterOutput.out.includes(ctx.destination));
  check("unknown lane: no tap", fx.getLaneTap("nope", "pre") === null);
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
