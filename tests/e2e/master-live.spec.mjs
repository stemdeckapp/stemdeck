// The master in a real browser (Chromium), muted.
//
// 1. The live master's compressors: the makeup gain each DynamicsCompressorNode
//    adds is cancelled exactly (masterMath.compressorMakeupGain), so below its
//    thresholds the master passes the signal at unity; the "original" path is
//    sample-aligned with the processed one (same look-ahead), so A/B never
//    jumps in time. Offline renders, sample by sample.
// 2. The limiter approximation on a hot signal: how far the live peaks go
//    over the ceiling (the export's alimiter at 4x does not), logged.
// 3. In the studio: master off is bit-exact (the chain's output equals the
//    master EQ's output sample for sample); on, the effect is in the graph.
// 4. The drawer: from the footer chip and from the Export Mix menu, a preset
//    measures the mix on the server, the readout shows it, the export URL
//    carries master=, the master is saved with the song and comes back after
//    a reload; off, the URL has no master=.

import { test, expect } from "@playwright/test";
import { JOB_ID, openStudio } from "./helpers.mjs";

test.use({ launchOptions: { args: ["--mute-audio", "--autoplay-policy=no-user-gesture-required"] } });

const decode = (p) => JSON.parse(Buffer.from(p, "base64url").toString("utf8"));

test("the live master's compressors pass a quiet signal at unity, and A/B is aligned", async ({ page }) => {
  await page.goto("/");
  const r = await page.evaluate(async () => {
    const { createMasterEffect } = await import("/js/masterFx.js");
    const { compressorMakeupGain } = await import("/js/masterMath.js");
    let rate = 48000;
    let len = rate;
    async function render(params, level = 10 ** (-40 / 20)) {
      const ctx = new OfflineAudioContext(2, len, rate);
      const buf = ctx.createBuffer(1, len, rate);
      const d = buf.getChannelData(0);
      for (let i = 0; i < len; i++) d[i] = level * Math.sin((2 * Math.PI * 997 * i) / rate);
      const src = ctx.createBufferSource();
      src.buffer = buf;
      const fx = createMasterEffect(ctx);
      fx.set(params, { immediate: true });
      const delay = ctx.createDelay(0.1);
      delay.delayTime.value = fx.latencySec;
      const merger = ctx.createChannelMerger(2);
      src.connect(fx.input);
      fx.output.connect(merger, 0, 0); // left: through the master
      src.connect(delay);
      delay.connect(merger, 0, 1); // right: the input, delayed by the same look-ahead
      merger.connect(ctx.destination);
      src.start(0);
      const out = await ctx.startRendering();
      return { a: out.getChannelData(0), b: out.getChannelData(1) };
    }
    const from = Math.floor(rate * 0.2);
    const ratioDb = ({ a, b }) => {
      let ea = 0;
      let eb = 0;
      for (let i = from; i < len; i++) { ea += a[i] * a[i]; eb += b[i] * b[i]; }
      return 10 * Math.log10(ea / eb);
    };
    const residualDb = ({ a, b }) => {
      let er = 0;
      let eb = 0;
      for (let i = from; i < len; i++) { er += (a[i] - b[i]) ** 2; eb += b[i] * b[i]; }
      return 10 * Math.log10(er / eb + 1e-30);
    };
    const base = { gainDb: 0, ceiling: -1, fader: 1, listen: "master", match: false, diffLu: NaN };
    const cases = {
      limiterOnly: { ...base, comp: 0, compThresholdDb: null },
      glueHalf: { ...base, comp: 0.5, compThresholdDb: -15 },
      glueFull: { ...base, comp: 1, compThresholdDb: -20 },
      original: { ...base, comp: 0.5, compThresholdDb: -15, listen: "original" },
    };
    const out = {};
    for (const [name, p] of Object.entries(cases)) {
      const res = await render(p);
      out[name] = { gainDb: ratioDb(res), residualDb: residualDb(res) };
    }
    // The fader (already applied upstream, to every lane) is undone before
    // the master and applied again after it: the master sees the export
    // level, and what comes out is at the input's level.
    const faded = await render({ ...cases.glueHalf, fader: 0.5 });
    out.fader = { gainDb: ratioDb(faded) };
    // Matched levels: a master measured 4 LU louder is turned down by 4 dB.
    const matched = await render({ ...cases.limiterOnly, match: true, diffLu: 4 });
    out.matched = { gainDb: ratioDb(matched) };
    // A hot signal (+6 dBFS sine) into the limiter at -1 dBTP: the live peak.
    const hot = await render({ ...cases.limiterOnly }, 2);
    let peak = 0;
    for (let i = from; i < len; i++) peak = Math.max(peak, Math.abs(hot.a[i]));
    out.hotPeakDb = 20 * Math.log10(peak);
    out.makeups = [[-1.1, 0, 20], [-15, 6, 2], [-20, 6, 3]].map(([t, k, r]) => compressorMakeupGain(t, k, r));
    // At 44.1 kHz the look-ahead is not a whole number of frames: the
    // original path must still line up with the master to the sample.
    rate = 44100;
    len = rate;
    const res441 = await render(cases.glueHalf);
    out.at441 = { gainDb: ratioDb(res441), residualDb: residualDb(res441) };
    return out;
  });
  console.log(JSON.stringify(r, null, 1));
  for (const name of ["limiterOnly", "glueHalf", "glueFull", "original"]) {
    expect(Math.abs(r[name].gainDb), `${name} gain`).toBeLessThan(0.02);
    // Sample-aligned: the residual is float rounding, not a time shift (a
    // one-sample shift of a 997 Hz sine at 48 kHz would be -18 dB).
    expect(r[name].residualDb, `${name} residual`).toBeLessThan(-60);
  }
  expect(Math.abs(r.fader.gainDb)).toBeLessThan(0.02);
  expect(r.at441.residualDb).toBeLessThan(-60);
  expect(Math.abs(r.matched.gainDb + 4)).toBeLessThan(0.02);
  // Approximate by design (ratio 20, sample peaks): logged, and bounded so a
  // regression that stops it limiting fails.
  expect(r.hotPeakDb).toBeLessThan(1.0);
});

