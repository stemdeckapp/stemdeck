// The EQ engine in a real browser (Chromium), muted.
//
// 1. Golden vectors: the response of the real BiquadFilterNodes (what the
//    studio plays through) against eqMath.js and against
//    tests/fixtures/eq_golden.json, which pytest holds the Python export maths
//    and an ffmpeg render to (tests/test_fx_filters.py). One file, three
//    implementations. STEMDECK_EQ_GOLDEN_WRITE=1 rewrites the file from this
//    browser instead of checking it.
// 2. An impulse rendered offline through the effects slot measures what the
//    nodes do, not only what they report.
// 3. Bypass and a flat EQ are bit-exact with no EQ (offline, sample by sample).
// 4. In the studio: a +12 dB bell boosts a playing lane by what the maths
//    says, bypass gives back the input bit for bit, the EQ is saved with the
//    song and comes back after a reload, and the export URL carries it.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test, expect } from "@playwright/test";
import { JOB_ID, openStudio } from "./helpers.mjs";

test.use({ launchOptions: { args: ["--mute-audio", "--autoplay-policy=no-user-gesture-required"] } });

const HERE = path.dirname(fileURLToPath(import.meta.url));
const GOLDEN = path.join(HERE, "..", "fixtures", "eq_golden.json");

const band = (type, f, g = 0, q = Math.SQRT1_2, slope = 12) => ({ on: true, type, f, g, q, slope });
const eq = (bands, out = 0) => ({ id: "eq", v: 1, bypass: false, params: { out, bands } });

// Every band type, both cut directions and slopes, high and low frequencies,
// and a full cascade with output gain.
const CASES = {
  bell_1k_p12_q1: eq([band("bell", 1000, 12, 1)]),
  bell_100_m9_q4: eq([band("bell", 100, -9, 4)]),
  bell_8k_p6_q2: eq([band("bell", 8000, 6, 2)]),
  bell_15k_p10_q07: eq([band("bell", 15000, 10, 0.707)]),
  lowshelf_200_p6: eq([band("lowshelf", 200, 6)]),
  highshelf_6k_m8: eq([band("highshelf", 6000, -8)]),
  lowcut_80_12: eq([band("lowcut", 80, 0, 0.707, 12)]),
  lowcut_40_48_q12: eq([band("lowcut", 40, 0, 1.2, 48)]),
  highcut_12k_24: eq([band("highcut", 12000, 0, 0.707, 24)]),
  highcut_5k_36: eq([band("highcut", 5000, 0, 0.707, 36)]),
  notch_3k_q8: eq([band("notch", 3000, 0, 8)]),
  bandpass_1k_q07: eq([band("bandpass", 1000, 0, 0.7)]),
  tilt_1k_p6: eq([band("tilt", 1000, 6)]),
  cascade: eq([
    band("lowcut", 30, 0, 0.707, 24),
    band("lowshelf", 120, 3),
    band("bell", 400, -4, 1.5),
    band("bell", 2500, 3, 0.8),
    band("highshelf", 10000, 4),
    band("highcut", 18000, 0, 0.707, 12),
  ], -2),
};

const FREQS = (() => {
  const out = [];
  for (let i = 0; i <= 120; i++) out.push(+(20 * 1000 ** (i / 120)).toFixed(4));
  out.push(18000);
  return [...new Set(out)].sort((a, b) => a - b);
})();

const RATES = [44100, 48000];

/** Runs in the page: node response, maths, and an offline impulse render. */
async function measureInBrowser(page) {
  return page.evaluate(async ({ cases, freqs, rates }) => {
    const { normalizeEq, eqResponseDb } = await import("/js/eqMath.js");
    const { createFxSlot } = await import("/js/fxChain.js");
    const out = {};
    for (const rate of rates) {
      out[rate] = {};
      for (const [name, raw] of Object.entries(cases)) {
        const desc = normalizeEq(raw);
        const len = rate * 2;
        const ctx = new OfflineAudioContext(1, len, rate);
        const slot = createFxSlot(ctx);
        slot.setEq(desc, { immediate: true });
        const node = Array.from(slot.getNodeResponseDb(freqs));
        const math = Array.from(eqResponseDb(desc, freqs, rate));
        // Impulse through the slot, offline, then its DTFT at each frequency.
        const buf = ctx.createBuffer(1, len, rate);
        buf.getChannelData(0)[0] = 1;
        const src = ctx.createBufferSource();
        src.buffer = buf;
        src.connect(slot.input);
        slot.output.connect(ctx.destination);
        src.start(0);
        const rendered = (await ctx.startRendering()).getChannelData(0);
        const imp = freqs.map((f) => {
          const w = (2 * Math.PI * f) / rate;
          let re = 0, im = 0;
          for (let n = 0; n < len; n++) {
            const h = rendered[n];
            if (h === 0) continue;
            re += h * Math.cos(w * n);
            im -= h * Math.sin(w * n);
          }
          return 10 * Math.log10(re * re + im * im);
        });
        out[rate][name] = { node, math, imp, sections: slot.sectionCount() };
      }
    }
    return out;
  }, { cases: CASES, freqs: FREQS, rates: RATES });
}

