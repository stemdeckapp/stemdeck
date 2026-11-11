// The effects chain's DSP, measured: every pedal and the chain host rendered
// through an OfflineAudioContext in a real browser (Chromium), with the same
// modules the studio plays and the export prints (static/js/fx/).
//
// What is asserted is measured on the rendered samples, never read back from
// the nodes:
//   - bypass and an empty chain are bit-identical to the input
//   - a delay's repeats land on n x time to within one sample, at 44.1 and
//     48 kHz, mono and ping-pong (they would be 128 frames late per repeat
//     without the measured loop overhead, dspBlocks.loopOverheadFrames)
//   - feedback decay per repeat, the stated output bound at 98 % feedback,
//     no NaN or Infinity
//   - a reorder gives a different result where the order matters, and the
//     crossfaded reorder steps no more than the signal itself does (a live
//     rewire does, which the test shows so it can fail)
//   - trails ring on bypass, a seek flushes them
//   - a synced delay sits on the beat

import { test, expect } from "@playwright/test";

// bypassCSP: the measuring helpers below go in as an inline script, which
// the app's own CSP (script-src 'self') rightly refuses on its pages.
test.use({ launchOptions: { args: ["--mute-audio"] }, bypassCSP: true });

const HELPERS = `
window.fxt = (() => {
  const mods = async () => ({
    model: await import("/js/fx/chainModel.js"),
    specs: await import("/js/fx/pedalSpecs.js"),
    host: await import("/js/fx/chainHost.js"),
    pedals: await import("/js/fx/pedals.js"),
    blocks: await import("/js/fx/dspBlocks.js"),
  });
  // A buffer source of a function of time (seconds), stereo.
  function source(ctx, seconds, f, fr) {
    const n = Math.round(seconds * ctx.sampleRate);
    const buf = ctx.createBuffer(2, n, ctx.sampleRate);
    for (let ch = 0; ch < 2; ch++) {
      const d = buf.getChannelData(ch);
      for (let i = 0; i < n; i++) d[i] = f(i / ctx.sampleRate, ch, i);
    }
    const src = ctx.createBufferSource();
    src.buffer = buf;
    return src;
  }
  // A 1 kHz burst, Hann windowed, 'dur' seconds from 0.
  const burst = (amp, dur) => (t) => (t < dur ? amp * Math.sin(2 * Math.PI * 1000 * t) * 0.5 * (1 - Math.cos(2 * Math.PI * t / dur)) : 0);
  // Lag of the best match of 'ref' inside 'x' around 'centre' (+-span), with
  // parabolic interpolation: sub-sample precision.
  function lagOf(x, ref, centre, span) {
    let best = -Infinity, bi = 0;
    const score = (lag) => { let s = 0; for (let i = 0; i < ref.length; i++) s += ref[i] * (x[lag + i] || 0); return s; };
    const scores = new Map();
    for (let lag = centre - span; lag <= centre + span; lag++) { const s = score(lag); scores.set(lag, s); if (s > best) { best = s; bi = lag; } }
    const a = scores.get(bi - 1) ?? best, b = best, c = scores.get(bi + 1) ?? best;
    const den = a - 2 * b + c;
    return { lag: den !== 0 ? bi + 0.5 * (a - c) / den : bi, score: best };
  }
  const peakAbs = (x, from = 0, to = x.length) => { let m = 0; for (let i = from; i < to; i++) m = Math.max(m, Math.abs(x[i])); return m; };
  const rms = (x, from = 0, to = x.length) => { let s = 0; for (let i = from; i < to; i++) s += x[i] * x[i]; return Math.sqrt(s / Math.max(1, to - from)); };
  const maxStep = (x, from, to) => { let m = 0; for (let i = Math.max(1, from); i < to; i++) m = Math.max(m, Math.abs(x[i] - x[i - 1])); return m; };
  const finite = (x) => { for (let i = 0; i < x.length; i++) if (!Number.isFinite(x[i])) return false; return true; };
  async function loopExtra(M, rate) {
    return { mono: await M.blocks.loopOverheadFrames(rate, 1), shaper4x: await M.blocks.shaperLatencyFrames(rate) };
  }
  // Phase delay in samples at f of the delay's two loop filters (one pass),
  // from real BiquadFilterNodes. The correlation peak of a 1 kHz burst
  // follows its carrier, so this is the shift each pass adds to where it is
  // found (the group delay, 0.6 samples here, is the shift of its envelope).
  function loopFilterDelay(rate, lowcut, highcut, f) {
    const ctx = new OfflineAudioContext(1, 128, rate);
    let pd = 0;
    for (const [type, fc] of [["highpass", lowcut], ["lowpass", highcut]]) {
      const bq = ctx.createBiquadFilter();
      bq.type = type; bq.frequency.value = fc; bq.Q.value = 20 * Math.log10(Math.SQRT1_2);
      const mag = new Float32Array(1), ph = new Float32Array(1);
      bq.getFrequencyResponse(Float32Array.from([f]), mag, ph);
      pd += -ph[0] / (2 * Math.PI * f) * rate;
    }
    return pd;
  }
  function delayPedal(M, inst, p, on = true) {
    return { iid: "dddd", type: "delay", on, preset: null, p: { ...M.specs.pedalDefaults("delay", inst), ...p } };
  }
  return { mods, source, burst, lagOf, peakAbs, rms, maxStep, finite, loopExtra, loopFilterDelay, delayPedal };
})();
`;

async function setup(page) {
  await page.goto("/api/health");
  await page.addScriptTag({ content: HELPERS });
}

