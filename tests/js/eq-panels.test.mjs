// Pure logic behind the EQ panels (static/js/eq/): which panel a lane gets,
// the panel rows' place and height in the lane stack, the remembered open
// state, the graph editor's axes, hit testing, drag, wheel and key maths,
// number formats, undo history and spectrum mapping, and how the pedal and
// keys panels' knobs become bands on the shared engine.
//
// Run:  node tests/js/eq-panels.test.mjs

import {
  panelKindFor, panelHeight, eqRowOrder, openPanelsHeight, laneLayout, laneHasPanel,
  parseOpenStore, withOpenChoice, openKey, OPEN_STORE_MAX, PANEL_KINDS,
} from "../../static/js/eq/eqPanelKinds.js";
import {
  F_MIN, F_MAX, DB_RANGE, plotRect, freqToX, xToFreq, dbToY, yToDb, nodeXY, hitTest, dragBand,
  wheelQ, defaultBandAt, resetBand, retypeBand, soloEq, keyStep, fmtFreq, fmtDb, fmtQ,
  parseFreq, parseNumber, createHistory, columnBins, spectrumFrame, curveFreqs, SPEC_MIN_DB,
} from "../../static/js/eq/eqGraphMath.js";
import {
  PEDALS, qVal, qPos, logPos, logVal, dynamicShelfFreq, pedalEq, pedalDefaults, normalizePedal,
  keysEq, keysDefaults, controlsFromEq, panelEq, hpfOn, lpfOn,
} from "../../static/js/eq/eqPedalMath.js";
import { graphPresets, controlPresets } from "../../static/js/eq/eqPresets.js";
import { eqIsFlat, eqResponseDb, normalizeEq, normalizeBand, bandIsNeutral } from "../../static/js/eqMath.js";
import { takeLaneOrder, laneOrder } from "../../static/js/capture/takeFolderModel.js";

