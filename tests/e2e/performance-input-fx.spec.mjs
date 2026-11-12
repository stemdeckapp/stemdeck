// Performance's live input effects (static/js/streamer/streamerFx.js): the
// musician's input heard through a chain while Hear myself is on, in the
// real capture graph Performance opens, against the real backend.
//
// Chromium's fake capture device plays a generated file in a loop (4 s at
// 48 kHz, mono): 1.9 s of a 330 Hz sine at 0.3, then four 10 ms Hann bursts
// of 1 kHz at 0.5, 500 ms apart, with silence between (the same signal as
// live-input-fx.spec.mjs). The monitor output (capture.monitorNode) and the
// dry input are recorded by the app's own recorder worklet on one clock, so
// every number is a sample count on the live path.
//
// 1. Gating: no host while Hear myself is off; one with it on; the stem lanes
//    build none in Performance, Practice or Karaoke; Practice and Karaoke
//    build no input chain. The strip's meter moves.
// 2. A delay echoes at its set time (within 0.5 ms) on both sides.
// 3. Bypass (chain off, every pedal off) is bit-identical to no chain.
// 4. Reordering while monitoring makes no click (stated step bound).
// 5. Latency readout: the distortion's measured frames, gone with Low latency.
// 6. A take is the dry input; it keeps the chain, its lane plays it, the take
//    lane's Effects switch turns it off and on, and the MP4 export prints it
//    (take_print) only while it is on.
// 7. The chain, the instrument and Low latency come back after a reload;
//    Hear myself does not.
// 8. The speakers warning; the add menu is never clipped; the Guide.
//
// The live effects are in Performance's Effects tab (streamerTabs.js), beside
// the Timeline tab: switching tabs keeps the chain and the monitor playing,
// and a take recorded while Effects shows reaches the timeline.
//
// What this cannot check: a real interface's round trip, feedback on real
// speakers, how the effects feel to play through (tracking notes, "Needs a
// human").
import { test, expect } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { openStudio, chooseMode, JOB_ID } from "./helpers.mjs";
import { removeTracks } from "./recording-helpers.mjs";
import { pedalDefaults } from "../../static/js/fx/pedalSpecs.js";
import { TRANSLATIONS } from "../../static/js/i18n.js";

const FILE_RATE = 48000;
const WAV = path.join(os.tmpdir(), "stemdeck-e2e-performance-input-fx.wav");

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
  permissions: ["microphone", "camera"],
  viewport: { width: 1800, height: 1020 },
  // The measuring helpers go in with eval, which the app's CSP refuses.
  bypassCSP: true,
});

// A take makes a Performance track on the shared backend: each test takes
// its tracks away again.
test.beforeEach(async ({ request }) => removeTracks(request));
test.afterEach(async ({ request }) => removeTracks(request));

const ROUTE = "performance";
const pedal = (iid, type, p = {}, on = true) => ({ iid, type, on, preset: null, p: { ...pedalDefaults(type, "guitar"), ...p } });
const ECHO = { char: "digital", sync: "off", stereo: "mono", time: 0.15, fb: 0, mod: 0, duck: 0, mix: 0.5, lowcut: 20, highcut: 12000 };
const sm = (page, sel) => page.locator(`#streamer ${sel}`);

const perf = (page, fn, arg) => page.evaluate(async ({ src, arg }) => {
  const m = await import("/js/streamer/streamerFx.js");
  return (0, eval)(`(${src})`)(m.perfFx, arg, m);
}, { src: fn.toString(), arg });

const hosts = (page) => perf(page, (p) => p.hostCount());
const setChain = (page, desc) => perf(page, (p, d) => { p.setInputChain("performance", d); }, desc);
const engineHosts = (page) => page.evaluate(async () => (await import("/js/state.js")).audioEngine?.chainHostCount?.() ?? 0);
const hearing = (page) => sm(page, "#streamerHear");

/** Press "Allow camera and microphone" when it shows (streamer-media.spec.mjs). */
async function allowDevices(page) {
  const allow = sm(page, ".sm-cam-act");
  if (await allow.isVisible()) await allow.click({ timeout: 3000 }).catch(() => {});
}

/** Performance open on the fixture, its input open. */
async function openPerformance(page, opts = {}) {
  if (!opts.skipStudio) await openStudio(page, opts);
  await chooseMode(page, "streamer");
  await expect(page.locator("#streamer")).toBeVisible();
  await allowDevices(page);
  await expect.poll(() => perf(page, (p) => !!p.inputCapture()), { timeout: 15000 }).toBe(true);
}