test.describe("effects chain DSP, offline", () => {
  test.beforeEach(async ({ page }) => { await setup(page); });

  test("bypass, an off chain and an empty chain are bit-identical to the input", async ({ page }) => {
    const r = await page.evaluate(async () => {
      const M = await fxt.mods();
      const rate = 48000;
      const out = {};
      const noise = (t, ch, i) => Math.sin(i * 12.9898 + ch * 78.233) * 0.9;
      const cases = {
        pedalOff: (ctx, extra) => {
          const pedal = fxt.delayPedal(M, "guitar", { char: "digital", fb: 0, mix: 0.6 }, false);
          return M.pedals.createSeries(ctx, { pedals: [pedal] }, { inst: "guitar", bpm: null, loopExtra: extra });
        },
        mixZero: (ctx, extra) => {
          const pedal = fxt.delayPedal(M, "guitar", { char: "analog", fb: 0.5, mix: 0 });
          return M.pedals.createSeries(ctx, { pedals: [pedal] }, { inst: "guitar", bpm: null, loopExtra: extra });
        },
        chainOff: (ctx, extra) => {
          const host = M.host.createChainHost(ctx);
          const desc = M.model.normalizeChain({ on: false, trim: 6, pedals: [fxt.delayPedal(M, "guitar", { fb: 0.5, mix: 0.5 })] });
          host.apply({ desc, inst: "guitar", bpm: null, loopExtra: extra }, { songTime: 0, when: 0 }, { immediate: true });
          return host;
        },
        emptyChainOn: (ctx, extra) => {
          const host = M.host.createChainHost(ctx);
          host.apply({ desc: M.model.emptyChain(), inst: "guitar", bpm: null, loopExtra: extra }, { songTime: 0, when: 0 }, { immediate: true });
          return host;
        },
        voiceSendMixZero: (ctx, extra) => {
          const pedal = fxt.delayPedal(M, "voice", { duck: 12, mix: 0 });
          return M.pedals.createSeries(ctx, { pedals: [pedal] }, { inst: "voice", bpm: null, loopExtra: extra });
        },
      };
      const extra = await fxt.loopExtra(M, rate);
      for (const [name, make] of Object.entries(cases)) {
        const ctx = new OfflineAudioContext(2, rate, rate);
        const src = fxt.source(ctx, 1, noise);
        const node = make(ctx, extra);
        src.connect(node.input);
        node.output.connect(ctx.destination);
        src.start();
        const res = await ctx.startRendering();
        let diff = 0;
        for (let ch = 0; ch < 2; ch++) {
          const y = res.getChannelData(ch);
          const x = src.buffer.getChannelData(ch);
          for (let i = 0; i < x.length; i++) if (y[i] !== x[i]) diff++;
        }
        out[name] = diff;
      }
      return out;
    });
    for (const [name, diff] of Object.entries(r)) expect(diff, `${name}: samples that differ`).toBe(0);
  });

  for (const rate of [44100, 48000]) {
    for (const stereo of ["mono", "pingpong"]) {
      test(`delay repeats land on n x time within a sample (${rate} Hz, ${stereo})`, async ({ page }) => {
        const r = await page.evaluate(async ({ rate, stereo }) => {
          const M = await fxt.mods();
          const extra = await fxt.loopExtra(M, rate);
          const time = 0.2137;
          const ctx = new OfflineAudioContext(2, Math.round(rate * 1.4), rate);
          const src = fxt.source(ctx, 0.02, fxt.burst(0.5, 0.02));
          const pedal = fxt.delayPedal(M, "guitar", { char: "digital", stereo, time, fb: 0.5, lowcut: 20, highcut: 12000, mod: 0, mix: 1 });
          const s = M.pedals.createSeries(ctx, { pedals: [pedal] }, { inst: "guitar", bpm: null, loopExtra: extra });
          src.connect(s.input);
          s.output.connect(ctx.destination);
          src.start();
          const res = await ctx.startRendering();
          const L = res.getChannelData(0);
          const R = res.getChannelData(1);
          const ref = src.buffer.getChannelData(0);
          const n0 = time * rate;
          // Every repeat passes the loop's low cut and high cut once more,
          // and those filters delay a 1 kHz burst by their own group delay.
          const gd = fxt.loopFilterDelay(rate, 20, 12000, 1000);
          const repeats = [];
          for (let n = 1; n <= 5; n++) {
            // Ping-pong alternates: odd repeats left, even right.
            const ch = stereo === "pingpong" && n % 2 === 0 ? R : L;
            const { lag } = fxt.lagOf(ch, ref, Math.round(n * n0), 200);
            repeats.push({ n, err: lag - n * n0 - n * gd, peak: fxt.peakAbs(ch, Math.round(n * n0) - 50, Math.round(n * n0) + ref.length + 50) });
          }
          return { extra, gd, repeats, finite: fxt.finite(L) && fxt.finite(R) };
        }, { rate, stereo });
        expect(r.finite).toBe(true);
        // Within a quarter of a sample of n x time plus n passes of the
        // filters; without the measured overhead each repeat would be 128
        // frames later than the one before.
        for (const rep of r.repeats) {
          expect(Math.abs(rep.err), `repeat ${rep.n} is ${rep.err.toFixed(3)} samples off (loop overhead ${JSON.stringify(r.extra)}, filter delay ${r.gd.toFixed(3)} per pass)`).toBeLessThan(0.25);
        }
        // Feedback 0.5 per repeat (the 1 kHz burst is in every filter's pass band).
        for (let i = 1; i < r.repeats.length; i++) {
          const ratio = r.repeats[i].peak / r.repeats[i - 1].peak;
          expect(Math.abs(ratio - 0.5), `repeat ${i + 1}/${i} level ratio ${ratio.toFixed(4)}`).toBeLessThan(0.01);
        }
        console.log(`[fx-dsp] ${rate} Hz ${stereo}: loop overhead ${JSON.stringify(r.extra)} frames, filter delay ${r.gd.toFixed(3)}/pass; errors ${r.repeats.map((x) => x.err.toFixed(3)).join(", ")} samples`);
      });
    }
  }

  test("a synced delay sits on the song's beat", async ({ page }) => {
    const r = await page.evaluate(async () => {
      const M = await fxt.mods();
      const rate = 48000;
      const extra = await fxt.loopExtra(M, rate);
      const bpm = 97.3;
      const out = {};
      for (const sync of ["1/4", "1/8d", "1/8t"]) {
        const ctx = new OfflineAudioContext(2, Math.round(rate * 2.2), rate);
        const src = fxt.source(ctx, 0.02, fxt.burst(0.5, 0.02));
        const pedal = fxt.delayPedal(M, "guitar", { char: "digital", sync, time: 0.1, fb: 0.6, lowcut: 20, highcut: 12000, mod: 0, mix: 1 });
        const s = M.pedals.createSeries(ctx, { pedals: [pedal] }, { inst: "guitar", bpm, loopExtra: extra });
        src.connect(s.input);
        s.output.connect(ctx.destination);
        src.start();
        const res = await ctx.startRendering();
        const L = res.getChannelData(0);
        const step = M.model.syncSeconds(sync, bpm) * rate;
        const gd = fxt.loopFilterDelay(rate, 20, 12000, 1000);
        const errs = [];
        for (let n = 1; n <= 3; n++) errs.push(fxt.lagOf(L, src.buffer.getChannelData(0), Math.round(n * step), 200).lag - n * step - n * gd);
        out[sync] = errs;
      }
      return out;
    });
    for (const [sync, errs] of Object.entries(r)) {
      for (const e of errs) expect(Math.abs(e), `${sync}: ${errs.map((x) => x.toFixed(3)).join(", ")}`).toBeLessThan(0.25);
    }
  });

  test("98 % feedback on a full-scale input stays bounded, with no NaN or Infinity", async ({ page }) => {
    const r = await page.evaluate(async () => {
      const M = await fxt.mods();
      const rate = 48000;
      const extra = await fxt.loopExtra(M, rate);
      const out = {};
      for (const char of ["digital", "analog", "tape"]) {
        for (const stereo of ["mono", "pingpong"]) {
          const ctx = new OfflineAudioContext(2, rate * 6, rate);
          const noise = (t, ch, i) => (t < 3 ? (Math.sin(i * 12.9898 + ch) * 43758.5453 % 1) : 0);
          const src = fxt.source(ctx, 6, noise);
          const pedal = fxt.delayPedal(M, "guitar", { char, stereo, time: 0.05, fb: 0.98, lowcut: 20, highcut: 12000, mod: 5, rate: 2.2, mix: 0.5 });
          const s = M.pedals.createSeries(ctx, { pedals: [pedal] }, { inst: "guitar", bpm: null, loopExtra: extra, songTime: 0, when: 0 });
          src.connect(s.input);
          s.output.connect(ctx.destination);
          src.start();
          const res = await ctx.startRendering();
          const L = res.getChannelData(0), R = res.getChannelData(1);
          out[`${char}/${stereo}`] = { peak: Math.max(fxt.peakAbs(L), fxt.peakAbs(R)), finite: fxt.finite(L) && fxt.finite(R) };
        }
      }
      return out;
    });
    // The bound: dry x + wet x the loop's ceiling (1 for the digital knee,
    // tanh(2) = 0.964 for analog and tape), at mix 0.5 on the pedal law:
    // 0.7071 x 1 + 0.7071 x 1 = 1.4142.
    for (const [k, v] of Object.entries(r)) {
      expect(v.finite, `${k} finite`).toBe(true);
      expect(v.peak, `${k} peak ${v.peak.toFixed(4)}`).toBeLessThanOrEqual(1.4143);
    }
    console.log(`[fx-dsp] 98 % feedback peaks: ${Object.entries(r).map(([k, v]) => `${k} ${v.peak.toFixed(3)}`).join("; ")}`);
  });

  test("trails ring on bypass; a seek flushes them", async ({ page }) => {
    const r = await page.evaluate(async () => {
      const M = await fxt.mods();
      const rate = 48000;
      const extra = await fxt.loopExtra(M, rate);
      const desc = (on) => M.model.normalizeChain({
        pedals: [fxt.delayPedal(M, "guitar", { char: "digital", time: 0.3, fb: 0.6, lowcut: 20, highcut: 12000, mod: 0, mix: 0.5 }, on)],
      });
      async function run(event) {
        const ctx = new OfflineAudioContext(2, rate * 2, rate);
        const src = fxt.source(ctx, 0.05, fxt.burst(0.5, 0.05));
        const host = M.host.createChainHost(ctx);
        const spec = (on) => ({ desc: desc(on), inst: "guitar", bpm: null, loopExtra: extra });
        host.apply(spec(true), { songTime: 0, when: 0 }, { immediate: true });
        src.connect(host.input);
        host.output.connect(ctx.destination);
        src.start();
        M.host.laterIn(ctx, 0.15, () => {
          if (event === "bypass") host.apply(spec(false), { songTime: 0.15, when: 0.15 });
          if (event === "seek") host.flush({ songTime: 10, when: 0.15 });
        });
        const res = await ctx.startRendering();
        const L = res.getChannelData(0);
        return {
          echo1: fxt.rms(L, Math.round(0.3 * rate), Math.round(0.35 * rate)),
          echo2: fxt.rms(L, Math.round(0.6 * rate), Math.round(0.65 * rate)),
          after: fxt.rms(L, Math.round(0.2 * rate), Math.round(2 * rate)),
        };
      }
      return { bypass: await run("bypass"), seek: await run("seek"), none: await run("none") };
    });
    const db = (x) => 20 * Math.log10(Math.max(x, 1e-12));
    // Bypass at 0.15 s, after the input: the repeats at 0.3 and 0.6 s ring as if nothing happened.
    expect(Math.abs(db(r.bypass.echo1) - db(r.none.echo1))).toBeLessThan(0.1);
    expect(Math.abs(db(r.bypass.echo2) - db(r.none.echo2))).toBeLessThan(0.1);
    // A seek at 0.15 s: nothing of the old place after it.
    expect(db(r.seek.after), `after a seek ${db(r.seek.after).toFixed(1)} dB`).toBeLessThan(-120);
    expect(db(r.none.echo1)).toBeGreaterThan(-30);
  });

  test("a reorder is crossfaded: no step beyond the signal's own, and a live rewire would step", async ({ page }) => {
    const r = await page.evaluate(async () => {
      const M = await fxt.mods();
      const rate = 48000;
      const extra = await fxt.loopExtra(M, rate);
      const a = { ...fxt.delayPedal(M, "guitar", { char: "analog", time: 0.11, fb: 0.7, lowcut: 300, highcut: 3000, mod: 0, mix: 0.6 }), iid: "aaaa" };
      const b = { ...fxt.delayPedal(M, "guitar", { char: "tape", time: 0.23, fb: 0.5, lowcut: 60, highcut: 8000, mod: 0, mix: 0.4 }), iid: "bbbb" };
      const ab = M.model.normalizeChain({ pedals: [a, b] });
      const ba = M.model.movePedal(ab, "bbbb", 0);
      const sine = (t) => 0.8 * Math.sin(2 * Math.PI * 220 * t);
      async function run({ reorder, naive, order }) {
        const ctx = new OfflineAudioContext(2, rate * 2, rate);
        const src = fxt.source(ctx, 2, sine);
        const host = M.host.createChainHost(ctx);
        const spec = (d) => ({ desc: d, inst: "guitar", bpm: null, loopExtra: extra });
        if (naive) {
          // What a live rewire does: the same two series, swapped with
          // disconnect/connect at one instant.
          const s1 = M.pedals.createSeries(ctx, ab, { inst: "guitar", bpm: null, loopExtra: extra });
          const s2 = M.pedals.createSeries(ctx, ba, { inst: "guitar", bpm: null, loopExtra: extra });
          src.connect(s1.input); s1.output.connect(ctx.destination);
          M.host.laterIn(ctx, 1.0, () => { src.disconnect(); s1.output.disconnect(); src.connect(s2.input); s2.output.connect(ctx.destination); });
        } else {
          host.apply(spec(order || ab), { songTime: 0, when: 0 }, { immediate: true });
          src.connect(host.input);
          host.output.connect(ctx.destination);
          if (reorder) M.host.laterIn(ctx, 1.0, () => host.apply(spec(ba), { songTime: 1, when: 1 }));
        }
        src.start();
        const res = await ctx.startRendering();
        const L = res.getChannelData(0);
        // Across: the 20 ms crossfade and a margin either side, before the
        // new order's first repeat (0.11 s later) changes the level.
        return {
          steady: fxt.maxStep(L, Math.round(0.6 * rate), Math.round(0.99 * rate)),
          across: fxt.maxStep(L, Math.round(0.99 * rate), Math.round(1.06 * rate)),
          tail: Array.from(L.subarray(Math.round(1.7 * rate), Math.round(1.9 * rate))),
          finite: fxt.finite(L),
        };
      }
      const crossfaded = await run({ reorder: true });
      const naive = await run({ naive: true });
      const abOnly = await run({ order: ab });
      const baOnly = await run({ order: ba });
      let num = 0, den = 0;
      for (let i = 0; i < abOnly.tail.length; i++) { const d = abOnly.tail[i] - baOnly.tail[i]; num += d * d; den += abOnly.tail[i] ** 2; }
      return {
        crossfaded: { steady: crossfaded.steady, across: crossfaded.across, finite: crossfaded.finite },
        naive: { steady: naive.steady, across: naive.across },
        orderDiffDb: 10 * Math.log10(num / den),
      };
    });
    console.log(`[fx-dsp] reorder steps: steady ${r.crossfaded.steady.toFixed(4)}, across crossfade ${r.crossfaded.across.toFixed(4)}, naive rewire ${r.naive.across.toFixed(4)}; A-B vs B-A difference ${r.orderDiffDb.toFixed(1)} dB`);
    expect(r.crossfaded.finite).toBe(true);
    // The steepest step of a 220 Hz sine at 0.8 and its echoes is the
    // signal's own; the crossfade adds nothing beyond a 10 % margin.
    expect(r.crossfaded.across).toBeLessThanOrEqual(r.crossfaded.steady * 1.1);
    // The test can fail: a rewire at one instant steps well beyond it.
    expect(r.naive.across).toBeGreaterThan(r.naive.steady * 1.5);
    // Saturating repeats: the order is heard.
    expect(r.orderDiffDb).toBeGreaterThan(-40);
  });

  test("a third change while two chains ring fades the oldest out, and at most three chains live", async ({ page }) => {
    const r = await page.evaluate(async () => {
      const M = await fxt.mods();
      const rate = 48000;
      const extra = await fxt.loopExtra(M, rate);
      const ctx = new OfflineAudioContext(2, rate * 2, rate);
      const src = fxt.source(ctx, 2, (t) => 0.5 * Math.sin(2 * Math.PI * 330 * t));
      const host = M.host.createChainHost(ctx);
      const spec = (time) => ({ desc: M.model.normalizeChain({ pedals: [fxt.delayPedal(M, "guitar", { char: "digital", time, fb: 0.9, mix: 0.5 })] }), inst: "guitar", bpm: null, loopExtra: extra });
      host.apply(spec(0.1), { songTime: 0, when: 0 }, { immediate: true });
      src.connect(host.input);
      host.output.connect(ctx.destination);
      src.start();
      const counts = [];
      for (const [i, t] of [0.5, 0.6, 0.7, 0.8].entries()) {
        M.host.laterIn(ctx, t, () => { host.apply(spec(0.2 + i * 0.05), { songTime: t, when: t }); counts.push(host.chainCount()); });
      }
      const res = await ctx.startRendering();
      const L = res.getChannelData(0);
      return { counts, steady: fxt.maxStep(L, rate * 0.2, rate * 0.49), across: fxt.maxStep(L, rate * 0.49, rate * 1.0), finite: fxt.finite(L) };
    });
    expect(r.finite).toBe(true);
    expect(Math.max(...r.counts), `chains alive after each change: ${r.counts}`).toBeLessThanOrEqual(3);
    // Four changes 100 ms apart, each crossfaded, the oldest faded out: the
    // steepest step stays within twice the signal's own (several echoes of a
    // sine at different delays sum to a higher, still smooth, level).
    expect(r.across).toBeLessThanOrEqual(r.steady * 2);
  });

  test("the LFO phase follows song time, so a chain built mid-song matches one built at 0", async ({ page }) => {
    const r = await page.evaluate(async () => {
      const M = await fxt.mods();
      const rate = 48000;
      const extra = await fxt.loopExtra(M, rate);
      const pedal = fxt.delayPedal(M, "guitar", { char: "analog", time: 0.2, fb: 0, mod: 4, rate: 1.7, mix: 1, lowcut: 20, highcut: 12000 });
      const sine = (t) => 0.3 * Math.sin(2 * Math.PI * 500 * t);
      // A: from song time 0. B: a context that starts at song time 3.21 s.
      async function run(start) {
        const ctx = new OfflineAudioContext(1, rate * 2, rate);
        const src = fxt.source(ctx, 6, sine);
        const s = M.pedals.createSeries(ctx, { pedals: [pedal] }, { inst: "guitar", bpm: null, loopExtra: extra, songTime: start, when: 0 });
        src.connect(s.input);
        s.output.connect(ctx.destination);
        src.start(0, start);
        return (await ctx.startRendering()).getChannelData(0);
      }
      const a = await run(0);
      const b = await run(1.0);
      // Song time 1.5 s: sample 1.5 s into A and 0.5 s into B.
      let num = 0, den = 0;
      for (let i = 0; i < rate * 0.4; i++) { const x = a[rate * 1.5 + i], y = b[rate * 0.5 + i]; num += (x - y) ** 2; den += x * x; }
      return 10 * Math.log10(num / den);
    });
    expect(r, `difference ${r.toFixed(1)} dB`).toBeLessThan(-60);
  });
});