const maxAbsDiff = (a, b, freqs, upTo = Infinity) => {
  let m = 0;
  for (let i = 0; i < a.length; i++) if (freqs[i] <= upTo) m = Math.max(m, Math.abs(a[i] - b[i]));
  return m;
};
const worstAt = (a, b, freqs) => {
  let m = 0, at = 0;
  for (let i = 0; i < a.length; i++) {
    const d = Math.abs(a[i] - b[i]);
    if (d > m) { m = d; at = freqs[i]; }
  }
  return `${at} Hz (${b[freqs.indexOf(at)].toFixed(2)} dB)`;
};

test("the live filters match the maths and the golden file", async ({ page }) => {
  test.setTimeout(120_000);
  await page.goto("/api/health");
  // A page on the app's origin, so /js/ modules import.
  await page.goto("/");
  const measured = await measureInBrowser(page);

  if (process.env.STEMDECK_EQ_GOLDEN_WRITE === "1") {
    const golden = { freqs: FREQS, rates: RATES, cases: CASES, chromium: {} };
    for (const rate of RATES) {
      golden.chromium[rate] = {};
      for (const name of Object.keys(CASES)) {
        golden.chromium[rate][name] = measured[rate][name].node.map((v) => +v.toFixed(5));
      }
    }
    fs.writeFileSync(GOLDEN, `${JSON.stringify(golden, null, 1)}\n`);
  }
  const golden = JSON.parse(fs.readFileSync(GOLDEN, "utf8"));
  expect(golden.freqs).toEqual(FREQS);

  const report = [];
  for (const rate of RATES) {
    for (const name of Object.keys(CASES)) {
      const m = measured[rate][name];
      const nodeVsMath = maxAbsDiff(m.node, m.math, FREQS);
      // Where the response is above -30 dB. Deeper in a cut's stopband the
      // rendered impulse departs from the ideal by up to about 2 dB (at
      // -48 dB): Chromium computes in double but stores each output sample,
      // which is also the filter's feedback state, as float32 (biquad.cc), and
      // that rounding is amplified near the poles. It is a deterministic error
      // on a single impulse; on music it is noise about 80 dB under the
      // signal (see 12-eq-mastering-sota.md).
      const pass = m.math.map((v) => v > -30);
      const impVsMath = maxAbsDiff(m.imp.filter((_, i) => pass[i]), m.math.filter((_, i) => pass[i]), FREQS.filter((_, i) => pass[i]));
      const nodeVsGolden = maxAbsDiff(m.node, golden.chromium[rate][name], FREQS);
      report.push(`${rate} ${name}: node-math ${nodeVsMath.toExponential(2)} dB, `
        + `impulse-math ${impVsMath.toExponential(2)} dB at ${worstAt(m.imp, m.math, FREQS)}, `
        + `node-golden ${nodeVsGolden.toExponential(2)} dB`);
      // Chromium reports the response in float32: a few millionths of a dB.
      expect(nodeVsMath, `${rate} ${name} node vs maths`).toBeLessThan(0.001);
      // The rendered impulse goes through float32 buffers.
      expect(impVsMath, `${rate} ${name} impulse vs maths (above -30 dB)`).toBeLessThan(0.3);
      expect(nodeVsGolden, `${rate} ${name} node vs golden`).toBeLessThan(0.001);
    }
  }
  console.log(report.join("\n"));
});