async function waitForEngine(page) {
  await expect.poll(() => page.evaluate(async () => {
    const { audioEngine } = await import("/js/state.js");
    return audioEngine?.supportsMaster === true;
  }), { timeout: 15000 }).toBe(true);
}

/** The loudness route answers for real (openStudio stubs every mix* URL). */
async function realLoudness(page) {
  await page.route("**/api/jobs/*/mixdown/loudness**", (route) => route.continue());
}

test("in the studio: master off is bit-exact, on puts the effect in the graph", async ({ page }) => {
  test.setTimeout(60_000);
  await openStudio(page);
  await realLoudness(page);
  await page.locator("#t-play").click();
  await waitForEngine(page);
  const r = await page.evaluate(async () => {
    const { audioEngine } = await import("/js/state.js");
    const master = await import("/js/masterEngine.js");
    const sleep = (ms) => new Promise((res) => setTimeout(res, ms));
    const ctx = audioEngine.audioContext;
    const slotTap = audioEngine.getMasterTap("post", { fftSize: 4096, smoothing: 0 });
    const out = ctx.createAnalyser();
    out.fftSize = 4096;
    audioEngine.getMasterOutputNode().connect(out);
    const a = new Float32Array(4096);
    const b = new Float32Array(4096);
    const a2 = new Float32Array(4096);
    async function same() {
      for (let tries = 0; tries < 200; tries++) {
        slotTap.getFloatTimeDomainData(a);
        out.getFloatTimeDomainData(b);
        slotTap.getFloatTimeDomainData(a2);
        if (a.every((v, i) => v === a2[i]) && a.some((v) => v !== 0)) return a.every((v, i) => v === b[i]);
        await sleep(3);
      }
      return null;
    }
    await sleep(300);
    const offExact = await same();
    const offNodes = audioEngine.getMasterFxNodes();
    master.choosePreset("streaming");
    await sleep(300);
    const onNodes = !!audioEngine.getMasterFxNodes();
    const onDiffers = (await same()) === false;
    master.setMasterOn(false);
    await sleep(400);
    const backExact = await same();
    return { offExact, offNodes, onNodes, onDiffers, backExact, latency: audioEngine.masterFxLatency() };
  });
  expect(r.offExact).toBe(true);
  expect(r.offNodes).toBeNull();
  expect(r.onNodes).toBe(true);
  expect(r.onDiffers).toBe(true);
  expect(r.backExact).toBe(true);
  expect(r.latency).toBe(0);
});

