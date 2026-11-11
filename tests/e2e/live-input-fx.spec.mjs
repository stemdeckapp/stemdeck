// Live input effects (static/js/capture/inputFx.js, liveFxUi.js): an armed
// recording track's input heard through its own chain while Hear myself is
// on, in the real capture graph, against the real backend.
//
// Chromium's fake capture device plays a generated file in a loop (4 s at
// 48 kHz, mono): 1.9 s of a 330 Hz sine at 0.3, then four 10 ms Hann bursts
// of 1 kHz at 0.5, 500 ms apart, with silence between. The monitor output
// (capture.monitorNode) and the dry input are both recorded by the app's own
// recorder worklet on the same clock, so every number below is a sample
// count on the live path, not an offline render.
//
// 1. Gating: no host while nothing is armed, while Hear myself is off, in
//    Practice, and in Performance; one host with a chain, monitoring on.
// 2. The meter in the strip moves with the input.
// 3. A delay on the live path echoes at its set time (within 0.5 ms), on
//    both sides of a mono input; a ping-pong delay fed a mono input is
//    heard on both sides (the stereo-delay silence).
// 4. Bypass (the chain off, every pedal off) is bit-identical to no chain.
// 5. Reordering while monitoring makes no click (stated step bound).
// 6. Latency: the distortion adds its measured frames on the live path;
//    Low latency takes them away; the readout says both.
// 7. The take uploaded to the server is the dry input; its lane gets a copy
//    of the chain.
// 8. The chain, the instrument and Low latency come back after a reload.
// 9. The speakers warning, and the Guide's sentences for the new controls.
//
// What this cannot check: a real interface's round trip, feedback on real
// speakers, and how the effects feel to play through ("Needs a human" in
// the tracking notes).
import { test, expect } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { openStudio, chooseMode } from "./helpers.mjs";
import { removeTracks, addTrack, waitForEngine, recBtn } from "./recording-helpers.mjs";
import { pedalDefaults } from "../../static/js/fx/pedalSpecs.js";
import { TRANSLATIONS } from "../../static/js/i18n.js";

const FILE_RATE = 48000;
const WAV = path.join(os.tmpdir(), "stemdeck-e2e-live-input-fx.wav");

/** The fake input: mono 16-bit WAV, written before the browser starts. */
function writeFakeInput() {
  const n = FILE_RATE * 4;
  const pcm = Buffer.alloc(n * 2);
  for (let i = 0; i < n; i++) {
    const t = i / FILE_RATE;
    let v = 0;
    if (t < 1.9) {
      const fade = Math.min(1, t / 0.02, (1.9 - t) / 0.02);
      v = 0.3 * fade * Math.sin(2 * Math.PI * 330 * t);
    } else {
      for (const b of [2.0, 2.5, 3.0, 3.5]) {
        const u = t - b;
        if (u >= 0 && u < 0.01) v = 0.5 * Math.sin(2 * Math.PI * 1000 * u) * 0.5 * (1 - Math.cos((2 * Math.PI * u) / 0.01));
      }
    }
    pcm.writeInt16LE(Math.max(-32768, Math.min(32767, Math.round(v * 32767))), i * 2);
  }
  const h = Buffer.alloc(44);
  h.write("RIFF", 0); h.writeUInt32LE(36 + pcm.length, 4); h.write("WAVE", 8);
  h.write("fmt ", 12); h.writeUInt32LE(16, 16); h.writeUInt16LE(1, 20); h.writeUInt16LE(1, 22);
  h.writeUInt32LE(FILE_RATE, 24); h.writeUInt32LE(FILE_RATE * 2, 28); h.writeUInt16LE(2, 32); h.writeUInt16LE(16, 34);
  h.write("data", 36); h.writeUInt32LE(pcm.length, 40);
  fs.writeFileSync(WAV, Buffer.concat([h, pcm]));
}
writeFakeInput();

test.use({
  launchOptions: {
    args: [
      "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", `--use-file-for-fake-audio-capture=${WAV}`,
      "--mute-audio", "--autoplay-policy=no-user-gesture-required",
    ],
  },
  permissions: ["microphone"],
  viewport: { width: 1600, height: 900 },
  // The measuring helpers go in with eval, which the app's CSP refuses.
  bypassCSP: true,
});

test.beforeEach(async ({ request }) => removeTracks(request));
test.afterEach(async ({ request }) => removeTracks(request));

const pedal = (iid, type, p = {}, on = true) => ({ iid, type, on, preset: null, p: { ...pedalDefaults(type, "guitar"), ...p } });
const ECHO = { char: "digital", sync: "off", stereo: "mono", time: 0.15, fb: 0, mod: 0, duck: 0, mix: 0.5, lowcut: 20, highcut: 12000 };