// ─── Chorus ───

test.describe("chorus DSP, offline", () => {
  test.beforeEach(async ({ page }) => { await setup(page); });

  const chorus = (M, inst, p, on = true) => ({ iid: "cccc", type: "chorus", on, preset: null, p: { ...M.specs.pedalDefaults("chorus", inst), ...p } });

  test("off and mix 0 are bit-identical; the delay is Time to the sample, the right side Time plus offset", async ({ page }) => {
    const r = await page.evaluate(async () => {
      const M = await fxt.mods();
      const rate = 48000;
      const extra = await fxt.loopExtra(M, rate);
      const c = (inst, p, on = true) => ({ iid: "cccc", type: "chorus", on, preset: null, p: { ...M.specs.pedalDefaults("chorus", inst), ...p } });
      async function run(pedal, src) {
        const ctx = new OfflineAudioContext(2, rate, rate);
        const s = fxt.source(ctx, 1, src);
        const ser = M.pedals.createSeries(ctx, { pedals: [pedal] }, { inst: "guitar", bpm: null, loopExtra: extra, songTime: 0, when: 0 });
        s.connect(ser.input); ser.output.connect(ctx.destination); s.start();
        const res = await ctx.startRendering();
        return { L: res.getChannelData(0), R: res.getChannelData(1), x: s.buffer.getChannelData(0) };
      }
      const noise = (t, ch, i) => Math.sin(i * 12.9898 + ch * 78.233) * 0.8;
      const diff = (o) => { let n = 0; for (let i = 0; i < o.x.length; i++) if (o.L[i] !== o.x[i]) n++; return n; };
      const off = diff(await run(c("guitar", { mix: 0.7, fb: 0.5, xover: 160 }, false), noise));
      const mix0 = diff(await run(c("guitar", { mix: 0, xover: 30 }), noise));
      const imp = (t, ch, i) => (i === 100 ? 0.8 : 0);
      const o = await run(c("guitar", { mix: 1, depth: 0, dtime: 10, offset: 5, highcut: 12000, wetlow: 0, xover: 30, stereo: "mono" }), imp);
      const peakAt = (d) => { let m = 0, k = 0; for (let i = 0; i < d.length; i++) if (Math.abs(d[i]) > m) { m = Math.abs(d[i]); k = i; } return k; };
      return { off, mix0, left: peakAt(o.L) - 100, right: peakAt(o.R) - 100, wantL: 10e-3 * rate, wantR: 15e-3 * rate };
    });
    expect(r.off, "off: samples that differ").toBe(0);
    expect(r.mix0, "mix 0: samples that differ").toBe(0);
    // The 12 kHz high cut smears an impulse by a sample at most.
    expect(Math.abs(r.left - r.wantL)).toBeLessThanOrEqual(1);
    expect(Math.abs(r.right - r.wantR)).toBeLessThanOrEqual(1);
  });

  test("the sweep spans Time +/- depth x 3 ms, the two sides opposite in wide", async ({ page }) => {
    const r = await page.evaluate(async () => {
      const M = await fxt.mods();
      const rate = 48000;
      const extra = await fxt.loopExtra(M, rate);
      const pedal = { iid: "cccc", type: "chorus", on: true, preset: null, p: { ...M.specs.pedalDefaults("chorus", "guitar"), mix: 1, depth: 0.5, dtime: 8, offset: 0, rate: 1, shape: "sine", stereo: "wide", highcut: 12000, wetlow: 0, xover: 30, fb: 0 } };
      const ctx = new OfflineAudioContext(2, rate * 2, rate);
      const noise = (t, ch, i) => { const x = Math.sin(i * 12.9898) * 43758.5453; return (x - Math.floor(x)) - 0.5; };
      const s = fxt.source(ctx, 2, noise);
      const ser = M.pedals.createSeries(ctx, { pedals: [pedal] }, { inst: "guitar", bpm: null, loopExtra: extra, songTime: 0, when: 0 });
      s.connect(ser.input); ser.output.connect(ctx.destination); s.start();
      const res = await ctx.startRendering();
      const x = s.buffer.getChannelData(0);
      const est = (out, t) => {
        // The lag of a 5 ms window of input inside the output near t.
        const at = Math.round(t * rate);
        const ref = x.subarray(at, at + 240);
        const shifted = new Float32Array(out.length - at);
        shifted.set(out.subarray(at));
        return fxt.lagOf(shifted, ref, Math.round(8e-3 * rate), Math.round(2.5e-3 * rate)).lag / rate * 1000;
      };
      const ts = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75];
      return { L: ts.map((t) => est(res.getChannelData(0), t)), R: ts.map((t) => est(res.getChannelData(1), t)) };
    });
    // Sine at 1 Hz from song time 0: +1.5 ms at 0.25 s and 1.25 s, -1.5 ms at
    // 0.75 s and 1.75 s; the right side the opposite.
    // Predicted at the output time of the window's centre (input time, plus
    // half the 5 ms window, plus the 8 ms delay).
    const want = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75].map((t) => 1.5 * Math.sin(2 * Math.PI * (t + 0.0105)));
    r.L.forEach((v, i) => expect(Math.abs(v - (8 + want[i])), `left at ${i}: ${v.toFixed(3)} ms`).toBeLessThan(0.03));
    r.R.forEach((v, i) => expect(Math.abs(v - (8 - want[i])), `right at ${i}: ${v.toFixed(3)} ms`).toBeLessThan(0.03));
  });

  test("the crossover keeps the lows dry and sends the highs through the effect", async ({ page }) => {
    const r = await page.evaluate(async () => {
      const M = await fxt.mods();
      const rate = 48000;
      const extra = await fxt.loopExtra(M, rate);
      const pedal = { iid: "cccc", type: "chorus", on: true, preset: null, p: { ...M.specs.pedalDefaults("chorus", "bass"), mix: 1, depth: 0, dtime: 10, highcut: 12000, wetlow: 0, xover: 160 } };
      const run = async (hz, mix) => {
        const ctx = new OfflineAudioContext(1, rate, rate);
        const s = fxt.source(ctx, 1, (t) => 0.5 * Math.sin(2 * Math.PI * hz * t));
        const ser = M.pedals.createSeries(ctx, { pedals: [{ ...pedal, p: { ...pedal.p, mix } }] }, { inst: "bass", bpm: null, loopExtra: extra, songTime: 0, when: 0 });
        s.connect(ser.input); ser.output.connect(ctx.destination); s.start();
        return { y: (await ctx.startRendering()).getChannelData(0), x: s.buffer.getChannelData(0) };
      };
      const out = {};
      for (const hz of [40, 2050]) {
        // All wet against all dry: in the crossover's low band they must be
        // the same signal, above it the wet one is the 10 ms copy.
        const wet = await run(hz, 1);
        const dry = await run(hz, 0);
        const a = Math.round(0.5 * rate), z = Math.round(0.9 * rate);
        let ey = 0, ex = 0, ed = 0;
        for (let i = a; i < z; i++) { ey += wet.y[i] ** 2; ex += wet.x[i] ** 2; ed += (wet.y[i] - dry.y[i]) ** 2; }
        out[hz] = { levelDb: 10 * Math.log10(ey / ex), wetVsDryDb: 10 * Math.log10(ed / ey) };
      }
      return out;
    });
    // Full level at both; at 40 Hz the effect changes nothing (below -30 dB),
    // at 2050 Hz (20.5 periods in 10 ms) the wet one is the delayed copy.
    expect(Math.abs(r[40].levelDb)).toBeLessThan(0.5);
    expect(r[40].wetVsDryDb).toBeLessThan(-30);
    expect(Math.abs(r[2050].levelDb)).toBeLessThan(0.5);
    expect(r[2050].wetVsDryDb).toBeGreaterThan(-3);
  });

  test("flanger feedback: the comb's repeats are exactly Time apart and fall by the feedback", async ({ page }) => {
    const r = await page.evaluate(async () => {
      const M = await fxt.mods();
      const rate = 48000;
      const extra = await fxt.loopExtra(M, rate);
      const pedal = { iid: "cccc", type: "chorus", on: true, preset: null, p: { ...M.specs.pedalDefaults("chorus", "bass"), mix: 1, depth: 0, dtime: 6, fb: 0.5, highcut: 12000, wetlow: 0, xover: 30 } };
      const ctx = new OfflineAudioContext(1, rate / 2, rate);
      const s = fxt.source(ctx, 0.5, (t, ch, i) => (i === 0 ? 0.9 : 0));
      const ser = M.pedals.createSeries(ctx, { pedals: [pedal] }, { inst: "bass", bpm: null, loopExtra: extra, songTime: 0, when: 0 });
      s.connect(ser.input); ser.output.connect(ctx.destination); s.start();
      const y = (await ctx.startRendering()).getChannelData(0);
      const d = 6e-3 * rate;
      const peaks = [1, 2, 3, 4].map((k) => { let m = 0, at = 0; for (let i = Math.round(k * d) - 40; i < Math.round(k * d) + 40; i++) if (Math.abs(y[i]) > m) { m = Math.abs(y[i]); at = i; } return { at, m }; });
      return { d, peaks, finite: fxt.finite(y) };
    });
    expect(r.finite).toBe(true);
    r.peaks.forEach((p, k) => expect(Math.abs(p.at - (k + 1) * r.d), `repeat ${k + 1} at ${p.at}`).toBeLessThanOrEqual(1));
    for (let k = 1; k < r.peaks.length; k++) expect(Math.abs(r.peaks[k].m / r.peaks[k - 1].m - 0.5)).toBeLessThan(0.05);
  });
});

