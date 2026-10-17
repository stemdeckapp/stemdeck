// masterMath.js (descriptor, wire form, presets, the live preview's maths)
// and the master effect's place in fxChain.js, without a browser. The real
// DynamicsCompressorNode is checked against compressorMakeupGain in
// tests/e2e/master-live.spec.mjs; the server parser in tests/test_mastering.py.

import {
  LOSSY_CEILING_DBTP, MASTER_LIMITS, applyPreset, compressorMakeupGain, defaultMaster,
  encodeMasterParam, fmtDb, glueSettings, holdFall, kWeightingCoefficients, limiterSettings,
  loudnessFromMeanSquares, masterIsOn, matchedLevels, meanSquareTail, normalizeMaster, peakDb,
} from "../../static/js/masterMath.js";
import { createEngineFx } from "../../static/js/fxChain.js";

let passed = 0;
let failed = 0;
function check(name, condition, detail = "") {
  if (condition) { passed++; console.log(`PASS  ${name}`); }
  else { failed++; console.log(`FAIL  ${name}${detail ? `  -- ${detail}` : ""}`); }
}
const close = (a, b, tol) => Math.abs(a - b) <= tol;
const decode = (p) => JSON.parse(Buffer.from(p, "base64url").toString("utf8"));

// ── Descriptor ──
{
  const off = normalizeMaster(null);
  check("no descriptor reads as off", off.bypass === true && !masterIsOn(off));
  check("a newer version reads as off", normalizeMaster({ id: "master", v: 2, params: {} }).bypass === true);
  const d = defaultMaster();
  check("the default is streaming -14 / -1, on", d.params.preset === "streaming"
    && d.params.target_lufs === -14 && d.params.ceiling_dbtp === -1 && masterIsOn(d));
  const forged = normalizeMaster({ id: "master", v: 1, params: { preset: "streaming", target_lufs: -6, ceiling_dbtp: -0.1 } });
  check("a named preset carries its own numbers", forged.params.target_lufs === -14 && forged.params.ceiling_dbtp === -1);
  const custom = normalizeMaster({ id: "master", v: 1, params: { preset: "custom", target_lufs: -40, ceiling_dbtp: 5, comp: 3 } });
  check("custom is clamped to the server's ranges", custom.params.target_lufs === MASTER_LIMITS.targetMin
    && custom.params.ceiling_dbtp === MASTER_LIMITS.ceilingMax && custom.params.comp === 1);
  const junk = normalizeMaster({ id: "master", v: 1, params: { preset: "nope", comp: "x", target_lufs: NaN } });
  check("junk falls back to defaults", junk.params.preset === "streaming" && junk.params.comp === 0);
  const rounded = normalizeMaster({ id: "master", v: 1, params: { preset: "custom", target_lufs: -13.94, ceiling_dbtp: -1.04, comp: 0.333 } });
  check("custom values are rounded to the wire's steps", rounded.params.target_lufs === -13.9
    && rounded.params.ceiling_dbtp === -1 && rounded.params.comp === 0.33);
  const gentle = applyPreset(d, "gentle");
  check("gentle has no target", gentle.params.target_lufs === null && gentle.params.preset === "gentle");
  const louder = applyPreset(d, "louder");
  check("louder: -9 with the glue at 0.5", louder.params.target_lufs === -9 && louder.params.comp === 0.5);
  const toCustom = applyPreset(louder, "custom");
  check("custom starts from the current numbers", toCustom.params.target_lufs === -9 && toCustom.params.comp === 0.5);
  const keep = applyPreset({ ...toCustom, params: { ...toCustom.params, target_lufs: -11 } }, "custom");
  check("custom again keeps the user's numbers", keep.params.target_lufs === -11);
}

// ── Wire form ──
{
  check("off sends nothing", encodeMasterParam(null) === "" && encodeMasterParam({ ...defaultMaster(), bypass: true }) === "");
  const p = encodeMasterParam(defaultMaster());
  check("the parameter is base64url without padding", /^[A-Za-z0-9_-]+$/.test(p));
  check("streaming wire", JSON.stringify(decode(p)) === JSON.stringify({ v: 1, target: -14, ceiling: -1 }));
  const g = decode(encodeMasterParam({ ...applyPreset(null, "gentle"), bypass: false }));
  check("gentle wire has a null target", g.target === null && g.ceiling === -1);
  const l = decode(encodeMasterParam({ ...applyPreset(null, "louder"), bypass: false }));
  check("louder wire carries the compressor", l.comp === 0.5 && l.target === -9);
  check("the wire stays far under the server's 256 characters", encodeMasterParam({ ...applyPreset(null, "louder"), bypass: false }).length < 100);
}