/** Show one of the timeline card's tabs ("timeline" or "effects"). */
async function showTab(page, name) {
  const tab = page.locator(`#streamerTab-${name}`);
  if ((await tab.getAttribute("aria-selected")) !== "true") await tab.click();
  await expect(tab).toHaveAttribute("aria-selected", "true");
}

/** The monitor's gain as it plays now (0 while Hear myself is off). */
const monitorGain = (page) => perf(page, (p) => p.inputCapture()?.monitorNode?.gain.value ?? -1);

async function hear(page, on) {
  // Hear myself is in the Effects tab.
  await showTab(page, "effects");
  const sw = hearing(page);
  if ((await sw.getAttribute("aria-checked")) !== String(on)) await sw.click();
  await expect(sw).toHaveAttribute("aria-checked", String(on));
}

async function openSection(page) {
  await showTab(page, "effects");
  await expect(page.locator("#streamerFx")).toBeVisible();
}

/**
 * Record `ms` of the dry input (the channel a take records) and of the
 * monitor output (both sides) on the context's clock, aligned frame for
 * frame. `actions`: "reorder" swaps the first two pedals every 250 ms.
 */
async function capture(page, { ms = 4500, actions = null } = {}) {
  await page.evaluate(async ({ ms, actions }) => {
    const { perfFx } = await import("/js/streamer/streamerFx.js");
    const c = perfFx.inputCapture();
    const ctx = c.ctx;
    const dryRec = c.createRecorder([0]);
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
        const d = perfFx.getInputFx("performance").chain;
        swaps.push(Math.round(ctx.currentTime * ctx.sampleRate));
        perfFx.setInputChain("performance", { ...d, pedals: [d.pedals[1], d.pedals[0], ...d.pedals.slice(2)] });
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
  }, { ms, actions });
}