let pass = 0;
let fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) { pass++; console.log(`PASS  ${name}`); }
  else { fail++; console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`); }
};
const near = (a, b, tol) => Math.abs(a - b) <= tol;
const dbAt = (eq, f) => eqResponseDb(eq, [f], 44100)[0];

// ─── Which panel ───
{
  check("bass lane: Bass EQ", panelKindFor("bass") === "bass");
  check("guitar lane: Guitar EQ", panelKindFor("guitar") === "guitar");
  check("drums lane: Drum EQ", panelKindFor("drums") === "drum");
  check("piano lane: Keys EQ", panelKindFor("piano") === "keys");
  for (const v of ["vocals", "lead_vocals", "backing_vocals"]) check(`${v}: voice placeholder`, panelKindFor(v) === "voice");
  check("original: general EQ", panelKindFor("original") === "graph");
  check("other: general EQ", panelKindFor("other") === "graph");
  check("recording track on an instrument: general EQ", panelKindFor("k0123abcd", { isTrack: true, inputKind: "instrument" }) === "graph");
  check("recording track on a microphone: voice placeholder", panelKindFor("k0123abcd", { isTrack: true, inputKind: "voice" }) === "voice");
  check("recording track, input unknown: general EQ", panelKindFor("k0123abcd", { isTrack: true }) === "graph");
  check("every kind has a height", PANEL_KINDS.every((k) => panelHeight(k) > 100));
  check("the master slot has no panel row", !laneHasPanel("__master") && laneHasPanel("bass"));
}

// ─── Place and height in the stack ───
{
  check("EQ row right under its lane", eqRowOrder(laneOrder(3)) === laneOrder(3) + 1);
  check("take lanes after the EQ row", takeLaneOrder(3, 0) > eqRowOrder(laneOrder(3)));
  check("original (-100): its EQ row still above the first lane", eqRowOrder(laneOrder(-1)) < laneOrder(0));
  const open = new Set(["bass", "drums"]);
  const px = openPanelsHeight(["vocals", "drums", "bass"], (l) => open.has(l), panelKindFor);
  check("open panels add their heights", px === panelHeight("drum") + panelHeight("bass"), String(px));
  const lay = laneLayout(700, 4, 300, 72);
  check("lanes share what the panels leave", lay.rowH === 100 && lay.stackH === 700, JSON.stringify(lay));
  const tight = laneLayout(500, 6, 304, 72);
  check("lanes never shrink under the minimum (the stack scrolls)", tight.rowH === 72 && tight.stackH === 6 * 72 + 304);
  check("no panel: the old rule (floor of panel / count)", laneLayout(901, 5, 0, 72).rowH === 180);
  const capped = laneLayout(901, 5, 0, 72, 90);
  check("a cap keeps rows compact on a tall panel", capped.rowH === 90 && capped.stackH === 450, JSON.stringify(capped));
  check("the cap never lifts a row over what fits", laneLayout(400, 5, 0, 72, 90).rowH === 80);
}

// ─── Remembered open state ───
{
  check("nothing stored: empty", JSON.stringify(parseOpenStore(null)) === "{}");
  check("garbage: empty", JSON.stringify(parseOpenStore("{nope")) === "{}");
  check("arrays and non-true values dropped", JSON.stringify(parseOpenStore('{"a:b":true,"c:d":1,"e:f":"x"}')) === '{"a:b":true}');
  let s = withOpenChoice({}, openKey("job1", "bass"), true);
  check("open is stored per song and lane", s["job1:bass"] === true);
  s = withOpenChoice(s, openKey("job1", "bass"), false);
  check("closed is simply absent", !("job1:bass" in s));
  let big = {};
  for (let i = 0; i < OPEN_STORE_MAX + 20; i++) big = withOpenChoice(big, `j:${i}`, true);
  check("the store is bounded, oldest first out", Object.keys(big).length === OPEN_STORE_MAX && !big["j:0"] && big[`j:${OPEN_STORE_MAX + 19}`]);
}

// ─── Graph axes ───
{
  const r = plotRect(800, 300);
  check("20 Hz at the left edge", near(freqToX(F_MIN, r), r.x, 1e-9));
  check("20 kHz at the right edge", near(freqToX(F_MAX, r), r.x + r.w, 1e-9));
  check("200 Hz exactly a third across (log axis, 20 Hz to 20 kHz is three decades)", near(freqToX(200, r), r.x + r.w / 3, 1e-9));
  let worst = 0;
  for (const f of [20, 31.5, 100, 440, 1000, 5000, 12345, 20000]) worst = Math.max(worst, Math.abs(xToFreq(freqToX(f, r), r) - f) / f);
  check("x <-> frequency round trip", worst < 1e-12, String(worst));
  check("0 dB in the middle", near(dbToY(0, r), r.y + r.h / 2, 1e-9));
  check("+range at the top", near(dbToY(DB_RANGE, r), r.y, 1e-9));
  check("y <-> dB round trip", near(yToDb(dbToY(-7.25, r), r), -7.25, 1e-9));
  check("clicks off the right edge clamp to 20 kHz", xToFreq(r.x + r.w + 50, r) === F_MAX);
}

// ─── Handles and hit testing ───
{
  const r = plotRect(800, 300);
  const bands = [normalizeBand({ type: "bell", f: 1000, g: 6, q: 1 }), normalizeBand({ type: "lowcut", f: 80 })];
  const p0 = nodeXY(bands[0], r);
  const p1 = nodeXY(bands[1], r);
  check("a cut's handle sits on the 0 dB line", near(p1.y, dbToY(0, r), 1e-9));
  check("a hit on a handle finds its band", hitTest(bands, p0.x + 3, p0.y - 3, r) === 0);
  check("a hit 20 px away finds nothing", hitTest(bands, p0.x + 20, p0.y, r) === -1);
  const both = [normalizeBand({ type: "bell", f: 1000, g: 0 }), normalizeBand({ type: "bell", f: 1000, g: 0 })];
  const pb = nodeXY(both[0], r);
  check("two handles on one spot: the one drawn on top (later) wins", hitTest(both, pb.x, pb.y, r) === 1);
  const off = normalizeBand({ type: "bell", f: 1000, g: 40 });
  check("a gain beyond the graph's range keeps its handle on the edge", near(nodeXY(off, r).y, r.y, 1e-9));
}

// ─── Drag, wheel, keys ───
{
  const r = plotRect(900, 300);
  const start = normalizeBand({ type: "bell", f: 1000, g: 0, q: 1 });
  const octave = r.w / Math.log2(1000);
  const right = dragBand(start, octave, 0, r);
  check("dragging one octave right doubles the frequency", near(right.f, 2000, 1), String(right.f));
  const up = dragBand(start, 0, -(r.h / 2) / 3, r);
  check("dragging up a third of the half height adds a third of the range", near(up.g, DB_RANGE / 3, 0.02), String(up.g));
  const fine = dragBand(start, octave, 0, r, { fine: true });
  check("Shift drags a fifth as far", near(Math.log2(fine.f / 1000), 0.2, 0.002), String(fine.f));
  const far = dragBand(start, 0, -5000, r);
  check("a drag keeps the gain inside the graph", far.g === DB_RANGE);
  const cut = dragBand(normalizeBand({ type: "lowcut", f: 100 }), 0, -100, r);
  check("a cut only moves in frequency", cut.g === 0 && cut.f === 100);
  check("wheel up narrows (higher Q)", wheelQ(1, -100) > 1);
  check("wheel is multiplicative: up then down returns", near(wheelQ(wheelQ(2, -120), 120), 2, 0.01));
  check("wheel stays in the engine's Q range", wheelQ(39, -100000) === 40 && wheelQ(0.03, 100000) === 0.025);
  const k = keyStep(start, "ArrowRight");
  check("Right arrow: a semitone up", near(k.f, 1000 * 2 ** (1 / 12), 0.1), String(k.f));
  check("Up arrow: +0.5 dB", keyStep(start, "ArrowUp").g === 0.5);
  check("Shift+Up: +0.1 dB", keyStep(start, "ArrowUp", true).g === 0.1);
  check("Page Up widens nothing on a cut but changes Q", keyStep(normalizeBand({ type: "notch", f: 500, q: 4 }), "PageUp").q > 4);
  check("an unknown key changes nothing", keyStep(start, "x") === start);
}

// ─── Defaults, reset, retype, solo ───
{
  check("a click at 25 Hz adds a low cut", defaultBandAt(25, 6).type === "lowcut" && defaultBandAt(25, 6).g === 0);
  check("a click at 60 Hz adds a low shelf with the click's gain", defaultBandAt(60, -4).type === "lowshelf" && defaultBandAt(60, -4).g === -4);
  check("a click at 1 kHz adds a bell", defaultBandAt(1000, 3).type === "bell");
  check("a click at 12 kHz adds a high shelf", defaultBandAt(12000, 3).type === "highshelf");
  check("a click at 18 kHz adds a high cut", defaultBandAt(18000, 3).type === "highcut");
  check("cuts start at 24 dB/oct", defaultBandAt(25, 0).slope === 24);
  const r = resetBand(normalizeBand({ type: "bell", f: 700, g: 9, q: 5 }));
  check("double-click resets gain and width, keeps frequency", r.g === 0 && r.q === 1 && r.f === 700);
  const t = retypeBand(normalizeBand({ type: "bell", f: 700, g: 9, q: 5 }), "lowcut");
  check("retyping a bell into a cut drops its gain and starts at 24 dB/oct", t.type === "lowcut" && t.g === 0 && t.slope === 24 && t.q === 0.707);
  const s = soloEq(normalizeBand({ type: "bell", f: 2000, g: 4, q: 3 }));
  check("solo plays a band pass at the band's frequency and width", s.params.bands.length === 1 && s.params.bands[0].type === "bandpass" && s.params.bands[0].f === 2000 && s.params.bands[0].q === 3);
}

// ─── Numbers ───
{
  check("62.5 Hz", fmtFreq(62.5) === "62.5 Hz", fmtFreq(62.5));
  check("440 Hz", fmtFreq(440) === "440 Hz");
  check("1.25 kHz", fmtFreq(1250) === "1.25 kHz", fmtFreq(1250));
  check("12.5 kHz", fmtFreq(12500) === "12.5 kHz");
  check("1 kHz without zeros", fmtFreq(1000) === "1 kHz", fmtFreq(1000));
  check("+3.0 dB", fmtDb(3) === "+3.0 dB");
  check("negative dB with a minus sign", fmtDb(-12) === "−12.0 dB");
  check("0.0 dB", fmtDb(0.01) === "0.0 dB");
  check("Q formats", fmtQ(0.707) === "0.71" && fmtQ(4) === "4.0" && fmtQ(12) === "12");
  check("typed 1.2k", parseFreq("1.2k") === 1200);
  check("typed 1,5 kHz", parseFreq("1,5 kHz") === 1500);
  check("typed 80 Hz", parseFreq("80 Hz") === 80);
  check("typed nonsense", Number.isNaN(parseFreq("abc")));
  check("typed minus sign", parseNumber("−6") === -6);
  check("typed +3,5", parseNumber("+3,5") === 3.5);
  check("empty is not a number", Number.isNaN(parseNumber("")));
}

// ─── Undo history ───
{
  const h = createHistory(3);
  const s = (n) => ({ n });
  check("empty: nothing to undo", !h.canUndo && h.undo(s(0)) === null);
  h.push(s(1)); h.push(s(1));
  check("equal neighbours stored once", h.undo(s(2)).n === 1 && !h.canUndo);
  check("redo returns what was undone", h.redo(s(1)).n === 2);
  h.push(s(3));
  check("a new change clears redo", !h.canRedo);
  for (let i = 0; i < 10; i++) h.push(s(10 + i));
  let n = 0;
  while (h.canUndo) { h.undo(s(0)); n++; }
  check("bounded to its limit", n === 3, String(n));
  const obj = { a: [1] };
  h.push(obj);
  obj.a.push(2);
  check("stored states are copies", h.undo({}).a.length === 1);
}

// ─── Spectrum ───
{
  const r = plotRect(600, 200);
  const cols = Math.round(r.w);
  const bins = columnBins(cols, 4096, 48000, r);
  let ok = true;
  for (let c = 0; c < cols; c++) if (bins.lo[c] > bins.hi[c] || bins.lo[c] < 1 || bins.hi[c] > 4095) ok = false;
  check("every column covers at least one valid bin", ok);
  check("columns move up the spectrum", bins.lo[cols - 1] > bins.lo[0]);
  const data = new Float32Array(4096).fill(-120);
  const binHz = 24000 / 4096;
  data[Math.round(1000 / binHz)] = -12;
  const out = new Float32Array(cols).fill(SPEC_MIN_DB);
  spectrumFrame(bins, data, out, 0.6);
  const c1k = Math.floor((freqToX(1000, r) - r.x));
  const peak = Math.max(...out.slice(Math.max(0, c1k - 2), c1k + 3));
  check("a 1 kHz peak lands in the 1 kHz column", peak === -12, String(peak));
  check("silence sits on the floor", out[0] === SPEC_MIN_DB);
  data.fill(-120);
  spectrumFrame(bins, data, out, 0.6);
  const after = Math.max(...out.slice(Math.max(0, c1k - 2), c1k + 3));
  check("a peak falls back slowly (peak smoothing)", near(after, -12.6, 1e-5), String(after));
  const tilted = columnBins(cols, 4096, 48000, r, 4.5);
  const cTop = cols - 1;
  check("display tilt: +4.5 dB per octave above 1 kHz", near(tilted.tilt[cTop], 4.5 * Math.log2(Math.sqrt(xToFreq(r.x + (cTop / cols) * r.w, r) * xToFreq(r.x + r.w, r)) / 1000), 1e-4) && tilted.tilt[cTop] > 13);
  check("display tilt: negative below 1 kHz", tilted.tilt[0] < -20);
  check("curve frequencies span 20 Hz to 20 kHz", near(curveFreqs(r)[0], 20, 1e-9) && near(curveFreqs(r).at(-1), 20000, 1e-6));
}

// ─── Pedals ───
{
  check("Q knob: wide at the left stop", near(qVal(0), 1, 1e-12));
  check("Q knob: medium (2.5) at noon", near(qVal(0.5), 2.5, 1e-12));
  check("Q knob: narrow (4) at the right stop", near(qVal(1), 4, 1e-12));
  let worst = 0;
  for (let p = 0; p <= 1.0001; p += 0.05) worst = Math.max(worst, Math.abs(qPos(qVal(p)) - p));
  check("Q knob round trip", worst < 1e-9, String(worst));
  check("freq knob is logarithmic: noon is the geometric middle", near(logVal(0.5, 35, 500), Math.sqrt(35 * 500), 1e-9));
  check("freq knob round trip", near(logPos(logVal(0.3, 100, 1600), 100, 1600), 0.3, 1e-12));

  for (const kind of ["bass", "guitar"]) {
    const d = pedalDefaults(kind);
    const flat = pedalEq(kind, d);
    check(`${kind}: the starting point is flat (no filter runs, nothing exported)`, eqIsFlat(flat));
    check(`${kind}: seven bands in fixed places`, flat.params.bands.map((b) => b.type).join() === "lowcut,lowshelf,bell,bell,bell,highshelf,highcut");
    check(`${kind}: the knobs ride along in params.ui`, flat.params.ui.panel === kind && flat.params.ui.c.lf === d.lf);
    check(`${kind}: high-pass at its stop is off`, !hpfOn(kind, PEDALS[kind].hpf.min) && hpfOn(kind, PEDALS[kind].hpf.min + 5));
    check(`${kind}: low-pass at its top is off`, !lpfOn(kind, PEDALS[kind].lpf.max) && lpfOn(kind, PEDALS[kind].lpf.max - 100));
    const c = normalizePedal(kind, { lf: 1, mf: 1e6, hg: 99, lq: 0, lvl: -99 });
    check(`${kind}: knob values clamp to the published ranges`, c.lf === PEDALS[kind].low.min && c.mf === PEDALS[kind].mid.max && c.hg === 15 && c.lq === 1 && c.lvl === -24);
    const mid = pedalEq(kind, { ...d, mf: d.mf, mg: 6, mq: 2.5 });
    check(`${kind}: mid gain +6 boosts its frequency by 6 dB`, near(dbAt(mid, d.mf), 6, 0.05), String(dbAt(mid, d.mf)));
    const hp = pedalEq(kind, { ...d, hpf: 100 });
    check(`${kind}: the high-pass is 12 dB/oct Butterworth (-3 dB at its corner)`, hp.params.bands[0].slope === 12 && near(dbAt(hp, 100), -3.01, 0.05));
    const lvl = pedalEq(kind, { ...d, lvl: 4 });
    check(`${kind}: level is the lane's output gain`, lvl.params.out === 4 && near(dbAt(lvl, 1000), 4, 1e-9));
    const order = pedalEq(kind, { ...d, lg: 3 }).params.bands.map((b) => `${b.type}:${b.f}`);
    const order2 = pedalEq(kind, { ...d, lg: -3, mg: 2 }).params.bands.map((b) => `${b.type}:${b.f}`);
    check(`${kind}: turning gains never reorders the bands`, order.join() === order2.join());
    check(`${kind}: the descriptor survives the engine's normalisation`, JSON.stringify(normalizeEq(lvl)) === JSON.stringify(lvl));
  }
  check("guitar ranges as published", PEDALS.guitar.mid.min === 250 && PEDALS.guitar.mid.max === 5000 && PEDALS.guitar.hpf.max === 330 && PEDALS.guitar.lpf.min === 1500);
  check("bass ranges as published", PEDALS.bass.mid.min === 100 && PEDALS.bass.mid.max === 1600 && PEDALS.bass.high.max === 9700 && PEDALS.bass.lpf.min === 215);
  check("bass low shelf: below 75 Hz for a little boost", dynamicShelfFreq(2) === 75 && dynamicShelfFreq(-3) === 75);
  check("bass low shelf: up to 400 Hz at full boost", dynamicShelfFreq(15) === 400 && dynamicShelfFreq(-15) === 400);
  check("bass low shelf: in between, in between", dynamicShelfFreq(9) > 75 && dynamicShelfFreq(9) < 400);
  const bassShelf = pedalEq("bass", { ...pedalDefaults("bass"), ls: 15 });
  check("bass low shelf follows its gain in the descriptor", bassShelf.params.bands[1].f === 400);
  check("guitar low shelf stays at 200 Hz", pedalEq("guitar", { ...pedalDefaults("guitar"), ls: 15 }).params.bands[1].f === 200);
}