test("bypass and a flat EQ are bit-exact with no EQ", async ({ page }) => {
  await page.goto("/");
  const result = await page.evaluate(async () => {
    const { normalizeEq, flatEq } = await import("/js/eqMath.js");
    const { createFxSlot } = await import("/js/fxChain.js");
    const rate = 48000;
    const len = rate; // 1 s
    async function run(setup) {
      const ctx = new OfflineAudioContext(2, len, rate);
      const buf = ctx.createBuffer(1, len, rate);
      const d = buf.getChannelData(0);
      let seed = 12345;
      for (let i = 0; i < len; i++) {
        seed = (seed * 1103515245 + 12345) >>> 0;
        d[i] = ((seed / 2 ** 32) * 2 - 1) * 0.5;
      }
      const src = ctx.createBufferSource();
      src.buffer = buf;
      const merger = ctx.createChannelMerger(2);
      const slot = createFxSlot(ctx);
      src.connect(slot.input);
      slot.output.connect(merger, 0, 0); // left: through the slot
      src.connect(merger, 0, 1); // right: straight
      merger.connect(ctx.destination);
      setup(slot, ctx);
      src.start(0);
      const out = await ctx.startRendering();
      const a = out.getChannelData(0);
      const b = out.getChannelData(1);
      // Compare after the 20 ms fades (from 0.1 s on).
      let diff = 0;
      let firstDiff = -1;
      for (let i = Math.floor(rate * 0.1); i < len; i++) {
        if (a[i] !== b[i]) { diff++; if (firstDiff < 0) firstDiff = i; }
      }
      let energy = 0;
      for (let i = 0; i < len; i++) energy += (a[i] - b[i]) ** 2;
      return { diff, firstDiff, energy };
    }
    const boost = normalizeEq({ params: { out: 3, bands: [
      { type: "bell", f: 1000, g: 12, q: 1 }, { type: "lowcut", f: 80, slope: 24 },
    ] } });
    const none = await run(() => {});
    const flat = await run((slot) => slot.setEq(normalizeEq({ params: { bands: [
      { type: "bell", f: 1000, g: 0, q: 1 }, { type: "highshelf", f: 8000, g: 0 },
    ] } }), { immediate: true }));
    const active = await run((slot) => slot.setEq(boost, { immediate: true }));
    // Bypass switched on with the normal ramps (not immediate), at time 0.
    const bypassed = await run((slot) => {
      slot.setEq(boost, { immediate: true });
      slot.setEq({ ...boost, bypass: true });
    });
    const reset = await run((slot) => {
      slot.setEq(boost, { immediate: true });
      slot.setEq(flatEq());
    });
    return { none, flat, active, bypassed, reset };
  });
  expect(result.none.diff).toBe(0);
  expect(result.flat.diff).toBe(0);
  expect(result.active.diff).toBeGreaterThan(1000); // the EQ does something
  expect(result.bypassed.diff).toBe(0);
  expect(result.reset.diff).toBe(0);
});