// ─── The master's A/B path (masterFx.js) ───

test("the master's original path keeps its last 12 ms when the input stops", async ({ page }) => {
  await setup(page);
  const r = await page.evaluate(async () => {
    const { createMasterEffect } = await import("/js/masterFx.js");
    const rate = 48000;
    const ctx = new OfflineAudioContext(2, rate * 2, rate);
    const buf = ctx.createBuffer(2, rate, rate);
    for (let c = 0; c < 2; c++) { const d = buf.getChannelData(c); for (let i = 0; i < d.length; i++) d[i] = 0.5 * Math.sin(2 * Math.PI * 440 * i / rate); }
    const src = ctx.createBufferSource(); src.buffer = buf;
    const fx = createMasterEffect(ctx);
    fx.set({ listen: "original", match: false }, { immediate: true });
    src.connect(fx.input); fx.output.connect(ctx.destination); src.start();
    const res = await ctx.startRendering();
    const L = res.getChannelData(0);
    let s = 0;
    for (let i = rate + 24; i < rate + Math.round(fx.latencySec * rate) - 24; i++) s += L[i] * L[i];
    return { tailRms: Math.sqrt(s / (fx.latencySec * rate - 48)), latencyMs: fx.latencySec * 1000 };
  });
  // Before the fix the default channel mode emptied the line: 0 here.
  expect(r.latencyMs).toBeGreaterThan(5);
  expect(r.tailRms).toBeGreaterThan(0.3);
});