test("the Master drawer measures, exports with master=, and is saved with the song", async ({ page }) => {
  test.setTimeout(90_000);
  await openStudio(page, { tauri: true });
  await realLoudness(page);
  await waitForEngine(page);

  // Off: the chip says so and the export URL has no master.
  await expect(page.locator("#t-master-btn")).toHaveAttribute("data-state", "off");

  // From the Export Mix menu.
  await page.locator("#t-export-btn").click();
  await expect(page.locator("#t-export-master .chip-item-title")).toHaveText("Master: Off");
  await page.locator("#t-export-master").click();
  await expect(page.locator("#master-drawer")).toBeVisible();
  await expect(page.locator("#t-export-panel")).toBeHidden();
  await expect(page.locator("#t-master-btn")).toHaveAttribute("aria-expanded", "true");

  await page.locator('.master-preset[data-preset="streaming"]').click();
  await expect(page.locator(".master-status")).toHaveAttribute("data-state", "ready", { timeout: 30000 });
  await expect(page.locator(".master-aLufs")).toHaveText("-14.0");
  await expect(page.locator(".master-power")).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator("#t-master-btn")).toHaveAttribute("data-state", "streaming");
  await expect(page.locator("#t-master-btn")).toHaveAttribute("aria-label", "Master: Streaming");
  // The master EQ is E2's general panel on the master lane.
  await expect(page.locator("#master-drawer .eq-graph-panel")).toBeVisible();

  // Export: the URL carries the master.
  await page.locator("#t-export-btn").click();
  await expect(page.locator("#t-export-master .chip-item-title")).toHaveText("Master: Streaming");
  await page.locator("#t-export-mix").click();
  await page.waitForFunction(() => window.__e2e.pickPending());
  await page.evaluate(() => window.__e2e.choosePath());
  await page.waitForFunction(() => window.__e2e.callsFor("download_to_path").length === 1);
  const url = await page.evaluate(() => window.__e2e.callsFor("download_to_path")[0].args.url);
  const master = new URL(url).searchParams.get("master");
  expect(master).toBeTruthy();
  expect(decode(master)).toEqual({ v: 1, target: -14, ceiling: -1 });
  await page.evaluate(() => window.__e2e.finishSave());

  // Louder with the glue: the wire carries the compressor.
  await page.locator('.master-preset[data-preset="louder"]').click();
  const wire = await page.evaluate(async () => (await import("/js/masterEngine.js")).exportMasterParam());
  expect(decode(wire)).toEqual({ v: 1, target: -9, ceiling: -1, comp: 0.5 });

  // Saved with the song's mix, after the master EQ slot entry.
  await page.waitForFunction(() => {
    const m = JSON.parse(localStorage.getItem(`stemdeck:mix:${"e2e0deadbeef"}:daw`) || "null");
    return m?.__master?.fx?.some((x) => x.id === "master" && x.params?.preset === "louder");
  }, null, { timeout: 5000 });
  await page.reload();
  await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
  await page.waitForFunction(
    () => !/\/\s*00:00\s*$/.test(document.querySelector("#t-time")?.textContent || "00:00 / 00:00"),
    null,
    { timeout: 20000 },
  );
  await expect(page.locator("#t-master-btn")).toHaveAttribute("data-state", "louder");

  // Off again: exports carry no master.
  await page.locator("#t-master-btn").click();
  await expect(page.locator('.master-preset[data-preset="louder"]')).toHaveAttribute("aria-pressed", "true");
  await page.locator(".master-power").click();
  await expect(page.locator("#t-master-btn")).toHaveAttribute("data-state", "off");
  const off = await page.evaluate(async () => (await import("/js/masterEngine.js")).exportMasterParam());
  expect(off).toBe("");
  // Escape closes the drawer.
  await page.locator(".master-power").press("Escape");
  await expect(page.locator("#master-drawer")).toBeHidden();
});