test("in the studio: a bell boosts the lane, bypass is exact, the EQ is saved and exported", async ({ page }) => {
  test.setTimeout(90_000);
  await openStudio(page);
  // "other" is an 880 Hz sine in the fixture.
  await page.locator("#t-play").click();
  await expect.poll(() => page.evaluate(async () => {
    const { audioEngine } = await import("/js/state.js");
    return audioEngine?.isClockReady?.() === true;
  }), { timeout: 15000 }).toBe(true);

  const measured = await page.evaluate(async () => {
    const eqApi = await import("/js/eqEngine.js");
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const pre = eqApi.getAnalyser("other", "pre", { fftSize: 4096, smoothing: 0 });
    const post = eqApi.getAnalyser("other", "post", { fftSize: 4096, smoothing: 0 });
    if (!pre || !post) return { error: "no analyser" };
    const a = new Float32Array(4096);
    const b = new Float32Array(4096);
    const a2 = new Float32Array(4096);
    const rms = (x) => Math.sqrt(x.reduce((s, v) => s + v * v, 0) / x.length);
    // Read pre, post, pre again: if pre did not move, both are the same
    // render quanta and can be compared sample for sample.
    async function snapshot() {
      for (let tries = 0; tries < 200; tries++) {
        pre.getFloatTimeDomainData(a);
        post.getFloatTimeDomainData(b);
        pre.getFloatTimeDomainData(a2);
        if (a.every((v, i) => v === a2[i]) && rms(a) > 0) {
          return { pre: Float32Array.from(a), post: Float32Array.from(b) };
        }
        await sleep(3);
      }
      return null;
    }
    await sleep(300);
    const before = await snapshot();
    const set = eqApi.setLaneEq("other", { params: { bands: [{ type: "bell", f: 1000, g: 12, q: 1 }] } });
    await sleep(400);
    const boosted = await snapshot();
    const expectedDb = eqApi.getFrequencyResponse("other", [880])[0];
    eqApi.setBypass("other", true);
    await sleep(300);
    const bypassed = await snapshot();
    eqApi.setBypass("other", false);
    await sleep(300);
    // A/B preview: plays flat without saving, then back to the saved EQ.
    eqApi.previewLaneEq("other", { params: { bands: [] } });
    await sleep(300);
    const preview = await snapshot();
    const savedDuringPreview = eqApi.getLaneEq("other").params.bands.length;
    eqApi.previewLaneEq("other", null);
    await sleep(300);
    const restored = await snapshot();
    const fx = eqApi.exportFxParam(["other", "vocals"]);
    const stored = JSON.parse(localStorage.getItem("stemdeck:mix:e2e0deadbeef:daw") || "null");
    const db = (s) => 20 * Math.log10(rms(s.post) / rms(s.pre));
    return {
      set,
      flatDb: db(before),
      flatExact: before.pre.every((v, i) => v === before.post[i]),
      boostDb: db(boosted),
      expectedDb,
      bypassExact: bypassed.pre.every((v, i) => v === bypassed.post[i]),
      previewExact: preview.pre.every((v, i) => v === preview.post[i]),
      savedDuringPreview,
      restoredDb: db(restored),
      fx,
      sampleRate: eqApi.liveSampleRate(),
      storedLater: stored,
    };
  });
  expect(measured.error).toBeUndefined();
  console.log(`studio: flat ${measured.flatDb.toFixed(4)} dB, boost ${measured.boostDb.toFixed(3)} dB `
    + `(maths ${measured.expectedDb.toFixed(3)} dB at 880 Hz, ${measured.sampleRate} Hz context)`);
  expect(measured.flatExact).toBe(true);
  expect(Math.abs(measured.boostDb - measured.expectedDb)).toBeLessThan(0.1);
  expect(measured.boostDb).toBeGreaterThan(10);
  expect(measured.bypassExact).toBe(true);
  expect(measured.previewExact).toBe(true);
  expect(measured.savedDuringPreview).toBe(1);
  expect(Math.abs(measured.restoredDb - measured.expectedDb)).toBeLessThan(0.1);
  // Only the lane whose EQ changes the sound is in the export parameter.
  const wire = JSON.parse(Buffer.from(measured.fx, "base64url").toString("utf8"));
  expect(wire).toEqual({ v: 1, lanes: { other: [{ id: "eq", out: 0, b: [["bell", 1000, 12, 1, 12]] }] } });

  // Saved with the song's mix (debounced), and back after a reload.
  await page.waitForFunction(() => {
    const m = JSON.parse(localStorage.getItem("stemdeck:mix:e2e0deadbeef:daw") || "null");
    return m?.other?.fx?.[0]?.params?.bands?.[0]?.g === 12;
  }, null, { timeout: 5000 });
  await page.reload();
  await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
  await page.waitForFunction(
    () => !/\/\s*00:00\s*$/.test(document.querySelector("#t-time")?.textContent || "00:00 / 00:00"),
    null,
    { timeout: 20000 },
  );
  const after = await page.evaluate(async () => {
    const eqApi = await import("/js/eqEngine.js");
    // The engine got it too: read the response back from its nodes.
    for (let i = 0; i < 50; i++) {
      const node = eqApi.getNodeResponse("other", [880]);
      if (node && Math.abs(node[0]) > 1) {
        return { eq: eqApi.getLaneEq("other"), nodeDb: node[0], mathDb: eqApi.getFrequencyResponse("other", [880])[0] };
      }
      await new Promise((r) => setTimeout(r, 100));
    }
    return { eq: eqApi.getLaneEq("other"), nodeDb: null };
  });
  expect(after.eq.params.bands).toEqual([{ on: true, type: "bell", f: 1000, g: 12, q: 1, slope: 12 }]);
  expect(after.nodeDb).not.toBeNull();
  expect(Math.abs(after.nodeDb - after.mathDb)).toBeLessThan(0.001);

  // The mixdown URL the export menu builds carries it.
  const download = page.waitForEvent("download");
  await page.evaluate(async () => {
    const player = await import("/js/player.js");
    player.downloadCurrentMix("wav");
  });
  const url = new URL((await download).url());
  const sent = JSON.parse(Buffer.from(url.searchParams.get("fx"), "base64url").toString("utf8"));
  expect(sent.lanes.other[0].b).toEqual([["bell", 1000, 12, 1, 12]]);
});

test("a saved EQ from a newer build reads as flat and is kept", async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("stemdeck:mix:e2e0deadbeef", JSON.stringify({
      other: { volume: 1, muted: false, soloed: false, pitch: 0, fx: [{ id: "eq", v: 99, params: { bands: "x" } }] },
      vocals: { volume: 1, muted: false, soloed: false, pitch: 0, fx: "garbage" },
      bass: { volume: 1, muted: false, soloed: false, pitch: 0, fx: [{ id: "eq", v: 1, params: { out: 99, bands: [{ type: "bell", f: 1e9, g: -500, q: 0 }, null, { type: "nope" }] } }] },
    }));
  });
  await openStudio(page);
  const r = await page.evaluate(async () => {
    const eqApi = await import("/js/eqEngine.js");
    const { mixerState } = await import("/js/state.js");
    return {
      other: eqApi.getLaneEq("other"),
      otherStored: mixerState.other.fx,
      vocals: eqApi.getLaneEq("vocals"),
      bass: eqApi.getLaneEq("bass"),
    };
  });
  expect(r.other.params.bands).toEqual([]);
  expect(r.otherStored[0].v).toBe(99);
  expect(r.vocals.params.bands).toEqual([]);
  expect(r.bass.params.out).toBe(24);
  expect(r.bass.params.bands).toEqual([{ on: true, type: "bell", f: 20000, g: -30, q: 0.025, slope: 12 }]);
});