const HELPERS = `
  window.__onsets = (x, rate) => {
    const out = [];
    let last = -Infinity;
    let armed = false;
    for (let i = 0; i < x.length; i++) {
      const a = Math.abs(x[i]);
      if (a >= 1e-3) { if (i - last > 0.2 * rate) armed = true; last = i; }
      if (armed && a > 0.02) { out.push(i); armed = false; }
    }
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
const installHelpers = (page) => page.evaluate((src) => { (0, eval)(src); }, HELPERS);

/**
 * Every item of the open add menu fully on screen and not covered or cut by
 * anything: the item itself is what the page has at its centre and at four
 * points 4 px inside its corners (its 4 px rounded corners are not
 * hit-tested), and the menu needs no scrolling to show it.
 */
async function menuOnScreen(page) {
  return page.evaluate(() => {
    const menu = document.querySelector(".fx-add-menu");
    if (!menu) return { ok: false, why: "no menu" };
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const items = [...menu.querySelectorAll(".fx-add-item")];
    const bad = [];
    for (const it of items) {
      const r = it.getBoundingClientRect();
      if (!(r.width > 0 && r.height > 0 && r.left >= 0 && r.top >= 0 && r.right <= vw && r.bottom <= vh)) {
        bad.push(`${it.dataset.type} outside the window (${Math.round(r.top)}..${Math.round(r.bottom)} of ${vh})`);
        continue;
      }
      for (const [x, y] of [[r.left + r.width / 2, r.top + r.height / 2], [r.left + 4, r.top + 4], [r.right - 4, r.top + 4], [r.left + 4, r.bottom - 4], [r.right - 4, r.bottom - 4]]) {
        const hit = document.elementFromPoint(x, y);
        if (!(hit === it || it.contains(hit))) {
          const stack = document.elementsFromPoint(x, y).slice(0, 4).map((e) => `${e.tagName}.${e.className}`).join(" > ");
          const mr = menu.getBoundingClientRect();
          bad.push(`${it.dataset.type} covered at ${Math.round(x)},${Math.round(y)} by ${hit?.className || hit?.tagName} (item ${Math.round(r.left)},${Math.round(r.top)} ${Math.round(r.width)}x${Math.round(r.height)}, menu ${Math.round(mr.left)},${Math.round(mr.top)} ${Math.round(mr.width)}x${Math.round(mr.height)}, stack ${stack})`);
          break;
        }
      }
    }
    return { ok: items.length === 5 && !bad.length, n: items.length, bad, scrolls: menu.scrollHeight > menu.clientHeight + 1 };
  });
}

test.describe("Performance input effects", () => {
  test("gating, and the strip's meter moves with the input", async ({ page }) => {
    // A saved Advanced chain on a stem lane: it plays in Advanced only.
    await page.addInitScript(({ key, chain }) => {
      if (sessionStorage.getItem("seeded")) return;
      sessionStorage.setItem("seeded", "1");
      localStorage.setItem(key, JSON.stringify({ bass: { volume: 1, muted: false, soloed: false, pitch: 0, fx: [chain] } }));
      localStorage.setItem("stemdeck.mode", "daw");
    }, { key: `stemdeck:mix:${JOB_ID}:daw`, chain: { id: "chain", v: 1, inst: null, on: true, trim: 0, pedals: [{ iid: "bbbb", type: "delay", on: true, preset: null, p: pedalDefaults("delay", "bass") }] } });
    await openStudio(page);
    await expect.poll(() => engineHosts(page)).toBeGreaterThan(0);
    await openPerformance(page, { skipStudio: true });
    // The stem lane: none in Performance.
    await expect.poll(() => engineHosts(page)).toBe(0);
    expect(await page.evaluate(async () => (await import("/js/fxEngine.js")).inputChainsAllowed("performance"))).toBe(true);
    expect(await page.evaluate(async () => (await import("/js/fxEngine.js")).inputChainsAllowed("studio"))).toBe(false);
    await setChain(page, { inst: "guitar", pedals: [pedal("dddd", "delay", ECHO)] });
    // An input open and a chain, Hear myself off (as at every page load): nothing built.
    await expect(hearing(page)).toHaveAttribute("aria-checked", "false");
    await page.waitForTimeout(300);
    expect(await hosts(page)).toBe(0);
    await hear(page, true);
    await expect.poll(() => hosts(page)).toBe(1);
    expect(await engineHosts(page)).toBe(0);
    await hear(page, false);
    await expect.poll(() => hosts(page)).toBe(0);
    await hear(page, true);
    await expect.poll(() => hosts(page)).toBe(1);
    // The Timeline tab in its place: the chain and the monitor play on.
    await expect.poll(() => monitorGain(page)).toBeGreaterThan(0.9);
    await showTab(page, "timeline");
    await expect(page.locator("#streamerFx")).toBeHidden();
    await page.waitForTimeout(300);
    expect(await hosts(page)).toBe(1);
    expect(await monitorGain(page)).toBeGreaterThan(0.9);
    await expect(hearing(page)).toHaveAttribute("aria-checked", "true");
    // No Audio Source: the input closes, nothing is built, the section says so.
    await openSection(page);
    await page.locator("#streamerAudioSrc").selectOption("none");
    await expect.poll(() => hosts(page)).toBe(0);
    await expect(page.locator("#streamerFxState")).toHaveText(TRANSLATIONS.en["streamer.fx.noInput"]);
    await page.locator("#streamerAudioSrc").selectOption({ index: 1 });
    await expect.poll(() => hosts(page), { timeout: 15000 }).toBe(1);
    // The meter reads the input going into the pedals.
    const text = page.locator("#streamerFx .fx-meter-text");
    await expect.poll(async () => {
      const v = parseFloat(await text.textContent());
      return Number.isFinite(v) ? v : -999;
    }, { timeout: 10000 }).toBeGreaterThan(-20);
    await expect(page.locator("#streamerFx .fx-clip")).not.toHaveClass(/\bon\b/);
    // Leaving Performance: the input closes and Hear myself goes off.
    await chooseMode(page, "practice");
    await expect.poll(() => hosts(page)).toBe(0);
    expect(await page.evaluate(async () => (await import("/js/fxEngine.js")).inputChainsAllowed())).toBe(false);
    expect(await engineHosts(page)).toBe(0);
    await chooseMode(page, "karaoke");
    await expect.poll(() => page.evaluate(async () => (await import("/js/mixer.js")).getMixMode())).toBe("karaoke");
    expect(await page.evaluate(async () => (await import("/js/fxEngine.js")).inputChainsAllowed())).toBe(false);
    expect(await hosts(page)).toBe(0);
    expect(await engineHosts(page)).toBe(0);
    await chooseMode(page, "streamer");
    await allowDevices(page);
    await expect.poll(() => perf(page, (p) => !!p.inputCapture()), { timeout: 15000 }).toBe(true);
    await expect(hearing(page)).toHaveAttribute("aria-checked", "false");
    expect(await hosts(page)).toBe(0);
  });

  test("a delay on the live path echoes at its set time, on both sides of a mono input", async ({ page }) => {
    await openPerformance(page);
    await setChain(page, { inst: "guitar", pedals: [pedal("dddd", "delay", ECHO)] });
    await hear(page, true);
    await expect.poll(() => hosts(page)).toBe(1);
    await page.waitForTimeout(500);
    await capture(page);
    await installHelpers(page);
    const r = await page.evaluate(() => {
      const { rate, dry, L, R } = window.__cap;
      const onsets = window.__onsets(dry, rate).filter((o) => o > 256 && o + 0.3 * rate < dry.length);
      const res = onsets.map((o) => {
        const ref = dry.slice(o - 64, o + Math.round(0.012 * rate));
        const direct = window.__lag(L, ref, o - 64, 32);
        const echoL = window.__lag(L, ref, o - 64 + Math.round(0.15 * rate), Math.round(0.005 * rate));
        const echoR = window.__lag(R, ref, o - 64 + Math.round(0.15 * rate), Math.round(0.005 * rate));
        const self = ref.reduce((s, v) => s + v * v, 0);
        return { ms: ((echoL.lag - direct.lag) / rate) * 1000, msR: ((echoR.lag - direct.lag) / rate) * 1000, rel: echoL.score / self, relR: echoR.score / self, directLag: direct.lag - (o - 64) };
      });
      return { rate, res };
    });
    console.log(`[perf-fx] delay set 150 ms at ${r.rate} Hz: ${r.res.map((x) => `L ${x.ms.toFixed(3)} ms, R ${x.msR.toFixed(3)} ms, echo/direct ${x.rel.toFixed(2)}`).join("; ")}`);
    expect(r.res.length).toBeGreaterThanOrEqual(2);
    for (const x of r.res) {
      expect(Math.abs(x.ms - 150)).toBeLessThan(0.5);
      expect(Math.abs(x.msR - 150)).toBeLessThan(0.5);
      expect(x.rel).toBeGreaterThan(0.3);
      expect(x.relR).toBeGreaterThan(0.3);
      expect(Math.abs(x.directLag)).toBeLessThan(1);
    }
  });

  test("bypass is bit-identical to no chain on the live path", async ({ page }) => {
    await openPerformance(page);
    await hear(page, true);
    await page.waitForTimeout(600);
    const differs = async () => {
      await capture(page, { ms: 1500 });
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
    const fresh = async (desc) => {
      await setChain(page, { pedals: [] });
      await expect.poll(() => hosts(page)).toBe(0);
      await setChain(page, desc);
      await expect.poll(() => hosts(page)).toBe(1);
      await page.waitForTimeout(300);
      return differs();
    };
    const chainOff = await fresh({ inst: "guitar", on: false, trim: 6, pedals: all(true) });
    const pedalsOff = await fresh({ inst: "guitar", pedals: all(false) });
    console.log(`[perf-fx] samples differing from the dry input (of ${none.n}, ${none.nz} non-zero): no chain ${none.d}, chain off ${chainOff.d}, every pedal off ${pedalsOff.d}`);
    expect(none.nz).toBeGreaterThan(1000);
    expect(none.d).toBe(0);
    expect(chainOff.d).toBe(0);
    expect(pedalsOff.d).toBe(0);
  });

  test("reordering while monitoring makes no click", async ({ page }) => {
    await openPerformance(page);
    await setChain(page, { inst: "guitar", pedals: [pedal("p001", "chorus"), pedal("p002", "phaser")] });
    await hear(page, true);
    await expect.poll(() => hosts(page)).toBe(1);
    await page.waitForTimeout(500);
    await capture(page, { ms: 4500, actions: "reorder" });
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
    console.log(`[perf-fx] reorder: ${r.swaps} swaps, ${r.inWindows} inside the steady sine; max step during ${r.during.toFixed(4)} against ${r.base.toFixed(4)} for the steady monitored signal`);
    expect(r.inWindows).toBeGreaterThanOrEqual(3);
    expect(r.base).toBeGreaterThan(0.005);
    // The same stated bound as the recording panel's: under 1.3 times the
    // steady signal's own largest step.
    expect(r.during).toBeLessThan(1.3 * r.base);
  });

  test("the latency readout: the distortion's measured frames, gone with Low latency", async ({ page }) => {
    await openPerformance(page);
    await setChain(page, { inst: "guitar", pedals: [pedal("p001", "drive", { blend: 0, level: 0 })] });
    await hear(page, true);
    await expect.poll(() => hosts(page)).toBe(1);
    await openSection(page);
    await page.waitForTimeout(500);
    const lagNow = async () => {
      await capture(page, { ms: 1500 });
      return page.evaluate(() => {
        const { dry, L } = window.__cap;
        for (let lag = 0; lag < 600; lag++) {
          let d = 0;
          for (let i = 1000; i < dry.length; i++) if (L[i] !== dry[i - lag]) { d++; if (d > 10) break; }
          if (d === 0) return lag;
        }
        return -1;
      });
    };
    const info = await perf(page, (p) => {
      const c = p.inputCapture();
      return { frames: c.routeLatencyFrames("performance"), rate: c.sampleRate, rt: c.roundTripSeconds, calibrated: c.calibrated, base: c.ctx.baseLatency, out: c.ctx.outputLatency };
    });
    const lag4x = await lagNow();
    const line = page.locator("#streamerFxLatency");
    await expect(line).toContainText(`${((info.frames / info.rate) * 1000).toFixed(1)} ms`);
    await expect(line).toContainText(TRANSLATIONS.en[info.calibrated ? "rec.liveFx.measured" : "rec.liveFx.estimated"]);
    await expect(line).toContainText(`${Math.round(info.rt * 1000)} ms round trip`);
    await page.locator("#streamerFx .sm-fx-low").click();
    await expect(page.locator("#streamerFx .sm-fx-low")).toHaveAttribute("aria-checked", "true");
    await expect.poll(() => perf(page, (p) => p.inputCapture().routeLatencyFrames("performance"))).toBe(0);
    await page.waitForTimeout(400);
    const lagLow = await lagNow();
    await expect(line).toContainText("0.0 ms");
    console.log(`[perf-fx] round trip ${(info.rt * 1000).toFixed(1)} ms (${info.calibrated ? "calibrated" : "browser estimate"}: outputLatency ${(info.out * 1000).toFixed(1)} ms, baseLatency ${(info.base * 1000).toFixed(1)} ms); distortion ${info.frames} frames reported, live lag ${lag4x} at 4x, ${lagLow} with Low latency, at ${info.rate} Hz`);
    expect(info.frames).toBeGreaterThan(0);
    expect(lag4x).toBe(info.frames);
    expect(lagLow).toBe(0);
  });

  test("a take is the dry input; it plays and exports through its own copy of the chain", async ({ page }) => {
    test.setTimeout(240000);
    const parts = [];
    const prints = [];
    const renders = [];
    page.on("request", (req) => {
      const url = req.url();
      if (/\/takes\/t[0-9a-f]{8}\/parts\/audio/.test(url)) parts.push({ seq: Number(new URL(url).searchParams.get("seq")), body: req.postDataBuffer() });
      if (req.method() === "POST" && /\/fx-prints$/.test(new URL(url).pathname)) prints.push(req.postDataJSON());
      if (req.method() === "POST" && /\/takes\/t[0-9a-f]{8}\/render$/.test(new URL(url).pathname)) renders.push(req.postDataJSON());
    });
    // The trim from 0.5 s to 5 s, so the take stops by itself.
    await openStudio(page);
    await page.evaluate(async ([id]) => {
      const m = await import("/js/streamer/streamerModel.js");
      const { storeSet } = await import("/js/utils.js");
      await storeSet(`stemdeck:streamer:scene:${id}`, m.serializeScene({ ...m.defaultScene("16:9"), trim: { in: 0.5, out: 5 } }));
    }, [JOB_ID]);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    await page.waitForFunction(() => !/\/\s*00:00\s*$/.test(document.querySelector("#t-time")?.textContent || "00:00 / 00:00"));
    await openPerformance(page, { skipStudio: true });
    await setChain(page, { inst: "voice", pedals: [pedal("dddd", "delay", { ...ECHO, duck: 0 })] });
    await hear(page, true);
    await expect.poll(() => hosts(page)).toBe(1);
    const recording = () => page.evaluate(() => document.querySelector("#streamer .sm-rec")?.classList.contains("recording"));
    await sm(page, ".sm-rec").click();
    await expect.poll(recording, { timeout: 10000 }).toBe(true);
    await expect.poll(recording, { timeout: 30000 }).toBe(false);
    await expect(sm(page, ".sm-export-go")).toBeEnabled({ timeout: 30000 });
    const ids = await page.evaluate(async (job) => {
      const r = await import("/js/streamer/streamerRecord.js");
      return { track: r.streamerTrackId(job), take: r.lastTake(job) };
    }, JOB_ID);
    expect(ids.track).toMatch(/^k[0-9a-f]{8}$/);
    expect(ids.take).toMatch(/^t[0-9a-f]{8}$/);
    // The take keeps its chain, and its lane plays it (in Performance only).
    const kept = await page.evaluate(async (take) => (await import("/js/streamer/streamerRecord.js")).takeInfo(take)?.fx, ids.take);
    expect(kept.inst).toBe("voice");
    expect(kept.chain.pedals.map((p) => p.type)).toEqual(["delay"]);
    await expect.poll(() => page.evaluate(async (id) => (await import("/js/fxEngine.js")).getLaneChain(id).pedals.map((p) => p.type).join(), ids.track), { timeout: 30000 }).toBe("delay");
    await expect.poll(() => engineHosts(page), { timeout: 15000 }).toBe(1);
    // Dry: the parts uploaded are the fake input's bursts and nothing where the echo would be.
    const rate = await page.evaluate(async () => (await import("/js/state.js")).audioEngine.audioContext.sampleRate);
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
    const onsets = found.filter((o) => { for (let i = o + Math.round(0.012 * rate); i < Math.min(x.length, o + 0.1 * rate); i++) if (Math.abs(x[i]) >= 1e-3) return false; return true; });
    const e = (a, b) => { let s = 0; for (let i = Math.max(0, a); i < Math.min(x.length, b); i++) s += x[i] * x[i]; return s; };
    const n = Math.round(0.01 * rate);
    const ref = Float32Array.from({ length: n }, (_, i) => 0.5 * Math.sin(2 * Math.PI * 1000 * (i / rate)) * 0.5 * (1 - Math.cos((2 * Math.PI * i) / n)));
    const res = onsets.filter((o) => o + 0.2 * rate < x.length).map((o) => {
      let best = 0;
      for (let k = -40; k <= 10; k++) {
        let s = 0, a = 0, b = 0;
        for (let i = 0; i < n; i++) { const v = x[o + k + i] || 0; s += v * ref[i]; a += v * v; b += ref[i] * ref[i]; }
        best = Math.max(best, s / Math.sqrt(a * b || 1));
      }
      const burst = e(o - 48, o + n);
      const echo = e(o + Math.round(0.15 * rate) - 96, o + Math.round(0.15 * rate) + n + 96);
      return { match: best, echoDb: echo > 0 ? 10 * Math.log10(echo / burst) : -Infinity };
    });
    console.log(`[perf-fx] take: ${parts.length} parts, ${x.length} frames at ${rate} Hz; bursts: ${res.map((r) => `match ${r.match.toFixed(4)}, echo window ${Number.isFinite(r.echoDb) ? r.echoDb.toFixed(1) + " dB" : "-inf dB (silent)"}`).join("; ")}`);
    expect(res.length).toBeGreaterThanOrEqual(2);
    for (const r of res) {
      expect(r.match).toBeGreaterThan(0.99);
      expect(r.echoDb).toBeLessThan(-60);
    }
    // The take lane's Effects switch: on, then off (the lane's chain off), then on.
    // The take was recorded while the Effects tab showed: the Timeline tab
    // has it, its lane and its Effects switch.
    await expect(page.locator("#streamerTab-effects")).toHaveAttribute("aria-selected", "true");
    await showTab(page, "timeline");
    const fxBtn = sm(page, ".sm-tl-fx");
    await expect(fxBtn).toBeVisible({ timeout: 15000 });
    await expect(fxBtn).toHaveAttribute("aria-pressed", "true");
    await expect(fxBtn).toHaveAccessibleName(TRANSLATIONS.en["streamer.lane.takeFx"]);
    // The MP4 export with the effects on: the take printed, the print rendered.
    let download = page.waitForEvent("download", { timeout: 120000 });
    await sm(page, ".sm-export-go").click();
    await download;
    await expect(sm(page, ".sm-export-text")).toContainText("Saved", { timeout: 30000 });
    expect(prints.map((p) => p.lane)).toEqual([ids.take]);
    expect(renders.at(-1).take_print).toMatch(/^[0-9a-f]{40}$/);
    // Off: the lane's chain is off and the export is the take as recorded.
    await fxBtn.click();
    await expect(fxBtn).toHaveAttribute("aria-pressed", "false");
    await expect.poll(() => page.evaluate(async (id) => (await import("/js/fxEngine.js")).getLaneChain(id).on, ids.track)).toBe(false);
    download = page.waitForEvent("download", { timeout: 120000 });
    await sm(page, ".sm-export-go").click();
    await download;
    await expect(sm(page, ".sm-export-text")).toContainText("Saved", { timeout: 30000 });
    expect(renders.length).toBe(2);
    expect(renders.at(-1).take_print).toBeUndefined();
    await fxBtn.click();
    await expect(fxBtn).toHaveAttribute("aria-pressed", "true");
    await expect.poll(() => page.evaluate(async (id) => (await import("/js/fxEngine.js")).getLaneChain(id).on, ids.track)).toBe(true);
    // Advanced plays the Performance track's lane without it (only stem and
    // track chains saved in Advanced's own record play there).
    await chooseMode(page, "daw");
    await expect.poll(() => engineHosts(page)).toBe(0);
  });

  test("the chain, the instrument and Low latency come back after a reload; Hear myself does not", async ({ page }) => {
    await openPerformance(page);
    await openSection(page);
    const box = page.locator("#streamerFx");
    await box.locator(".fx-add").click();
    await page.locator('#streamerFx .fx-add-item[data-type="delay"]').click();
    await box.locator(".fx-add").click();
    await page.locator('#streamerFx .fx-add-item[data-type="drive"]').click();
    await expect(box.locator(".fx-card")).toHaveCount(2);
    const order = () => box.locator(".fx-card .fx-card-main").allTextContents();
    expect(await order()).toEqual(["Distortion", "Delay"]);
    // The strip runs left to right, as on a lane: Alt+Left moves a card earlier.
    await expect(box.locator(".fx-strip")).not.toHaveClass(/fx-strip-vertical/);
    await box.locator(".fx-card .fx-card-main").nth(1).focus();
    await page.keyboard.press("Alt+ArrowLeft");
    await expect.poll(order).toEqual(["Delay", "Distortion"]);
    await box.locator(".fx-inst").selectOption("bass");
    await box.locator(".sm-fx-low").click();
    await hear(page, true);
    const before = await perf(page, (p) => p.getInputFx("performance"));
    expect(before.chain.inst).toBe("bass");
    expect(before.low).toBe(true);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    await page.waitForFunction(() => !/\/\s*00:00\s*$/.test(document.querySelector("#t-time")?.textContent || "00:00 / 00:00"), null, { timeout: 20000 });
    await openPerformance(page, { skipStudio: true });
    expect(await perf(page, (p) => p.getInputFx("performance"))).toEqual(before);
    // The Effects tab, chosen before the reload, is the one that shows.
    await expect(page.locator("#streamerTab-effects")).toHaveAttribute("aria-selected", "true");
    await expect(page.locator("#streamerFx")).toBeVisible();
    await expect(box.locator(".fx-card .fx-card-main")).toHaveText(["Delay", "Distortion"]);
    await expect(box.locator(".fx-inst")).toHaveValue("bass");
    await expect(box.locator(".sm-fx-low")).toHaveAttribute("aria-checked", "true");
    await expect(hearing(page)).toHaveAttribute("aria-checked", "false");
    await expect(page.locator("#streamerFxState")).toHaveText(TRANSLATIONS.en["rec.liveFx.monitorOff"]);
    expect(await hosts(page)).toBe(0);
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
    await openPerformance(page);
    const warn = page.locator("#streamerSpeakersWarn");
    const phones = page.locator("#streamerHeadphones");
    await expect(warn).toBeHidden();
    // The headphones note shows while Hear myself is on.
    await expect(phones).toBeHidden();
    await hear(page, true);
    await expect(phones).toHaveText(TRANSLATIONS.en["rec.headphonesNote"]);
    await expect(warn).toBeVisible();
    await expect(warn).toContainText("Speakers (Realtek(R) Audio)");
    await hear(page, false);
    await expect(warn).toBeHidden();
    await page.evaluate(() => localStorage.setItem("e2e.output", "Speakers (Focusrite USB Audio)"));
    await hear(page, true);
    await page.waitForTimeout(300);
    await expect(warn).toBeHidden();
  });

  for (const size of [{ width: 1800, height: 1020 }, { width: 1280, height: 560 }]) {
    test(`the add menu is never clipped in Performance's panel (${size.width}x${size.height})`, async ({ page }) => {
      await page.setViewportSize(size);
      await openPerformance(page);
      // The Effects tab is in the centre column at every width (no drawer).
      await openSection(page);
      const add = page.locator("#streamerFx .fx-add");
      await add.scrollIntoViewIfNeeded();
      await add.click();
      await expect(page.locator("#streamerFx .fx-add-menu")).toBeVisible();
      await expect(add).toHaveAttribute("aria-expanded", "true");
      const r = await menuOnScreen(page);
      console.log(`[perf-fx] add menu at ${size.width}x${size.height}: ${r.n} items, ${r.bad.length ? r.bad.join("; ") : "all on screen and uncovered"}`);
      expect(r.ok, r.bad.join("; ")).toBe(true);
      expect(r.scrolls).toBe(false);
      // Keyboard: Down moves on, Escape closes and gives focus back.
      await expect(page.locator(".fx-add-item").first()).toBeFocused();
      await page.keyboard.press("ArrowDown");
      await expect(page.locator(".fx-add-item").nth(1)).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(page.locator(".fx-add-menu")).toHaveCount(0);
      await expect(add).toBeFocused();
      await expect(add).toHaveAttribute("aria-expanded", "false");
      // A press outside closes it; the Performance view stays as it was.
      await add.click();
      await expect(page.locator(".fx-add-menu")).toBeVisible();
      await page.locator("#streamerFx .sm-fx-dry").click();
      await expect(page.locator(".fx-add-menu")).toHaveCount(0);
      await expect(page.locator("#streamer")).toBeVisible();
      // A scroll of the panel that moves the button closes it.
      await add.click();
      await expect(page.locator(".fx-add-menu")).toBeVisible();
      const moved = await page.evaluate(() => {
        const btn = document.querySelector("#streamerFx .fx-add");
        const before = btn.getBoundingClientRect().top;
        for (let n = btn.parentElement; n; n = n.parentElement) {
          const oy = getComputedStyle(n).overflowY;
          if ((oy === "auto" || oy === "scroll") && n.scrollHeight > n.clientHeight + 2) {
            n.scrollTop += n.scrollTop > 0 ? -30 : 30;
            break;
          }
        }
        return btn.getBoundingClientRect().top - before;
      });
      console.log(`[perf-fx] add menu at ${size.width}x${size.height}: the panel scrolled the button by ${moved.toFixed(0)} px`);
      if (moved !== 0) await expect(page.locator(".fx-add-menu")).toHaveCount(0);
      else await page.keyboard.press("Escape");
    });
  }

  test("the Guide explains Performance's live effects controls", async ({ page }) => {
    await openPerformance(page);
    await setChain(page, { inst: "guitar", pedals: [pedal("dddd", "delay", ECHO)] });
    await openSection(page);
    await expect(page.locator("#streamerFx .fx-card")).toHaveCount(1);
    await page.evaluate(async () => (await import("/js/helpMode.js")).setHelpMode(true));
    await expect(page.locator("#helpModeBtn")).toHaveAttribute("aria-pressed", "true");
    const tip = page.locator("#helpTip");
    const en = (key) => TRANSLATIONS.en[key];
    const cases = [
      ["#streamerTab-timeline", "help.smTabTimeline"],
      ["#streamerTab-effects", "help.smTabEffects"],
      ["#streamerHear", "help.smMonitor"],
      ["#streamerMonitorLevel", "help.smMonitorLevel"],
      ["#streamerFxTitle", "help.smFx"],
      ["#streamerFx .fx-power", "help.liveFxPower"],
      ["#streamerFx .fx-inst", "help.smFxInstrument"],
      ["#streamerFx .fx-trim .eq-knob-dial", "help.liveFxTrim"],
      ["#streamerFx .fx-meter", "help.liveFxMeter"],
      ["#streamerFx .fx-clip", "help.liveFxClip"],
      ["#streamerFx .sm-fx-low", "help.liveFxLow"],
      ["#streamerFx .fx-add", "help.liveFxAdd"],
      ["#streamerFx .fx-preset", "help.liveFxPreset"],
      ["#streamerFx .fx-card-main", "help.fxCard"],
      ["#streamerFx .fx-card-power", "help.fxCardPower"],
    ];
    for (const [sel, key] of cases) {
      await page.locator(sel).first().hover();
      await expect(tip, sel).toContainText(en(key));
    }
    // Every new string is in all ten tables (the i18n check runs on the file too).
    for (const key of ["streamer.fx.dry", "streamer.fx.noInput", "streamer.fx.inputName", "streamer.lane.takeFx", "help.smMonitor", "help.smFx", "help.smFxInstrument", "help.smTakeFx", "help.smTabTimeline", "help.smTabEffects", "streamer.tabs.timeline", "streamer.tabs.effects", "streamer.tabs.aria"]) {
      for (const lang of ["en", "pl", "ja", "ko", "zh-Hans", "de", "fr", "es", "pt", "id"]) expect(TRANSLATIONS[lang][key], `${lang} ${key}`).toBeTruthy();
    }
  });
});