/** The studio in Advanced, a new track, armed, its input open. Returns the track id. */
async function armedTrack(page) {
  await page.addInitScript(() => localStorage.setItem("stemdeck.mode", "daw"));
  await openStudio(page, { tauri: true });
  const id = await addTrack(page);
  await waitForEngine(page);
  await page.locator(`.track-arm[data-track="${id}"]`).click();
  await expect.poll(() => page.evaluate(async () => !!(await import("/js/capture/inputFx.js")).inputCapture()), { timeout: 15000 }).toBe(true);
  return id;
}

async function openPanel(page) {
  if (!(await page.locator("#recPanel").isVisible())) await page.locator("#t-rec-opts").click();
  await expect(page.locator("#recPanel")).toBeVisible();
}

async function monitor(page, on) {
  await openPanel(page);
  const box = page.locator("#recMonitor");
  if (on) await box.check(); else await box.uncheck();
}

const setChain = (page, id, desc) => page.evaluate(async ({ id, desc }) => {
  (await import("/js/capture/inputFx.js")).setInputChain(id, desc);
}, { id, desc });

const hosts = (page) => page.evaluate(async () => (await import("/js/capture/inputFx.js")).hostCount());

/**
 * Record `ms` of the dry input (the track's channels, as a take records
 * them) and of the monitor output (both sides) on the context's clock, the
 * two aligned frame for frame. `actions`: "reorder" swaps the first two
 * pedals every 250 ms and returns the context frames of each swap.
 */
async function capture(page, id, { ms = 4500, actions = null } = {}) {
  await page.evaluate(async ({ id, ms, actions }) => {
    const inp = await import("/js/capture/inputFx.js");
    const tl = await import("/js/capture/takeLanes.js");
    const c = inp.inputCapture();
    const ctx = c.ctx;
    const chans = tl.trackInput(tl.getTracks().find((t) => t.id === id)).channels;
    const dryRec = c.createRecorder(chans);
    const mon = new AudioWorkletNode(ctx, "capture-recorder", {
      numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1], channelCount: 2,
      channelCountMode: "explicit", channelInterpretation: "discrete", processorOptions: { channels: 2 },
    });
    const sink = ctx.createGain();
    sink.gain.value = 0;
    sink.connect(ctx.destination);
    c.monitorNode.connect(mon);
    mon.connect(sink);
    const dryB = [];
    const monB = [];
    let monStopped;
    const monDone = new Promise((r) => { monStopped = r; });
    mon.port.onmessage = (e) => {
      if (e.data.type === "batch") monB.push(e.data);
      else if (e.data.type === "stopped") monStopped();
    };
    await dryRec.start((m) => dryB.push(m));
    mon.port.postMessage({ type: "start" });
    const swaps = [];
    const t0 = performance.now();
    while (performance.now() - t0 < ms) {
      await new Promise((r) => setTimeout(r, 250));
      if (actions === "reorder") {
        const d = inp.getInputFx(id).chain;
        swaps.push(Math.round(ctx.currentTime * ctx.sampleRate));
        inp.setInputChain(id, { ...d, pedals: [d.pedals[1], d.pedals[0], ...d.pedals.slice(2)] });
      }
    }
    await dryRec.stop();
    mon.port.postMessage({ type: "stop" });
    await monDone;
    dryRec.dispose();
    try { c.monitorNode.disconnect(mon); } catch { /* gone */ }
    mon.disconnect();
    sink.disconnect();
    const span = (bs) => [bs[0].frame, bs.at(-1).frame + bs.at(-1).channels[0].length];
    const [d0, d1] = span(dryB);
    const [m0, m1] = span(monB);
    const base = Math.max(d0, m0);
    const n = Math.min(d1, m1) - base;
    const lay = (bs, ch) => {
      const out = new Float32Array(n);
      for (const b of bs) {
        const src = b.channels[ch] || b.channels[0];
        for (let i = 0; i < src.length; i++) {
          const k = b.frame + i - base;
          if (k >= 0 && k < n) out[k] = src[i];
        }
      }
      return out;
    };
    window.__cap = { rate: ctx.sampleRate, dry: lay(dryB, 0), L: lay(monB, 0), R: lay(monB, 1), swaps: swaps.map((f) => f - base) };
  }, { id, ms, actions });
}