test("custom target and ceiling, and the Guide explains the chip", async ({ page }) => {
  test.setTimeout(60_000);
  await openStudio(page);
  await realLoudness(page);
  await page.locator("#t-master-btn").click();
  await page.locator('.master-preset[data-preset="custom"]').click();
  const target = page.locator(".master-target input");
  await expect(target).toBeEnabled();
  await target.fill("-16");
  await target.press("Enter");
  await target.blur();
  const ceiling = page.locator(".master-ceiling input");
  await ceiling.fill("-2");
  await ceiling.blur();
  await expect.poll(() => page.evaluate(async () => (await import("/js/masterEngine.js")).exportMasterParam()))
    .toBeTruthy();
  const wire = await page.evaluate(async () => (await import("/js/masterEngine.js")).exportMasterParam());
  expect(decode(wire)).toEqual({ v: 1, target: -16, ceiling: -2 });
  // Out of range is clamped to what the server accepts.
  await target.fill("-60");
  await target.blur();
  const clamped = await page.evaluate(async () => (await import("/js/masterEngine.js")).exportMasterParam());
  expect(decode(clamped).target).toBe(-24);
  await expect(page.locator(".master-status")).toHaveAttribute("data-state", "ready", { timeout: 30000 });

  // Guide: pointing at the chip explains it.
  await page.locator("#helpModeBtn").click();
  await expect(page.locator("body")).toHaveClass(/help-mode/);
  await page.locator("#t-master-btn").hover();
  await expect(page.locator("#helpTip")).toBeVisible();
  await expect(page.locator("#helpTip")).toContainText("loudness target");
  await page.locator(".master-readout").hover();
  await expect(page.locator("#helpTip")).toContainText("measured by the server");
});

test("render and listen: the exact excerpt, mastered and original, loudness-matched", async ({ page }) => {
  test.setTimeout(60_000);
  await openStudio(page);
  await realLoudness(page);
  // The excerpt is a real region export (openStudio stubs mix* URLs).
  await page.route("**/api/jobs/*/mixdown.wav**", (route) => route.continue());
  const asked = [];
  page.on("response", (r) => {
    if (r.url().includes("/mixdown.wav")) asked.push({ url: r.url(), status: r.status() });
  });
  await waitForEngine(page);
  await page.locator("#t-master-btn").click();
  await page.locator('.master-preset[data-preset="louder"]').click();
  await expect(page.locator(".master-status")).toHaveAttribute("data-state", "ready", { timeout: 30000 });
  await page.locator(".master-excerpt-btn").click();
  await expect(page.locator("#master-drawer")).toHaveAttribute("data-excerpt", "playing", { timeout: 30000 });
  await expect(page.locator(".master-excerpt-state")).toContainText("Exact render");
  expect(asked).toHaveLength(2);
  expect(asked.every((a) => a.status === 200)).toBe(true);
  const withMaster = asked.filter((a) => new URL(a.url).searchParams.get("master"));
  expect(withMaster).toHaveLength(1);
  for (const a of asked) {
    const q = new URL(a.url).searchParams;
    expect(Number(q.get("start"))).toBeGreaterThanOrEqual(0);
    expect(Number(q.get("end"))).toBeGreaterThan(Number(q.get("start")));
  }
  // Original selected: the excerpt switches with it; Stop ends it.
  await page.locator('.master-listen-seg [data-listen="original"]').click();
  await expect(page.locator('.master-listen-seg [data-listen="original"]')).toHaveAttribute("aria-pressed", "true");
  await page.locator(".master-excerpt-btn").click();
  await expect(page.locator("#master-drawer")).not.toHaveAttribute("data-excerpt", "playing");
});