// ─── Phaser ───

test.describe("phaser DSP, offline", () => {
  test.beforeEach(async ({ page }) => { await setup(page); });

  /** Steady level (dB against the input) of a tone through a phaser, or its level in a short window at `at` s. */
  const measure = (page, cases) => page.evaluate(async (cases) => {
    const M = await fxt.mods();
    const rate = 48000;
    const out = [];
    for (const c of cases) {
      const pedal = { iid: "pppp", type: "phaser", on: c.on ?? true, preset: null, p: { ...M.specs.pedalDefaults("phaser", "guitar"), xover: 30, mix: 0.5, ...c.p } };
      const ctx = new OfflineAudioContext(1, rate * (c.seconds || 1), rate);
      const s = fxt.source(ctx, c.seconds || 1, c.noise ? (t, ch, i) => Math.sin(i * 12.9898) * 0.7 : (t) => 0.5 * Math.sin(2 * Math.PI * c.hz * t));
      const ser = M.pedals.createSeries(ctx, { pedals: [pedal] }, { inst: "guitar", bpm: c.bpm ?? null, loopExtra: { mono: 128 }, songTime: 0, when: 0 });
      s.connect(ser.input); ser.output.connect(ctx.destination); s.start();
      const y = (await ctx.startRendering()).getChannelData(0);
      const x = s.buffer.getChannelData(0);
      if (c.noise) { let d = 0; for (let i = 0; i < x.length; i++) if (x[i] !== y[i]) d++; out.push(d); continue; }
      const [a, z] = c.at != null ? [Math.round((c.at - 0.002) * rate), Math.round((c.at + 0.002) * rate)] : [Math.round(0.5 * rate), Math.round(0.9 * rate)];
      let ey = 0, ex = 0;
      for (let i = a; i < z; i++) { ey += y[i] * y[i]; ex += x[i] * x[i]; }
      out.push(10 * Math.log10(ey / ex));
    }
    return out;
  }, cases);

  test("notches where N first-order stages reach -180 degrees (the bilinear law of the digital allpass)", async ({ page }) => {
    const fc = 1000;
    const fs = 48000;
    // Analog: f = fc tan((2k+1) pi / 2N). The biquad is the bilinear,
    // prewarped allpass, so: tan(pi f / fs) = tan(pi fc / fs) tan((2k+1) pi / 2N).
    const notch = (n, k) => (fs / Math.PI) * Math.atan(Math.tan((Math.PI * fc) / fs) * Math.tan(((2 * k + 1) * Math.PI) / (2 * n)));
    const still = { depth: 0, centre: fc };
    const cases = [
      { hz: notch(2, 0), p: { ...still, stages: "2" } },
      { hz: notch(4, 0), p: { ...still, stages: "4" } },
      { hz: notch(4, 1), p: { ...still, stages: "4" } },
      { hz: notch(12, 0), p: { ...still, stages: "12" } },
      { hz: notch(12, 2), p: { ...still, stages: "12" } },
      // In phase between notches: dry and wet add up (+3 dB on the pedal law).
      { hz: fc, p: { ...still, stages: "4" } },
    ];
    const db = await measure(page, cases);
    console.log(`[fx-dsp] phaser notches: ${db.map((v) => v.toFixed(1)).join(", ")} dB`);
    db.slice(0, 5).forEach((v, i) => expect(v, `notch ${i}: ${v.toFixed(1)} dB at ${cases[i].hz.toFixed(1)} Hz`).toBeLessThan(-40));
    expect(Math.abs(db[5] - 3.01)).toBeLessThan(0.2);
  });

  test("the sweep is exponential and in phase with song time; a bar-synced sweep follows the tempo", async ({ page }) => {
    // Depth 2 octaves around 1000 Hz, sine at 1 Hz: the corner is 2000 Hz at
    // 0.25 s and 500 Hz at 0.75 s, so 828.4 Hz (0.414 x 2000) is notched at
    // 0.25 s only. Synced to 1 bar at 120 BPM (0.5 Hz) the same happens at 0.5 s.
    const swept = { stages: "4", depth: 2, centre: 1000, rate: 1, shape: "sine" };
    const f = 2000 * Math.tan(Math.PI / 8);
    const db = await measure(page, [
      { hz: f, p: swept, at: 0.25 },
      { hz: f, p: swept, at: 0.75 },
      { hz: f, p: { ...swept, sync: "bar", rate: 3 }, bpm: 120, at: 0.5, seconds: 2 },
      { hz: f, p: { ...swept, sync: "bar", rate: 3 }, bpm: 120, at: 1.5, seconds: 2 },
    ]);
    console.log(`[fx-dsp] phaser sweep: ${db.map((v) => v.toFixed(1)).join(", ")} dB`);
    expect(db[0]).toBeLessThan(-25);
    expect(db[1]).toBeGreaterThan(-6);
    expect(db[2]).toBeLessThan(-25);
    expect(db[3]).toBeGreaterThan(-6);
  });

  test("off and mix 0 are bit-identical", async ({ page }) => {
    const d = await measure(page, [
      { noise: true, on: false, p: { stages: "12", depth: 3, xover: 150 } },
      { noise: true, p: { stages: "4", depth: 3, mix: 0 } },
    ]);
    expect(d).toEqual([0, 0]);
  });
});