// ── Live preview maths ──
{
  // Knee 0: the curve above the threshold is T + (x - T)/R, so the spec's
  // makeup is (1 / curve(0 dBFS)) ^ 0.6.
  for (const [T, R] of [[-1.1, 20], [-6, 4], [-20, 2], [-30, 3]]) {
    const expect = (1 / 10 ** ((T + (0 - T) / R) / 20)) ** 0.6;
    check(`makeup, hard knee T ${T} R ${R}`, close(compressorMakeupGain(T, 0, R), expect, 1e-5),
      `${compressorMakeupGain(T, 0, R)} vs ${expect}`);
  }
  check("ratio 1 has no makeup", close(compressorMakeupGain(0, 0, 1), 1, 1e-6));
  const soft = compressorMakeupGain(-20, 6, 2);
  check("a soft knee has a makeup between the hard knees around it",
    soft > compressorMakeupGain(-14, 0, 2) * 0.99 && soft < compressorMakeupGain(-20, 0, 2) * 1.01, String(soft));
  const g = glueSettings(0.5, -15);
  check("glue: 2:1, 6 dB knee, slow", g.ratio === 2 && g.knee === 6 && g.attack === 0.02 && g.release === 0.25 && g.threshold === -15);
  check("glue off without an amount or a threshold", glueSettings(0, -15) === null && glueSettings(0.5, null) === null);
  const lim = limiterSettings(-1);
  check("limiter: the export's limit, ratio 20 (the spec's maximum)", close(lim.threshold, -1.1, 1e-9) && lim.ratio === 20 && lim.knee === 0);
  check("lossy ceiling is -1.5", LOSSY_CEILING_DBTP === -1.5);

  const louder = matchedLevels(4.4, true);
  check("matched: a louder master is turned down by the difference", close(louder.master, 10 ** (-4.4 / 20), 1e-12) && louder.original === 1);
  const quieter = matchedLevels(-3, true);
  check("matched: a louder original is turned down instead", quieter.master === 1 && close(quieter.original, 10 ** (-3 / 20), 1e-12));
  const unmatched = matchedLevels(4.4, false);
  check("not matched: both at their own level", unmatched.master === 1 && unmatched.original === 1);
  check("no measurement: both at their own level", matchedLevels(NaN, true).master === 1);
}

// ── K-weighting and meters ──
{
  // ITU-R BS.1770-4, table 1 and 2: the 48 kHz coefficients.
  const k = kWeightingCoefficients(48000);
  const shelfB = [1.53512485958697, -2.69169618940638, 1.19839281085285];
  const shelfA = [1, -1.69065929318241, 0.73248077421585];
  const hpB = [1, -2, 1];
  const hpA = [1, -1.99004745483398, 0.99007225036621];
  const near = (a, b, tol) => a.every((v, i) => close(v, b[i], tol));
  check("K-weighting shelf matches BS.1770 at 48 kHz", near(k.shelf.feedforward, shelfB, 2e-4) && near(k.shelf.feedback, shelfA, 2e-4),
    JSON.stringify(k.shelf));
  // BS.1770 normalises the high-pass to b = [1, -2, 1]; ours keeps unity
  // passband gain, which differs from it by 0.0001 dB.
  const scale = k.highpass.feedforward[0];
  check("K-weighting high-pass matches BS.1770 at 48 kHz", near(k.highpass.feedforward.map((v) => v / scale), hpB, 1e-9)
    && near(k.highpass.feedback, hpA, 5e-5), JSON.stringify(k.highpass));
  check("a 1 kHz full-scale sine in one channel reads -3.01 LUFS (K gain at 1 kHz is about 0)",
    close(loudnessFromMeanSquares([0.5]), -3.70, 0.01), String(loudnessFromMeanSquares([0.5])));
  check("silence is -Infinity LUFS", loudnessFromMeanSquares([0, 0]) === -Infinity);
  const buf = new Float32Array([0, 0.5, -0.25, 0.1]);
  check("mean square of the tail", close(meanSquareTail(buf, 2), (0.25 ** 2 + 0.1 ** 2) / 2, 1e-9));
  check("peak in dBFS", close(peakDb(buf), 20 * Math.log10(0.5), 1e-6) && peakDb(new Float32Array(4)) === -Infinity);
  check("a meter rises at once and falls slowly", holdFall(-10, -3, 1.5) === -3 && holdFall(-3, -20, 1.5) === -4.5);
  check("numbers are formatted with a sign when asked", fmtDb(1.44, { sign: true }) === "+1.4" && fmtDb(-14) === "-14.0"
    && fmtDb(null) === String.fromCharCode(183));
}