/** The burst onsets in the dry capture (after silence), as frame indexes. */
const ONSETS = `
  window.__onsets = (x, rate) => {
    const out = [];
    let last = -Infinity;
    let armed = false;
    for (let i = 0; i < x.length; i++) {
      const a = Math.abs(x[i]);
      if (a >= 1e-3) { if (i - last > 0.2 * rate) armed = true; last = i; }
      if (armed && a > 0.02) { out.push(i); armed = false; }
    }
    // A burst, not the sine's start: silent again 12 ms to 100 ms later.
    return out.filter((o) => { for (let i = o + Math.round(0.012 * rate); i < Math.min(x.length, o + 0.1 * rate); i++) if (Math.abs(x[i]) >= 1e-3) return false; return true; });
  };
  window.__lag = (x, ref, centre, span) => {
    let best = -Infinity, bi = centre;
    const score = (lag) => { let s = 0; for (let i = 0; i < ref.length; i++) s += ref[i] * (x[lag + i] || 0); return s; };
    const m = new Map();
    for (let lag = centre - span; lag <= centre + span; lag++) { const s = score(lag); m.set(lag, s); if (s > best) { best = s; bi = lag; } }
    const a = m.get(bi - 1) ?? best, b = best, c = m.get(bi + 1) ?? best;
    const den = a - 2 * b + c;
    return { lag: den !== 0 ? bi + 0.5 * (a - c) / den : bi, score: best };
  };
`;
const installHelpers = (page) => page.evaluate((src) => { (0, eval)(src); }, ONSETS);