// ─── Distortion ───

test.describe("distortion DSP, offline", () => {
  test.beforeEach(async ({ page }) => { await setup(page); });

  // Render a tone or noise through one drive pedal; Goertzel levels in dBc.
  const run = (page, cases) => page.evaluate(async (cases) => {
    const M = await fxt.mods();
    const rate = 48000;
    const extra = await fxt.loopExtra(M, rate);
    const goertzel = (x, f, a, n) => {
      const w = (2 * Math.PI * f) / rate, c = 2 * Math.cos(w);
      let s1 = 0, s2 = 0;
      for (let i = 0; i < n; i++) { const h = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (n - 1)); const s0 = x[a + i] * h + c * s1 - s2; s2 = s1; s1 = s0; }
      return Math.sqrt(Math.max(1e-30, s1 * s1 + s2 * s2 - c * s1 * s2));
    };
    const out = [];
    for (const c of cases) {
      const pedal = { iid: "dddd", type: "drive", on: c.on ?? true, preset: null, p: { ...M.specs.pedalDefaults("drive", c.inst || "guitar"), cab: "off", lowcut: 20, midg: 0, highcut: 12000, blend: 1, level: 0, xover: 30, ...c.p } };
      const ctx = new OfflineAudioContext(1, rate * 2, rate);
      const sig = c.noise ? (t, ch, i) => { const v = Math.sin(i * 12.9898) * 43758.5453; return 2 * (v - Math.floor(v)) - 1; } : (t) => c.amp * Math.sin(2 * Math.PI * c.hz * t);
      const s = fxt.source(ctx, 2, sig);
      const ser = M.pedals.createSeries(ctx, { pedals: [pedal] }, { inst: c.inst || "guitar", bpm: null, loopExtra: extra, lowLatency: !!c.low, songTime: 0, when: 0 });
      s.connect(ser.input); ser.output.connect(ctx.destination); s.start();
      const y = (await ctx.startRendering()).getChannelData(0);
      const x = s.buffer.getChannelData(0);
      if (c.kind === "shifted") {
        const lat = c.low ? 0 : extra.shaper4x;
        let d = 0; for (let i = lat; i < x.length; i++) if (y[i] !== x[i - lat]) d++;
        out.push({ differ: d, lat }); continue;
      }
      if (c.kind === "peak") { out.push({ peak: fxt.peakAbs(y), finite: fxt.finite(y) }); continue; }
      const a = rate / 2, n = rate;
      const f0 = goertzel(y, c.hz, a, n);
      const dbc = (f) => 20 * Math.log10(goertzel(y, f, a, n) / f0);
      if (c.kind === "harm") { out.push({ h2: dbc(2 * c.hz), h3: dbc(3 * c.hz), levelDb: 20 * Math.log10(f0 / goertzel(x, c.hz, a, n)) }); continue; }
      if (c.kind === "alias") {
        const harm = []; for (let k = 1; k * c.hz < rate / 2; k++) harm.push(k * c.hz);
        let worst = -Infinity;
        for (let k = 1; k <= 80; k++) {
          let f = (k * c.hz) % rate; if (f > rate / 2) f = rate - f;
          if (f < 20 || harm.some((h) => Math.abs(h - f) < 4)) continue;
          worst = Math.max(worst, dbc(f));
        }
        out.push({ worstAlias: worst }); continue;
      }
    }
    return out;
  }, cases);

  test("off is the input delayed by the measured shaper latency, bit for bit", async ({ page }) => {
    const [r] = await run(page, [{ kind: "shifted", noise: true, on: false, p: { drive: 40, clip: "fuzz", xover: 150 }, inst: "bass" }]);
    console.log(`[fx-dsp] drive latency ${r.lat} frames`);
    expect(r.lat).toBeGreaterThan(0);
    expect(r.differ).toBe(0);
  });

  test("clip families: symmetric makes odd harmonics only, asymmetric adds even ones", async ({ page }) => {
    const [sym, asym, hard] = await run(page, [
      { kind: "harm", hz: 1000, amp: 0.5, p: { clip: "softsym", drive: 20 } },
      { kind: "harm", hz: 1000, amp: 0.5, p: { clip: "softasym", drive: 20 } },
      { kind: "harm", hz: 1000, amp: 0.5, p: { clip: "hard", drive: 30 } },
    ]);
    console.log(`[fx-dsp] harmonics dBc: soft sym H2 ${sym.h2.toFixed(1)} H3 ${sym.h3.toFixed(1)}; soft asym H2 ${asym.h2.toFixed(1)}; hard H3 ${hard.h3.toFixed(1)}`);
    expect(sym.h2).toBeLessThan(-60);
    expect(sym.h3).toBeGreaterThan(-30);
    expect(asym.h2).toBeGreaterThan(-40);
    expect(hard.h3).toBeGreaterThan(-20);
  });

  test("aliasing at 4x oversampling stays below the stated threshold", async ({ page }) => {
    // 1499 Hz, so folded harmonics land between the true ones.
    const r = await run(page, [
      { kind: "alias", hz: 1499, amp: 0.5, p: { clip: "softsym", drive: 20 } },
      { kind: "alias", hz: 1499, amp: 0.5, p: { clip: "hard", drive: 35 } },
      { kind: "alias", hz: 1499, amp: 0.5, p: { clip: "fuzz", drive: 40 } },
    ]);
    const raw = await page.evaluate(async () => {
      // The same hard clip on a bare WaveShaper without oversampling, for scale.
      const rate = 48000;
      const ctx = new OfflineAudioContext(1, rate * 2, rate);
      const s = fxt.source(ctx, 2, (t) => 0.5 * Math.sin(2 * Math.PI * 1499 * t));
      const g = ctx.createGain(); g.gain.value = 10 ** (35 / 20) / 64;
      const ws = ctx.createWaveShaper();
      const c = new Float32Array(16385); for (let i = 0; i < c.length; i++) c[i] = Math.max(-1, Math.min(1, 64 * (2 * i / 16384 - 1)));
      ws.curve = c; ws.oversample = "none";
      s.connect(g); g.connect(ws); ws.connect(ctx.destination); s.start();
      const y = (await ctx.startRendering()).getChannelData(0);
      const goertzel = (f) => { const w = 2 * Math.PI * f / rate, cc = 2 * Math.cos(w); let s1 = 0, s2 = 0; for (let i = 0; i < rate; i++) { const h = 0.5 - 0.5 * Math.cos(2 * Math.PI * i / (rate - 1)); const s0 = y[rate / 2 + i] * h + cc * s1 - s2; s2 = s1; s1 = s0; } return Math.sqrt(Math.max(1e-30, s1 * s1 + s2 * s2 - cc * s1 * s2)); };
      const f0 = goertzel(1499); const harm = []; for (let k = 1; k * 1499 < rate / 2; k++) harm.push(k * 1499);
      let worst = -Infinity;
      for (let k = 1; k <= 80; k++) { let f = (k * 1499) % rate; if (f > rate / 2) f = rate - f; if (f < 20 || harm.some((h) => Math.abs(h - f) < 4)) continue; worst = Math.max(worst, 20 * Math.log10(goertzel(f) / f0)); }
      return worst;
    });
    console.log(`[fx-dsp] worst alias dBc: soft ${r[0].worstAlias.toFixed(1)}, hard ${r[1].worstAlias.toFixed(1)}, fuzz ${r[2].worstAlias.toFixed(1)}; bare hard clip without oversampling ${raw.toFixed(1)}`);
    // Stated thresholds (measured -103, -99 and -103 dBc on Chromium 151):
    // soft below -90 dBc, hard and fuzz below -85 dBc, and the hard clip at
    // least 50 dB cleaner than the same clip without oversampling (-25 dBc).
    expect(r[0].worstAlias).toBeLessThan(-90);
    expect(r[1].worstAlias).toBeLessThan(-85);
    expect(r[2].worstAlias).toBeLessThan(-85);
    expect(r[1].worstAlias).toBeLessThan(raw - 50);
  });

  test("Low latency (a live input's switch): no latency, off is the input itself, its aliasing measured", async ({ page }) => {
    const [off, offOn] = await run(page, [
      { kind: "shifted", low: true, noise: true, on: false, p: { drive: 40, clip: "fuzz", xover: 150 }, inst: "bass" },
      // Blend 0 (the clean path only) while on: no alignment delay either.
      { kind: "shifted", low: true, noise: true, on: true, p: { drive: 40, clip: "hard", blend: 0 } },
    ]);
    const r = await run(page, [
      { kind: "alias", low: true, hz: 1499, amp: 0.5, p: { clip: "softsym", drive: 20 } },
      { kind: "alias", low: true, hz: 1499, amp: 0.5, p: { clip: "hard", drive: 35 } },
      { kind: "alias", low: true, hz: 1499, amp: 0.5, p: { clip: "fuzz", drive: 40 } },
    ]);
    console.log(`[fx-dsp] Low latency: latency ${off.lat} frames; off differs in ${off.differ} samples, clean blend in ${offOn.differ}; worst alias dBc without oversampling: soft ${r[0].worstAlias.toFixed(1)}, hard ${r[1].worstAlias.toFixed(1)}, fuzz ${r[2].worstAlias.toFixed(1)}`);
    expect(off.lat).toBe(0);
    expect(off.differ).toBe(0);
    expect(offOn.differ).toBe(0);
    // What the switch costs, stated in its Guide sentence as "a harsher top
    // on high notes": aliasing well above the 4x figures (-103, -99, -103
    // dBc), measured here so a change to it is seen.
    for (const x of r) expect(x.worstAlias).toBeGreaterThan(-90);
  });

  test("the clean blend is aligned with the driven path (no comb filter)", async ({ page }) => {
    // A 192-frame misalignment at 48 kHz would notch 625 Hz and pass 1 kHz.
    const r = await run(page, [
      { kind: "harm", hz: 625, amp: 0.001, p: { clip: "softsym", drive: 0, blend: 0.5 } },
      { kind: "harm", hz: 1000, amp: 0.001, p: { clip: "softsym", drive: 0, blend: 0.5 } },
    ]);
    console.log(`[fx-dsp] blend 50 %: ${r[0].levelDb.toFixed(2)} dB at 625 Hz, ${r[1].levelDb.toFixed(2)} dB at 1 kHz`);
    expect(Math.abs(r[0].levelDb - r[1].levelDb)).toBeLessThan(0.3);
  });

  test("every preset stays within +3.5 dBFS (1.5) on a full-scale input, with no NaN", async ({ page }) => {
    const presets = await page.evaluate(async () => (await import("/js/fx/pedalSpecs.js")).PEDALS.drive.presets.map((p) => ({ inst: p.inst, p: p.p })));
    const r = await run(page, presets.map((x) => ({ kind: "peak", noise: true, inst: x.inst, p: x.p })));
    console.log(`[fx-dsp] drive preset peaks: ${r.map((x) => x.peak.toFixed(2)).join(", ")}`);
    for (const x of r) { expect(x.finite).toBe(true); expect(x.peak).toBeLessThanOrEqual(1.5); }
  });

  test("lane latency compensation: a lane with a distortion and a lane without line up", async ({ page }) => {
    await setup(page);
    const r = await page.evaluate(async () => {
      const M = await fxt.mods();
      const rate = 48000;
      const extra = await fxt.loopExtra(M, rate);
      const ctx = new OfflineAudioContext(2, rate / 2, rate);
      const merge = ctx.createChannelMerger(2);
      merge.connect(ctx.destination);
      const member = (name, ch) => { const gain = ctx.createGain(); gain.connect(merge, 0, ch); return { controlName: name, gain, level: 1, chainHost: null, live: [] }; };
      const a = member("a", 0), b = member("b", 1);
      const hosts = M.host.createChainHosts(ctx, { members: () => [a, b], membersOf: (n) => [a, b].filter((m) => m.controlName === n), liveNodes: (m) => m.live });
      const desc = M.model.normalizeChain({ pedals: [{ iid: "dddd", type: "drive", on: true, p: { ...M.specs.pedalDefaults("drive", "guitar"), clip: "softsym", drive: 0, cab: "off", lowcut: 20, highcut: 12000, blend: 0.5 } }] });
      hosts.setLane("a", { desc, inst: "guitar", bpm: null, loopExtra: extra }, { immediate: true });
      for (const m of [a, b]) {
        const s = fxt.source(ctx, 0.5, (t, ch, i) => (i === 1000 ? 0.01 : 0));
        s.connect(hosts.inputOf(m)); s.start();
      }
      const res = await ctx.startRendering();
      const peak = (d) => { let k = 0; for (let i = 0; i < d.length; i++) if (Math.abs(d[i]) > Math.abs(d[k])) k = i; return k; };
      return { a: peak(res.getChannelData(0)), b: peak(res.getChannelData(1)), latency: hosts.latencySec() * rate, hostsBuilt: [a, b].filter((m) => m.chainHost).length };
    });
    console.log(`[fx-dsp] compensation: lane with drive peaks at ${r.a}, lane without at ${r.b}; latency ${r.latency} frames`);
    expect(r.hostsBuilt).toBe(2);
    expect(r.latency).toBeGreaterThan(0);
    expect(Math.abs(r.a - r.b)).toBeLessThanOrEqual(1);
    expect(r.b).toBe(1000 + r.latency);
  });
});