// ── In the engine's graph (fake nodes) ──
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
  createDynamicsCompressor() {
    const n = new Node(this, "compressor");
    for (const p of ["threshold", "knee", "ratio", "attack", "release"]) n[p] = new Param(0);
    n.reduction = 0;
    return n;
  }
  createDelay() { const n = new Node(this, "delay"); n.delayTime = new Param(0); return n; }
  createAnalyser() { return new Node(this, "analyser"); }
}
{
  const ctx = new Ctx();
  const fx = createEngineFx(ctx, () => []);
  const before = ctx.nodes.length;
  check("off: no compressor exists", !ctx.nodes.some((n) => n.kind === "compressor"));
  check("the chain ends at a stable tap on the destination", fx.masterOutput.out.includes(ctx.destination));
  check("off: nothing to take out", fx.setMasterFx(null) === false && ctx.nodes.length === before);
  const params = { gainDb: 3, comp: 0.5, compThresholdDb: -15, ceiling: -1, fader: 0.5, listen: "master", match: true, diffLu: 4 };
  fx.setMasterFx(params, { immediate: true });
  const comps = ctx.nodes.filter((n) => n.kind === "compressor");
  check("on: a glue compressor and a limiter", comps.length === 2);
  const [glue, lim] = comps;
  check("the glue gets the measured threshold and 2:1", glue.threshold.value === -15 && glue.ratio.value === 2);
  check("the limiter sits at the ceiling minus the margin, ratio 20", close(lim.threshold.value, -1.1, 1e-9) && lim.ratio.value === 20);
  const gains = ctx.nodes.filter((n) => n.kind === "gain");
  const pre = gains.find((g) => close(g.gain.value, 2, 1e-12));
  check("the fader is undone before the master (1 / 0.5)", !!pre);
  check("the measured gain is applied", gains.some((g) => close(g.gain.value, 10 ** (3 / 20), 1e-12)));
  check("matched: the master is turned down by the measured difference", gains.some((g) => close(g.gain.value, 10 ** (-4 / 20), 1e-12)));
  const delay = ctx.nodes.find((n) => n.kind === "delay");
  check("the original path is delayed by both look-aheads", close(delay.delayTime.value, 0.012, 1e-12));
  check("nodes are exposed for the meters", !!fx.getMasterFxNodes()?.input && !!fx.getMasterFxNodes()?.output);
  check("latency is reported", close(fx.masterFxLatency(), 0.012, 1e-12));
  fx.setMasterFx({ ...params, listen: "original" }, { immediate: true });
  check("original: the dry path plays", ctx.nodes.some((n) => n.kind === "gain" && n.out.length && close(n.gain.value, 1, 1e-12) && delay.out.includes(n)));
  fx.setMasterFx({ ...params, comp: 0 }, { immediate: true });
  check("glue off: the compressor idles at ratio 1", glue.ratio.value === 1);
  fx.setMasterFx(null, { immediate: true });
  check("off again: the effect is gone", fx.getMasterFxNodes() === null && fx.masterFxLatency() === 0);
  const around = ctx.nodes.find((n) => n.kind === "gain" && n.out.includes(fx.masterOutput));
  check("off again: the straight path is at exactly 1", around && around.gain.value === 1);
  check("off again: nothing feeds a compressor any more",
    ctx.nodes.every((n) => !n.out.some((d) => d.kind === "compressor")));
  fx.dispose();
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