test.describe("live input effects", () => {
  test("gating: a host only for an armed input, with a chain, monitoring on, in Advanced", async ({ page }) => {
    const id = await armedTrack(page);
    await setChain(page, id, { pedals: [pedal("dddd", "delay", ECHO)] });
    await openPanel(page);
    // Armed, chain set, Hear myself off: nothing is built.
    await expect(page.locator("#recMonitor")).not.toBeChecked();
    await page.waitForTimeout(300);
    expect(await hosts(page)).toBe(0);
    await monitor(page, true);
    await expect.poll(() => hosts(page)).toBe(1);
    await monitor(page, false);
    await expect.poll(() => hosts(page)).toBe(0);
    await monitor(page, true);
    await expect.poll(() => hosts(page)).toBe(1);
    // Disarmed: the routes go, so does the host (the panel keeps the input open for its meter).
    // (A click outside the panel closes it: it is opened again to look.)
    await page.locator(`.track-arm[data-track="${id}"]`).click();
    await expect.poll(() => hosts(page)).toBe(0);
    await openPanel(page);
    await expect(page.locator("#recLiveFxEmpty")).toBeVisible();
    await page.locator(`.track-arm[data-track="${id}"]`).click();
    await expect.poll(() => hosts(page)).toBe(1);
    // Performance: no live chain for the studio's armed track (the gate is
    // Performance's own input there), the stem lanes none either.
    await chooseMode(page, "streamer");
    await expect.poll(() => hosts(page)).toBe(0);
    expect(await page.evaluate(async () => (await import("/js/fxEngine.js")).inputChainsAllowed("studio"))).toBe(false);
    expect(await page.evaluate(async () => (await import("/js/fxEngine.js")).inputChainsAllowed("performance"))).toBe(true);
    expect(await page.evaluate(async () => (await import("/js/state.js")).audioEngine?.chainHostCount?.() ?? 0)).toBe(0);
    await chooseMode(page, "daw");
    // Practice: leaving Advanced disarms every track, so nothing is built.
    await chooseMode(page, "practice");
    await expect.poll(() => hosts(page)).toBe(0);
    expect(await page.evaluate(async () => (await import("/js/fxEngine.js")).inputChainsAllowed())).toBe(false);
    expect(await page.evaluate(async () => (await import("/js/fxEngine.js")).inputChainsAllowed("studio"))).toBe(false);
  });

  test("the strip's meter moves with the input, and the Clip light stays off below full scale", async ({ page }) => {
    const id = await armedTrack(page);
    await openPanel(page);
    await expect(page.locator("#recLiveFx .fx-card")).toHaveCount(0);
    await expect(page.locator("#recLiveFx")).toBeVisible();
    const text = page.locator("#recLiveFx .fx-meter-text");
    await expect.poll(async () => {
      const v = parseFloat(await text.textContent());
      return Number.isFinite(v) ? v : -999;
    }, { timeout: 10000 }).toBeGreaterThan(-20);
    await expect(page.locator("#recLiveFx .fx-clip")).not.toHaveClass(/\bon\b/);
    // The trim moves what the meter reads (the feed into the pedals) once a chain exists.
    await setChain(page, id, { trim: -12, pedals: [pedal("dddd", "delay", ECHO)] });
    const peak = async () => page.evaluate(async (id) => {
      const inp = await import("/js/capture/inputFx.js");
      const tl = await import("/js/capture/takeLanes.js");
      const c = inp.inputCapture();
      let raw = 0;
      for (let i = 0; i < 20; i++) { raw = Math.max(raw, c.channelLevel(tl.trackInput(tl.getTracks().find((t) => t.id === id)).channels)); await new Promise((r) => setTimeout(r, 20)); }
      return raw;
    }, id);
    expect(await peak()).toBeGreaterThan(0.25);
  });

  test("a delay on the live path echoes at its set time, on both sides of a mono input", async ({ page }) => {
    const id = await armedTrack(page);
    await setChain(page, id, { pedals: [pedal("dddd", "delay", ECHO)] });
    await monitor(page, true);
    await expect.poll(() => hosts(page)).toBe(1);
    await page.waitForTimeout(500);
    await capture(page, id);
    await installHelpers(page);
    const r = await page.evaluate(() => {
      const { rate, dry, L, R } = window.__cap;
      const onsets = window.__onsets(dry, rate).filter((o) => o > 256 && o + 0.3 * rate < dry.length);
      const res = [];
      for (const o of onsets) {
        const ref = dry.slice(o - 64, o + Math.round(0.012 * rate));
        const direct = window.__lag(L, ref, o - 64, 32);
        const echoL = window.__lag(L, ref, o - 64 + Math.round(0.15 * rate), Math.round(0.005 * rate));
        const echoR = window.__lag(R, ref, o - 64 + Math.round(0.15 * rate), Math.round(0.005 * rate));
        res.push({ ms: ((echoL.lag - direct.lag) / rate) * 1000, msR: ((echoR.lag - direct.lag) / rate) * 1000, rel: echoL.score / direct.score, relR: echoR.score / direct.score, directLag: direct.lag - (o - 64) });
      }
      return { rate, res };
    });
    console.log(`[live-fx] rate ${r.rate}; echoes: ${r.res.map((x) => `${x.ms.toFixed(3)} ms (L ${x.rel.toFixed(2)}, R ${x.relR.toFixed(2)})`).join(", ")}; direct lag ${r.res.map((x) => x.directLag.toFixed(2)).join(", ")} frames`);
    expect(r.res.length).toBeGreaterThanOrEqual(2);
    for (const x of r.res) {
      expect(Math.abs(x.ms - 150)).toBeLessThan(0.5);
      expect(Math.abs(x.msR - 150)).toBeLessThan(0.5);
      expect(x.rel).toBeGreaterThan(0.3);
      expect(x.relR).toBeGreaterThan(0.3);
      expect(Math.abs(x.directLag)).toBeLessThan(1);
    }
  });

  test("a ping-pong delay fed a mono input is heard on both sides", async ({ page }) => {
    const id = await armedTrack(page);
    await setChain(page, id, { pedals: [pedal("dddd", "delay", { ...ECHO, stereo: "pingpong", fb: 0.5 })] });
    await monitor(page, true);
    await expect.poll(() => hosts(page)).toBe(1);
    await page.waitForTimeout(500);
    await capture(page, id);
    await installHelpers(page);
    const r = await page.evaluate(() => {
      const { rate, dry, L, R } = window.__cap;
      const onsets = window.__onsets(dry, rate).filter((o) => o + 0.45 * rate < dry.length);
      const e = (x, a, b) => { let s = 0; for (let i = a; i < b; i++) s += x[i] * x[i]; return s; };
      return onsets.map((o) => {
        const d = e(dry, o, o + Math.round(0.012 * rate));
        const a = o + Math.round(0.1 * rate);
        const b = o + Math.round(0.45 * rate);
        return { L: 10 * Math.log10(e(L, a, b) / d), R: 10 * Math.log10(e(R, a, b) / d) };
      });
    });
    console.log(`[live-fx] ping-pong echo energy vs the burst: ${r.map((x) => `L ${x.L.toFixed(1)} dB, R ${x.R.toFixed(1)} dB`).join("; ")}`);
    expect(r.length).toBeGreaterThanOrEqual(2);
    for (const x of r) {
      expect(x.L).toBeGreaterThan(-20);
      expect(x.R).toBeGreaterThan(-20);
    }
  });

  test("bypass is bit-identical to no chain on the live path", async ({ page }) => {
    const id = await armedTrack(page);
    await monitor(page, true);
    await page.waitForTimeout(600);
    const differs = async () => {
      await capture(page, id, { ms: 1500 });
      return page.evaluate(() => {
        const { dry, L, R } = window.__cap;
        let d = 0;
        let nz = 0;
        for (let i = 0; i < dry.length; i++) { if (L[i] !== dry[i] || R[i] !== dry[i]) d++; if (dry[i] !== 0) nz++; }
        return { d, nz, n: dry.length };
      });
    };
    const none = await differs();
    const all = (on) => ["delay", "chorus", "phaser", "reverb"].map((t, i) => pedal(`p00${i}`, t, t === "delay" ? ECHO : {}, on));
    // Each case is built from no chain: a pedal switched off lets its echoes
    // ring out by design (a 2.5 s reverb tail), so a case that follows
    // another would measure the tail, not the bypass.
    const fresh = async (desc) => {
      await setChain(page, id, { pedals: [] });
      await expect.poll(() => hosts(page)).toBe(0);
      await setChain(page, id, desc);
      await expect.poll(() => hosts(page)).toBe(1);
      await page.waitForTimeout(300);
      return differs();
    };
    const chainOff = await fresh({ on: false, trim: 6, pedals: all(true) });
    const pedalsOff = await fresh({ pedals: all(false) });
    // Low latency: a distortion switched off has no alignment delay to shift by.
    await page.evaluate(async (id) => (await import("/js/capture/inputFx.js")).setInputLowLatency(id, true), id);
    const driveOffLow = await fresh({ pedals: [...all(false), pedal("p009", "drive", {}, false)] });
    await page.evaluate(async (id) => (await import("/js/capture/inputFx.js")).setInputLowLatency(id, false), id);
    // Switched off while playing: once the tails have rung out (a 150 ms
    // echo with no feedback, a chorus), the input again, bit for bit.
    await fresh({ pedals: [pedal("p001", "delay", ECHO), pedal("p002", "chorus")] });
    await setChain(page, id, { pedals: [pedal("p001", "delay", ECHO, false), pedal("p002", "chorus", {}, false)] });
    await page.waitForTimeout(800);
    const afterTails = await differs();
    console.log(`[live-fx] samples differing from the dry input (of ${none.n}, ${none.nz} non-zero): no chain ${none.d}, chain off ${chainOff.d}, every pedal off ${pedalsOff.d}, drive off at Low latency ${driveOffLow.d}, switched off once the tails rang out ${afterTails.d}`);
    expect(none.nz).toBeGreaterThan(1000);
    expect(none.d).toBe(0);
    expect(chainOff.d).toBe(0);
    expect(pedalsOff.d).toBe(0);
    expect(driveOffLow.d).toBe(0);
    expect(afterTails.d).toBe(0);
  });

  test("reordering while monitoring makes no click", async ({ page }) => {
    const id = await armedTrack(page);
    await setChain(page, id, { pedals: [pedal("p001", "chorus"), pedal("p002", "phaser")] });
    await monitor(page, true);
    await expect.poll(() => hosts(page)).toBe(1);
    await page.waitForTimeout(500);
    await capture(page, id, { ms: 4500, actions: "reorder" });
    const r = await page.evaluate(() => {
      const { rate, dry, L, swaps } = window.__cap;
      const B = 128;
      const nb = Math.floor(dry.length / B);
      const rms = (x, b) => { let s = 0; for (let i = b * B; i < (b + 1) * B; i++) s += x[i] * x[i]; return Math.sqrt(s / B); };
      const sine = Array.from({ length: nb }, (_, b) => { const v = rms(dry, b); return v > 0.17 && v < 0.25; });
      const steady = (b) => { for (let k = b - 4; k <= b + 4; k++) if (!sine[k]) return false; return true; };
      const step = (b) => { let m = 0; for (let i = Math.max(1, b * B); i < (b + 1) * B; i++) m = Math.max(m, Math.abs(L[i] - L[i - 1])); return m; };
      const near = (b) => swaps.some((s) => b * B >= s - B && b * B < s + 0.15 * rate);
      let base = 0, during = 0, inWindows = 0;
      for (let b = 0; b < nb; b++) {
        if (!steady(b)) continue;
        if (near(b)) during = Math.max(during, step(b));
        else base = Math.max(base, step(b));
      }
      for (const s of swaps) { const b = Math.floor(s / B); if (steady(b) && steady(b + Math.floor(0.05 * rate / B))) inWindows++; }
      return { base, during, inWindows, swaps: swaps.length };
    });
    console.log(`[live-fx] reorder: ${r.swaps} swaps, ${r.inWindows} inside the steady sine; max step during ${r.during.toFixed(4)} against ${r.base.toFixed(4)} for the steady monitored signal`);
    expect(r.inWindows).toBeGreaterThanOrEqual(3);
    expect(r.base).toBeGreaterThan(0.005);
    // Stated bound: a change never steps more than 1.3 times the steady
    // signal's own largest step (a rewire without a crossfade measured 18x
    // offline, tests/e2e/fx-chain-dsp.spec.mjs).
    expect(r.during).toBeLessThan(1.3 * r.base);
  });

  test("latency: a distortion adds its measured frames on the live path, Low latency takes them away", async ({ page }) => {
    const id = await armedTrack(page);
    // Blend 0: the clean path only, which is aligned with the shaper's latency.
    await setChain(page, id, { pedals: [pedal("p001", "drive", { blend: 0, level: 0 })] });
    await monitor(page, true);
    await expect.poll(() => hosts(page)).toBe(1);
    await page.waitForTimeout(500);
    const lagNow = async () => {
      await capture(page, id, { ms: 1500 });
      return page.evaluate(() => {
        const { dry, L } = window.__cap;
        let best = -1;
        for (let lag = 0; lag < 600; lag++) {
          let d = 0;
          for (let i = 1000; i < dry.length; i++) if (L[i] !== dry[i - lag]) { d++; if (d > 10) break; }
          if (d === 0) { best = lag; break; }
        }
        return best;
      });
    };
    const info = await page.evaluate(async (id) => {
      const inp = await import("/js/capture/inputFx.js");
      const c = inp.inputCapture();
      const probe = new AudioContext({ latencyHint: 0 });
      const zeroHint = { base: probe.baseLatency, out: probe.outputLatency };
      await probe.close();
      return {
        frames: c.routeLatencyFrames(id), rate: c.sampleRate, rt: c.roundTripSeconds, calibrated: c.calibrated,
        base: c.ctx.baseLatency, out: c.ctx.outputLatency, zeroHint,
      };
    }, id);
    const lag4x = await lagNow();
    await expect(page.locator("#recLiveFxLatency")).toContainText(`${((info.frames / info.rate) * 1000).toFixed(1)} ms`);
    await page.locator("#recLiveFxLow").check();
    await expect.poll(() => page.evaluate(async (id) => (await import("/js/capture/inputFx.js")).inputCapture().routeLatencyFrames(id), id)).toBe(0);
    await page.waitForTimeout(400);
    const lagLow = await lagNow();
    await expect(page.locator("#recLiveFxLatency")).toContainText("0.0 ms");
    console.log(`[live-fx] headless round trip ${(info.rt * 1000).toFixed(1)} ms (${info.calibrated ? "calibrated" : "browser estimate"}: outputLatency ${(info.out * 1000).toFixed(1)} ms, baseLatency ${(info.base * 1000).toFixed(1)} ms; a latencyHint 0 context reports baseLatency ${(info.zeroHint.base * 1000).toFixed(1)} ms); distortion: ${info.frames} frames reported, live lag ${lag4x} frames at 4x, ${lagLow} with Low latency, at ${info.rate} Hz`);
    expect(info.frames).toBeGreaterThan(0);
    expect(lag4x).toBe(info.frames);
    expect(lagLow).toBe(0);
  });

  test("the take uploaded is the dry input, and its lane gets a copy of the chain", async ({ page }) => {
    const parts = [];
    page.on("request", (req) => {
      if (/\/takes\/t[0-9a-f]{8}\/parts\/audio/.test(req.url())) parts.push({ seq: Number(new URL(req.url()).searchParams.get("seq")), body: req.postDataBuffer() });
    });
    const id = await armedTrack(page);
    await setChain(page, id, { pedals: [pedal("dddd", "delay", ECHO)] });
    await monitor(page, true);
    await expect.poll(() => hosts(page)).toBe(1);
    await page.keyboard.press("Escape");
    await recBtn(page).click();
    await expect(recBtn(page)).toHaveClass(/recording/, { timeout: 10000 });
    await page.waitForFunction(async () => (await import("/js/state.js")).audioEngine?.isPlaying(), null, { timeout: 10000 });
    await page.waitForTimeout(4500);
    await recBtn(page).click();
    await expect(recBtn(page)).not.toHaveClass(/recording/);
    await expect.poll(() => page.evaluate(async (id) => (await import("/js/fxEngine.js")).getLaneChain(id).pedals.map((p) => p.type).join(), id), { timeout: 20000 }).toBe("delay");
    const lane = await page.evaluate(async (id) => (await import("/js/fxEngine.js")).getLaneChain(id), id);
    expect(lane.inst).toBe("guitar");
    expect(lane.pedals[0].p.time).toBeCloseTo(0.15, 6);
    const rate = await page.evaluate(async () => (await import("/js/state.js")).audioEngine.audioContext.sampleRate);
    // Parts are raw float32 of the track's channels (one here), in order.
    parts.sort((a, b) => a.seq - b.seq);
    const buf = Buffer.concat(parts.map((p) => p.body));
    const count = Math.floor(buf.length / 4);
    const x = new Float32Array(buf.buffer.slice(buf.byteOffset, buf.byteOffset + count * 4));
    const found = [];
    let last = -Infinity;
    let armed = false;
    for (let i = 0; i < x.length; i++) {
      const a = Math.abs(x[i]);
      if (a >= 1e-3) { if (i - last > 0.2 * rate) armed = true; last = i; }
      if (armed && a > 0.02) { found.push(i); armed = false; }
    }
    // Bursts only (silent again 12 ms to 100 ms later), not the sine's start.
    const onsets = found.filter((o) => { for (let i = o + Math.round(0.012 * rate); i < Math.min(x.length, o + 0.1 * rate); i++) if (Math.abs(x[i]) >= 1e-3) return false; return true; });
    const e = (a, b) => { let s = 0; for (let i = Math.max(0, a); i < Math.min(x.length, b); i++) s += x[i] * x[i]; return s; };
    // The fake input's burst, at the context's rate, against the take's.
    const n = Math.round(0.01 * rate);
    const ref = Float32Array.from({ length: n }, (_, i) => 0.5 * Math.sin(2 * Math.PI * 1000 * (i / rate)) * 0.5 * (1 - Math.cos((2 * Math.PI * i) / n)));
    const res = onsets.filter((o) => o + 0.2 * rate < x.length).map((o) => {
      let best = 0;
      let bestK = 0;
      for (let k = -40; k <= 10; k++) {
        let s = 0, a = 0, b = 0;
        for (let i = 0; i < n; i++) { const v = x[o + k + i] || 0; s += v * ref[i]; a += v * v; b += ref[i] * ref[i]; }
        const c = s / Math.sqrt(a * b || 1);
        if (c > best) { best = c; bestK = k; }
      }
      const burst = e(o - 48, o + n);
      const echo = e(o + Math.round(0.15 * rate) - 96, o + Math.round(0.15 * rate) + n + 96);
      return { match: best, k: bestK, echoDb: echo > 0 ? 10 * Math.log10(echo / burst) : -Infinity };
    });
    console.log(`[live-fx] take: ${parts.length} parts, ${x.length} frames at ${rate} Hz; bursts: ${res.map((r) => `match ${r.match.toFixed(4)}, echo window ${Number.isFinite(r.echoDb) ? r.echoDb.toFixed(1) + " dB" : "-inf dB (silent)"}`).join("; ")}`);
    expect(res.length).toBeGreaterThanOrEqual(2);
    for (const r of res) {
      expect(r.match).toBeGreaterThan(0.99);
      expect(r.echoDb).toBeLessThan(-60);
    }
  });

  test("the chain, the instrument and Low latency come back after a reload; Hear myself does not", async ({ page }) => {
    const id = await armedTrack(page);
    await openPanel(page);
    await page.locator("#recLiveFx .fx-add").click();
    await page.locator('#recLiveFx .fx-add-item[data-type="delay"]').click();
    await page.locator("#recLiveFx .fx-add").click();
    await page.locator('#recLiveFx .fx-add-item[data-type="drive"]').click();
    await expect(page.locator("#recLiveFx .fx-card")).toHaveCount(2);
    // Keyboard reorder: the delay first.
    const order = () => page.locator("#recLiveFx .fx-card .fx-card-main").allTextContents();
    expect((await order())[0]).toBe("Distortion");
    await page.locator("#recLiveFx .fx-card .fx-card-main").nth(1).focus();
    await page.keyboard.press("Alt+ArrowLeft");
    await expect.poll(order).toEqual(["Delay", "Distortion"]);
    await page.locator("#recLiveFx .fx-inst").selectOption("bass");
    await page.locator("#recLiveFxLow").check();
    await monitor(page, true);
    const before = await page.evaluate(async (id) => (await import("/js/capture/inputFx.js")).getInputFx(id), id);
    expect(before.chain.inst).toBe("bass");
    expect(before.low).toBe(true);
    // A new page load (the init scripts run again), the song opened again.
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.locator('.cat-item[data-id="e2e0deadbeef"]').first().click();
    await page.waitForFunction(() => !/\/\s*00:00\s*$/.test(document.querySelector("#t-time")?.textContent || "00:00 / 00:00"), null, { timeout: 20000 });
    await waitForEngine(page);
    const after = await page.evaluate(async (id) => (await import("/js/capture/inputFx.js")).getInputFx(id), id);
    expect(after).toEqual(before);
    await page.locator(`.track-arm[data-track="${id}"]`).click();
    await openPanel(page);
    await expect(page.locator("#recLiveFx .fx-card .fx-card-main")).toHaveText(["Delay", "Distortion"]);
    await expect(page.locator("#recLiveFx .fx-inst")).toHaveValue("bass");
    await expect(page.locator("#recLiveFxLow")).toBeChecked();
    await expect(page.locator("#recMonitor")).not.toBeChecked();
    await expect(page.locator("#recLiveFxOff")).toBeVisible();
  });

  test("the speakers warning shows only while monitoring plays to speakers", async ({ page }) => {
    await page.addInitScript(() => {
      const orig = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
      navigator.mediaDevices.enumerateDevices = async () => {
        const list = (await orig()).filter((d) => d.kind !== "audiooutput");
        const label = localStorage.getItem("e2e.output") || "Default - Speakers (Realtek(R) Audio)";
        return [...list, { kind: "audiooutput", deviceId: "default", groupId: "g", label, toJSON() { return this; } }];
      };
    });
    await armedTrack(page);
    const warn = page.locator("#recSpeakersWarn");
    await openPanel(page);
    await expect(warn).toBeHidden();
    await monitor(page, true);
    await expect(warn).toBeVisible();
    await expect(warn).toContainText("Speakers (Realtek(R) Audio)");
    await monitor(page, false);
    await expect(warn).toBeHidden();
    await page.evaluate(() => localStorage.setItem("e2e.output", "Speakers (Focusrite USB Audio)"));
    await monitor(page, true);
    await page.waitForTimeout(300);
    await expect(warn).toBeHidden();
  });

  test("the Guide explains the live effects controls", async ({ page }) => {
    const id = await armedTrack(page);
    await setChain(page, id, { pedals: [pedal("dddd", "delay", ECHO)] });
    await openPanel(page);
    await expect(page.locator("#recLiveFx .fx-card")).toHaveCount(1);
    // The Guide on without a click: a click on its button is a click outside
    // the recording panel, which closes the panel.
    await page.evaluate(async () => (await import("/js/helpMode.js")).setHelpMode(true));
    await expect(page.locator("#helpModeBtn")).toHaveAttribute("aria-pressed", "true");
    const tip = page.locator("#helpTip");
    const en = (key) => TRANSLATIONS.en[key];
    const cases = [
      ["#recLiveFx .rec-live-title", "help.liveFx"],
      ["#recLiveFx .fx-power", "help.liveFxPower"],
      ["#recLiveFx .fx-inst", "help.liveFxInstrument"],
      ["#recLiveFx .fx-trim .eq-knob-dial", "help.liveFxTrim"],
      ["#recLiveFx .fx-meter", "help.liveFxMeter"],
      ["#recLiveFx .fx-clip", "help.liveFxClip"],
      ["#recLiveFxLow", "help.liveFxLow"],
      ["#recLiveFx .fx-add", "help.liveFxAdd"],
      ["#recLiveFx .fx-preset", "help.liveFxPreset"],
      ["#recLiveFx .fx-card-main", "help.fxCard"],
    ];
    for (const [sel, key] of cases) {
      await page.locator(sel).first().hover();
      await expect(tip, sel).toContainText(en(key));
    }
  });

  // The add menu is a popup anchored to "+ Add effect" (fx/chainStrip.js),
  // never cut by the recording panel or the strip, and a press on it is a
  // press inside the panel (the panel stays open).
  for (const size of [{ width: 1280, height: 720 }, { width: 1280, height: 560 }]) {
    test(`the add menu is never clipped in the recording panel (${size.width}x${size.height})`, async ({ page }) => {
      await page.setViewportSize(size);
      await armedTrack(page);
      await openPanel(page);
      const add = page.locator("#recLiveFx .fx-add");
      await add.scrollIntoViewIfNeeded();
      await add.click();
      const menu = page.locator("#recLiveFx .fx-add-menu");
      await expect(menu).toBeVisible();
      const r = await page.evaluate(() => {
        const m = document.querySelector(".fx-add-menu");
        const vw = window.innerWidth;
        const vh = window.innerHeight;
        const items = [...m.querySelectorAll(".fx-add-item")];
        const bad = [];
        for (const it of items) {
          const b = it.getBoundingClientRect();
          if (!(b.width > 0 && b.height > 0 && b.left >= 0 && b.top >= 0 && b.right <= vw && b.bottom <= vh)) {
            bad.push(`${it.dataset.type} outside the window (${Math.round(b.top)}..${Math.round(b.bottom)} of ${vh})`);
            continue;
          }
          for (const [x, y] of [[b.left + b.width / 2, b.top + b.height / 2], [b.left + 4, b.top + 4], [b.right - 4, b.top + 4], [b.left + 4, b.bottom - 4], [b.right - 4, b.bottom - 4]]) {
            const hit = document.elementFromPoint(x, y);
            if (!(hit === it || it.contains(hit))) { bad.push(`${it.dataset.type} covered at ${Math.round(x)},${Math.round(y)} by ${hit?.className || hit?.tagName}`); break; }
          }
        }
        return { n: items.length, bad, scrolls: m.scrollHeight > m.clientHeight + 1 };
      });
      console.log(`[live-fx] add menu at ${size.width}x${size.height}: ${r.n} items, ${r.bad.length ? r.bad.join("; ") : "all on screen and uncovered"}`);
      expect(r.n).toBe(5);
      expect(r.bad).toEqual([]);
      expect(r.scrolls).toBe(false);
      // Escape closes the menu only: the panel stays, focus back on the button.
      await page.keyboard.press("Escape");
      await expect(menu).toHaveCount(0);
      await expect(page.locator("#recPanel")).toBeVisible();
      await expect(add).toBeFocused();
      // An item adds its pedal and the panel stays open.
      await add.click();
      await page.locator('#recLiveFx .fx-add-item[data-type="delay"]').click();
      await expect(page.locator("#recLiveFx .fx-card")).toHaveCount(1);
      await expect(page.locator("#recPanel")).toBeVisible();
    });
  }
});