// ─── Reverb ───

test.describe("reverb DSP, offline", () => {
  test.beforeEach(async ({ page }) => { await setup(page); });

  test("every preset decays in its RT60 within 15 %, with no NaN and peaks within 2 on full-scale noise", async ({ page }) => {
    const r = await page.evaluate(async () => {
      const M = await fxt.mods();
      const rate = 48000;
      const extra = await fxt.loopExtra(M, rate);
      const out = [];
      for (const pr of M.specs.PEDALS.reverb.presets) {
        // Wet only (mix 1 on the pedal law) for the decay; the preset as it is for the peak.
        const wet = { iid: "rrrr", type: "reverb", on: true, preset: null, p: { ...M.specs.pedalDefaults("reverb", pr.inst), ...pr.p, mix: 1 } };
        const n = Math.round((pr.p.decay + 0.6) * rate);
        let ctx = new OfflineAudioContext(1, n, rate);
        let s = fxt.source(ctx, n / rate, (t, ch, i) => (i === 0 ? 1 : 0));
        let ser = M.pedals.createSeries(ctx, { pedals: [wet] }, { inst: "guitar", bpm: null, loopExtra: extra, songTime: 0, when: 0 });
        s.connect(ser.input); ser.output.connect(ctx.destination); s.start();
        const y = (await ctx.startRendering()).getChannelData(0);
        const e = new Float64Array(y.length); let acc = 0;
        for (let i = y.length - 1; i >= 0; i--) { acc += y[i] * y[i]; e[i] = acc; }
        const db = (i) => 10 * Math.log10(e[i] / e[0]);
        let a = 0; while (db(a) > -5) a++;
        let b = a; while (b < y.length - 1 && db(b) > -35) b++;
        const rt60 = ((b - a) / rate) * 2;
        const as = { iid: "rrrr", type: "reverb", on: true, preset: null, p: { ...M.specs.pedalDefaults("reverb", pr.inst), ...pr.p } };
        ctx = new OfflineAudioContext(2, rate * 2, rate);
        s = fxt.source(ctx, 2, (t, ch, i) => { const v = Math.sin(i * 12.9898 + ch * 7) * 43758.5453; return 2 * (v - Math.floor(v)) - 1; });
        ser = M.pedals.createSeries(ctx, { pedals: [as] }, { inst: pr.inst, bpm: null, loopExtra: extra, songTime: 0, when: 0 });
        s.connect(ser.input); ser.output.connect(ctx.destination); s.start();
        const res = await ctx.startRendering();
        const L = res.getChannelData(0), R = res.getChannelData(1);
        out.push({ id: pr.id, want: pr.p.decay, rt60, peak: Math.max(fxt.peakAbs(L), fxt.peakAbs(R)), finite: fxt.finite(L) && fxt.finite(R) });
      }
      return out;
    });
    console.log(`[fx-dsp] reverb presets: ${r.map((x) => `${x.id} RT60 ${x.rt60.toFixed(2)}/${x.want} s peak ${x.peak.toFixed(2)}`).join("; ")}`);
    for (const x of r) {
      expect(Math.abs(x.rt60 - x.want) / x.want, `${x.id}: RT60 ${x.rt60.toFixed(3)} s for ${x.want} s`).toBeLessThan(0.15);
      expect(x.finite).toBe(true);
      expect(x.peak, x.id).toBeLessThanOrEqual(2);
    }
  });

  test("pre-delay to the sample, and off or mix 0 is bit-identical", async ({ page }) => {
    const r = await page.evaluate(async () => {
      const M = await fxt.mods();
      const rate = 48000;
      const extra = await fxt.loopExtra(M, rate);
      const render = async (p, sig, on = true) => {
        const ctx = new OfflineAudioContext(1, rate, rate);
        const s = fxt.source(ctx, 1, sig);
        const pedal = { iid: "rrrr", type: "reverb", on, preset: null, p: { ...M.specs.pedalDefaults("reverb", "guitar"), type: "plate", decay: 0.5, mix: 1, ...p } };
        const ser = M.pedals.createSeries(ctx, { pedals: [pedal] }, { inst: "guitar", bpm: null, loopExtra: extra, songTime: 0, when: 0 });
        s.connect(ser.input); ser.output.connect(ctx.destination); s.start();
        return { y: (await ctx.startRendering()).getChannelData(0), x: s.buffer.getChannelData(0) };
      };
      const imp = (t, ch, i) => (i === 500 ? 1 : 0);
      const a = (await render({ predelay: 0 }, imp)).y;
      const b = (await render({ predelay: 37 }, imp)).y;
      // The lag that best maps the 0 ms render onto the 37 ms one.
      let best = -Infinity, lag = 0;
      for (let L = 1700; L <= 1850; L++) { let s = 0; for (let i = 0; i < 8000; i++) s += a[i] * b[i + L]; if (s > best) { best = s; lag = L; } }
      const noise = (t, ch, i) => Math.sin(i * 12.9898) * 0.8;
      const diff = (o) => { let n = 0; for (let i = 0; i < o.x.length; i++) if (o.x[i] !== o.y[i]) n++; return n; };
      return { lag, want: 0.037 * rate, off: diff(await render({}, noise, false)), mix0: diff(await render({ mix: 0 }, noise)) };
    });
    expect(r.lag).toBe(Math.round(r.want));
    expect(r.off).toBe(0);
    expect(r.mix0).toBe(0);
  });

  test("changing decay (a new IR) or pre-delay (a new line), both crossfaded, does not click", async ({ page }) => {
    const r = await page.evaluate(async () => {
      const M = await fxt.mods();
      const rate = 48000;
      const extra = await fxt.loopExtra(M, rate);
      const spec = (p) => ({ desc: M.model.normalizeChain({ pedals: [{ iid: "rrrr", type: "reverb", on: true, p: { ...M.specs.pedalDefaults("reverb", "guitar"), type: "hall", decay: 2, mix: 0.5, ...p } }] }), inst: "guitar", bpm: null, loopExtra: extra });
      const out = {};
      for (const [name, change] of [["decay", { decay: 4 }], ["predelay", { predelay: 80 }]]) {
        const ctx = new OfflineAudioContext(2, rate * 2, rate);
        const s = fxt.source(ctx, 2, (t) => 0.5 * Math.sin(2 * Math.PI * 220 * t));
        const host = M.host.createChainHost(ctx);
        host.apply(spec({}), { songTime: 0, when: 0 }, { immediate: true });
        s.connect(host.input); host.output.connect(ctx.destination); s.start();
        M.host.laterIn(ctx, 1.0, () => host.apply(spec(change), { songTime: 1, when: 1 }));
        const L = (await ctx.startRendering()).getChannelData(0);
        out[name] = { steady: fxt.maxStep(L, rate * 0.6, rate * 0.99), across: fxt.maxStep(L, rate * 0.99, rate * 1.06), finite: fxt.finite(L) };
      }
      return out;
    });
    console.log(`[fx-dsp] reverb change steps: decay ${r.decay.across.toFixed(4)} vs ${r.decay.steady.toFixed(4)}, pre-delay ${r.predelay.across.toFixed(4)} vs ${r.predelay.steady.toFixed(4)}`);
    for (const k of ["decay", "predelay"]) {
      expect(r[k].finite).toBe(true);
      expect(r[k].across, k).toBeLessThanOrEqual(r[k].steady * 1.1);
    }
  });
});