// ─── Keys ───
{
  const flat = keysEq(keysDefaults());
  check("keys: the starting point is flat", eqIsFlat(flat));
  check("keys: low cut, body, warmth, presence, air, tone", flat.params.bands.map((b) => b.type).join() === "lowcut,lowshelf,bell,bell,highshelf,tilt");
  const air = keysEq({ ...keysDefaults(), air: 6 });
  check("keys: air lifts the top, not the middle", dbAt(air, 16000) > 5 && Math.abs(dbAt(air, 500)) < 0.2);
  const tone = keysEq({ ...keysDefaults(), tone: 4 });
  check("keys: tone tilts (+ is brighter)", dbAt(tone, 10000) > 1.5 && dbAt(tone, 60) < -1.5);
  const pres = keysEq({ ...keysDefaults(), presence: 5, presF: 4000 });
  check("keys: presence peaks at its focus", near(dbAt(pres, 4000), 5, 0.05));
  check("keys: level is clamped to +-12", keysEq({ ...keysDefaults(), lvl: 30 }).params.out === 12);
}

// ─── Reading a saved EQ back into knobs ───
{
  const saved = pedalEq("bass", { ...pedalDefaults("bass"), mg: -4 });
  check("the panel's own save comes back as its knobs", controlsFromEq("bass", saved).mg === -4);
  check("another panel's save shows this panel's starting point", controlsFromEq("guitar", saved).mg === 0);
  check("no EQ: the starting point", controlsFromEq("keys", null).air === 0);
  check("panelEq picks the right mapping", panelEq("keys", keysDefaults()).params.ui.panel === "keys");
}

// ─── Presets ───
{
  for (const kind of ["graph", "drum"]) {
    for (const p of graphPresets(kind)) {
      const eq = normalizeEq({ params: { bands: p.bands } });
      check(`${p.key}: valid and audible`, eq.params.bands.length === p.bands.length && !eqIsFlat(eq) && eq.params.bands.every((b) => !bandIsNeutral(b)));
    }
  }
  for (const kind of ["bass", "guitar", "keys"]) {
    for (const p of controlPresets(kind)) {
      const eq = panelEq(kind, { ...(kind === "keys" ? keysDefaults() : pedalDefaults(kind)), ...p.c });
      const back = controlsFromEq(kind, eq);
      const kept = Object.entries(p.c).every(([k, v]) => near(back[k], v, 0.05));
      check(`${p.key}: inside the knobs' ranges and audible`, kept && !eqIsFlat(eq));
    }
  }
}

console.log(`\n${pass} passed, ${fail} failed`);
if (fail) process.exit(1);
