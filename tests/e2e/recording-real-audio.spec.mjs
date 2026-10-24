// Recording from real hardware: skipped unless STEMDECK_REAL_AUDIO=1.
//
// CI has no audio interface, so these run only on a machine with one, by
// hand: an audio interface (here a Yamaha MG12XU, "MG-XU" on Windows) with a
// signal on its channel 1 (panned hard left, so Windows' stereo input carries
// it on the LEFT channel only), and a microphone in a quiet room.
//
//   STEMDECK_REAL_AUDIO=1 npx playwright test recording-real-audio
//
// Optional: STEMDECK_REAL_BROWSER=msedge, STEMDECK_REAL_AUDIO_KEEP=1 (leave the
// takes on disk for ffprobe), STEMDECK_REAL_INTERFACE (default "MG-XU") and STEMDECK_REAL_MIC
// (default "StreamCam") match the device names the app shows.
//
// The browser is launched with --use-fake-ui-for-media-stream (permission
// granted without a prompt) but WITHOUT --use-fake-device-for-media-stream,
// so getUserMedia opens the real devices. --mute-audio keeps the speakers
// silent: the context still runs and every node is still measurable, only
// the output device gets nothing. What reaches the speakers is measured by a
// tap on every connection to the destination.
//
// Every number is printed (dBFS, RMS and peak, per channel) so a run doubles
// as a measurement.
import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { openStudio, JOB_ID } from "./helpers.mjs";
import { recBtn, trackRows, removeTracks, jobState, addTrack, waitForEngine, watchTakes } from "./recording-helpers.mjs";

const REAL = process.env.STEMDECK_REAL_AUDIO === "1";
const IFACE = process.env.STEMDECK_REAL_INTERFACE || "MG-XU";
const MIC = process.env.STEMDECK_REAL_MIC || "StreamCam";
const E2E_DIR = process.env.STEMDECK_E2E_DIR || "";

test.skip(!REAL, "needs real audio hardware: set STEMDECK_REAL_AUDIO=1");

test.use({
  // STEMDECK_REAL_BROWSER=msedge runs the same checks in Edge.
  ...(process.env.STEMDECK_REAL_BROWSER ? { channel: process.env.STEMDECK_REAL_BROWSER } : {}),
  launchOptions: { args: ["--use-fake-ui-for-media-stream", "--mute-audio", "--autoplay-policy=no-user-gesture-required"] },
  permissions: ["microphone"],
});

// STEMDECK_REAL_AUDIO_KEEP=1 leaves the takes on disk, for ffprobe.
test.afterEach(async ({ request }) => { if (process.env.STEMDECK_REAL_AUDIO_KEEP !== "1") await removeTracks(request); });
test.beforeEach(async ({ request }) => removeTracks(request));

// ─── Measuring ───

const dB = (x) => (x > 0 ? 20 * Math.log10(x) : -200);
const fmt = (x) => `${x.toFixed(1)} dBFS`;

/** Per-channel RMS and peak (dBFS) of a WAV file: PCM16 or float32. */
function wavStats(buf) {
  let p = 12;
  let fmtInfo = null;
  let data = null;
  while (p + 8 <= buf.length) {
    const id = buf.toString("ascii", p, p + 4);
    const size = buf.readUInt32LE(p + 4);
    if (id === "fmt ") fmtInfo = { tag: buf.readUInt16LE(p + 8), ch: buf.readUInt16LE(p + 10), rate: buf.readUInt32LE(p + 12), bits: buf.readUInt16LE(p + 22) };
    if (id === "data") data = buf.subarray(p + 8, p + 8 + size);
    p += 8 + size + (size & 1);
  }
  const { ch, bits, tag, rate } = fmtInfo;
  const bytes = bits / 8;
  const frames = Math.floor(data.length / (bytes * ch));
  const out = [];
  for (let c = 0; c < ch; c++) {
    let sum = 0;
    let peak = 0;
    for (let i = 0; i < frames; i++) {
      const o = (i * ch + c) * bytes;
      const v = tag === 3 ? data.readFloatLE(o) : data.readInt16LE(o) / 32768;
      sum += v * v;
      peak = Math.max(peak, Math.abs(v));
    }
    out.push({ rms: dB(Math.sqrt(sum / frames)), peak: dB(peak) });
  }
  return { rate, channels: ch, frames, seconds: frames / rate, perChannel: out };
}

/** Samples of channel 0 of a WAV, for comparing two signals. */
function wavChannel(buf, c = 0) {
  let p = 12;
  let f = null;
  let data = null;
  while (p + 8 <= buf.length) {
    const id = buf.toString("ascii", p, p + 4);
    const size = buf.readUInt32LE(p + 4);
    if (id === "fmt ") f = { tag: buf.readUInt16LE(p + 8), ch: buf.readUInt16LE(p + 10), bits: buf.readUInt16LE(p + 22) };
    if (id === "data") data = buf.subarray(p + 8, p + 8 + size);
    p += 8 + size + (size & 1);
  }
  const bytes = f.bits / 8;
  const frames = Math.floor(data.length / (bytes * f.ch));
  const s = new Float32Array(frames);
  for (let i = 0; i < frames; i++) {
    const o = (i * f.ch + c) * bytes;
    s[i] = f.tag === 3 ? data.readFloatLE(o) : data.readInt16LE(o) / 32768;
  }
  return s;
}

function log(...a) { console.log("[real-audio]", ...a); }

const SHOTS = process.env.STEMDECK_REAL_AUDIO_SHOTS || "";
const shot = (name) => (SHOTS ? path.join(SHOTS, name) : test.info().outputPath(name));

/** The mixer rows of the recording tracks, for a screenshot. */
async function rowClip(page) {
  const box = await page.evaluate(() => {
    const rows = [...document.querySelectorAll(".mixer-column .lane-header.track-lane")];
    const r = rows.map((e) => e.getBoundingClientRect());
    const top = Math.min(...r.map((b) => b.top));
    const bottom = Math.max(...r.map((b) => b.bottom));
    return { x: r[0].left, y: top - 4, width: r[0].width, height: bottom - top + 8 };
  });
  return box;
}

/** The raw capture (float32, every recorded channel) of a take, from disk. */
function rawTake(takeId) {
  if (!E2E_DIR) return null;
  const file = path.join(E2E_DIR, "jobs", JOB_ID, "takes", takeId, "audio.raw.wav");
  return fs.existsSync(file) ? fs.readFileSync(file) : null;
}

/**
 * Tap everything that is connected to an AudioContext's destination, before
 * the app builds its graph: window.__outLevel(ms) is the RMS and peak (dBFS)
 * of what the speakers would get.
 */
async function installOutputTap(page) {
  await page.addInitScript(() => {
    const orig = AudioNode.prototype.connect;
    const taps = new WeakMap();
    AudioNode.prototype.connect = function (dest, ...rest) {
      const r = orig.call(this, dest, ...rest);
      if (typeof AudioDestinationNode !== "undefined" && dest instanceof AudioDestinationNode) {
        let tap = taps.get(dest.context);
        if (!tap) {
          tap = dest.context.createAnalyser();
          tap.fftSize = 2048;
          tap.channelCount = 2;
          tap.channelCountMode = "explicit";
          taps.set(dest.context, tap);
        }
        window.__tapCtx = dest.context;
        window.__tap = tap;
        try { orig.call(this, tap, rest[0] ?? 0); } catch { /* not connectable */ }
      }
      return r;
    };
    window.__outLevel = async (ms = 1500) => {
      const tap = window.__tap;
      if (!tap) return { rms: -200, peak: -200 };
      const buf = new Float32Array(tap.fftSize);
      let sum = 0;
      let n = 0;
      let peak = 0;
      const t0 = performance.now();
      while (performance.now() - t0 < ms) {
        await new Promise((r) => setTimeout(r, 40));
        tap.getFloatTimeDomainData(buf);
        for (const v of buf) { sum += v * v; n++; peak = Math.max(peak, Math.abs(v)); }
      }
      const d = (x) => (x > 0 ? 20 * Math.log10(x) : -200);
      return { rms: d(Math.sqrt(sum / Math.max(1, n))), peak: d(peak) };
    };
  });
}

// ─── Driving the app ───

/**
 * Pick a track's input: an instrument option reads "MG-XU · Input 1", a
 * microphone's is its name (one input, so `channelText` is null).
 */
async function chooseTrackInput(page, row, deviceMatch, channelText) {
  const sel = row.locator(".track-input");
  await expect.poll(async () => (await sel.locator("option").allTextContents()).length, { timeout: 15000 }).toBeGreaterThan(0);
  const value = await sel.evaluate((s, { dev, ch }) => {
    for (const o of s.options) {
      const text = o.textContent.trim();
      if (o.disabled || !text.includes(dev)) continue;
      if (ch == null ? o.dataset.kind === "voice" : text.endsWith(` · ${ch}`)) return o.value;
    }
    return null;
  }, { dev: deviceMatch, ch: channelText });
  expect(value, `${deviceMatch} ${channelText ?? ""} in the track's input list`).not.toBeNull();
  await sel.selectOption(value);
}

/** Record `ms` with the armed tracks; returns the finished takes' ids in order. */
async function recordArmed(page, takesLog, ms = 5000) {
  const before = takesLog.finishStatus.length;
  await page.evaluate(async () => {
    const st = await import("/js/state.js");
    st.audioEngine?.setTime?.(0);
  });
  await recBtn(page).click();
  await expect(recBtn(page)).toHaveClass(/recording/, { timeout: 15000 });
  await page.waitForTimeout(ms);
  if (await recBtn(page).evaluate((b) => b.classList.contains("recording"))) await recBtn(page).click();
  await expect(recBtn(page)).not.toHaveClass(/recording/, { timeout: 15000 });
  const armedCount = await trackRows(page).locator('.track-arm[aria-pressed="true"]').count();
  await expect.poll(() => takesLog.finishStatus.length - before, { timeout: 30000 }).toBe(Math.max(1, armedCount));
  expect(takesLog.finishStatus.slice(before).every((s) => s === 200)).toBe(true);
  const job = await jobState(page);
  return job.takes.slice(-Math.max(1, armedCount));
}

test.describe("real audio hardware", () => {
  test.setTimeout(180_000);

  test("the interface: Input 1 carries the loop, Input 2 is silent, 1+2 is left only", async ({ page }) => {
    await installOutputTap(page);
    const takesLog = watchTakes(page);
    await openStudio(page);
    await addTrack(page);
    await waitForEngine(page);
    const row = trackRows(page).first();

    const results = {};
    for (const ch of ["Input 1", "Input 2", "Input 1+2"]) {
      await chooseTrackInput(page, row, IFACE, ch);
      await waitForEngine(page);
      const arm = trackRows(page).first().locator(".track-arm");
      if ((await arm.getAttribute("aria-pressed")) !== "true") await arm.click();
      const [take] = await recordArmed(page, takesLog);
      await page.locator("#recKeep").click();
      await page.keyboard.press("Escape");
      const raw = rawTake(take.id);
      const wav = Buffer.from(await (await page.request.get(take.url)).body());
      results[ch] = { take: take.id, raw: raw && wavStats(raw), wav: wavStats(wav), finish: takesLog.finishes.at(-1).body.capture };
      log(ch, "capture:", JSON.stringify(results[ch].finish));
      if (results[ch].raw) log(ch, "audio.raw.wav:", results[ch].raw.channels, "ch", results[ch].raw.perChannel.map((c, i) => `ch${i + 1} rms ${fmt(c.rms)} peak ${fmt(c.peak)}`).join(" | "));
      log(ch, "audio.wav:", results[ch].wav.perChannel.map((c, i) => `${i ? "R" : "L"} rms ${fmt(c.rms)} peak ${fmt(c.peak)}`).join(" | "));
      await waitForEngine(page);
    }

    // Levels are compared with each other, not with a fixed number: the
    // mixer's faders set the absolute level. Input 1: the loop, well above
    // the converter's floor. Input 2: the floor (about -105 dBFS here).
    const in1 = results["Input 1"].raw.perChannel[0];
    const in2 = results["Input 2"].raw.perChannel[0];
    expect(results["Input 1"].raw.channels).toBe(1);
    expect(results["Input 2"].raw.channels).toBe(1);
    expect(in1.rms).toBeGreaterThan(-80);
    expect(in2.rms).toBeLessThan(-85);
    expect(in1.rms - in2.rms).toBeGreaterThan(25);
    // Input 1+2: a stereo take, left the loop and right silent.
    const pair = results["Input 1+2"].raw;
    expect(pair.channels).toBe(2);
    expect(pair.perChannel[0].rms - pair.perChannel[1].rms).toBeGreaterThan(25);
    expect(pair.perChannel[1].rms).toBeLessThan(-85);
    // The saved, aligned take (mono written to both sides, padded to the
    // song) holds the loop on both sides.
    for (const c of results["Input 1"].wav.perChannel) expect(c.rms - in2.rms).toBeGreaterThan(20);
  });

  test("the microphone records the room, not the loop", async ({ page }) => {
    await installOutputTap(page);
    const takesLog = watchTakes(page);
    await openStudio(page);
    await addTrack(page);
    await waitForEngine(page);
    const row = trackRows(page).first();
    await chooseTrackInput(page, row, IFACE, "Input 1");
    await waitForEngine(page);
    await trackRows(page).first().locator(".track-arm").click();
    const [loopTake] = await recordArmed(page, takesLog);
    await page.locator("#recKeep").click();
    await page.keyboard.press("Escape");
    await waitForEngine(page);

    await chooseTrackInput(page, trackRows(page).first(), MIC, null);
    await waitForEngine(page);
    const arm = trackRows(page).first().locator(".track-arm");
    if ((await arm.getAttribute("aria-pressed")) !== "true") await arm.click();
    const [micTake] = await recordArmed(page, takesLog);
    await page.locator("#recKeep").click();
    const loop = rawTake(loopTake.id);
    const mic = rawTake(micTake.id);
    const ls = wavStats(loop);
    const ms = wavStats(mic);
    log("interface Input 1 raw:", ls.perChannel.map((c) => `rms ${fmt(c.rms)} peak ${fmt(c.peak)}`).join(" | "));
    log("microphone Input 1 raw:", ms.perChannel.map((c) => `rms ${fmt(c.rms)} peak ${fmt(c.peak)}`).join(" | "));
    // Different signals: the normalised cross-correlation of the two takes,
    // searched over +-50 ms of lag, stays low.
    const a = wavChannel(loop);
    const b = wavChannel(mic);
    const n = Math.min(a.length, b.length) - 4410;
    let best = 0;
    const energy = (x, off) => { let s = 0; for (let i = 0; i < n; i++) s += x[i + off] * x[i + off]; return Math.sqrt(s); };
    const ea = energy(a, 2205);
    for (let lag = -2205; lag <= 2205; lag += 21) {
      let s = 0;
      for (let i = 0; i < n; i++) s += a[i + 2205] * b[i + 2205 + lag];
      best = Math.max(best, Math.abs(s) / (ea * energy(b, 2205 + lag) || 1));
    }
    log("loop vs microphone: peak normalised cross-correlation", best.toFixed(3));
    expect(best).toBeLessThan(0.5);
    expect(micTake.id).not.toBe(loopTake.id);
  });

  test("two tracks on Input 1 and Input 2 of the interface record together", async ({ page }) => {
    const takesLog = watchTakes(page);
    await openStudio(page);
    const a = await addTrack(page);
    await waitForEngine(page);
    const b = await addTrack(page);
    await waitForEngine(page);
    await chooseTrackInput(page, trackRows(page).nth(0), IFACE, "Input 1");
    await waitForEngine(page);
    await chooseTrackInput(page, trackRows(page).nth(1), IFACE, "Input 2");
    await waitForEngine(page);
    await trackRows(page).nth(0).locator(".track-arm").click();
    await trackRows(page).nth(1).locator(".track-arm").click();
    const takes = await recordArmed(page, takesLog, 4000);
    expect(takes.length).toBe(2);
    const byTrack = Object.fromEntries(takes.map((t) => [t.track_id, wavStats(rawTake(t.id))]));
    log("track on Input 1:", fmt(byTrack[a].perChannel[0].rms), "peak", fmt(byTrack[a].perChannel[0].peak));
    log("track on Input 2:", fmt(byTrack[b].perChannel[0].rms), "peak", fmt(byTrack[b].perChannel[0].peak));
    expect(byTrack[a].perChannel[0].rms - byTrack[b].perChannel[0].rms).toBeGreaterThan(25);
    expect(byTrack[b].perChannel[0].rms).toBeLessThan(-85);
    await page.locator("#recKeep").click();
  });
});

test.describe("real audio hardware: what the user sees and hears", () => {
  test.setTimeout(180_000);

  test("armed track on the interface: meter, monitoring, take, playback", async ({ page }) => {
    await installOutputTap(page);
    const takesLog = watchTakes(page);
    await openStudio(page);
    await addTrack(page);
    await waitForEngine(page);
    await chooseTrackInput(page, trackRows(page).first(), IFACE, "Input 1");
    await waitForEngine(page);
    await trackRows(page).first().locator(".track-arm").click();
    await page.waitForTimeout(1500);

    // (a) The armed track's own meter shows the input.
    const vu = trackRows(page).first().locator(".lane-vu");
    const vuLevels = [];
    for (let i = 0; i < 10; i++) {
      vuLevels.push(await vu.evaluate((e) => parseFloat(e.style.getPropertyValue("--vu-in")) || 0));
      await page.waitForTimeout(100);
    }
    log("(a) armed track meter, input level samples (% of a -60..0 dBFS scale):", vuLevels.join(","));
    // -60 dBFS is an empty meter: the loop shows.
    expect(Math.max(...vuLevels)).toBeGreaterThan(3);
    await page.screenshot({ path: shot("track-row-armed.png"), clip: await rowClip(page) });

    // (b) Monitoring: what reaches the speakers with the transport stopped.
    const silent = await page.evaluate(() => window.__outLevel(1000));
    await page.locator("#t-rec-opts").click();
    await expect(page.locator("#recPanel")).toBeVisible();
    await page.waitForTimeout(1500);
    const panelMeter = await page.locator("#recMeterFill").evaluate((e) => e.style.transform);
    log("(a) recording panel meter:", panelMeter);
    expect(parseFloat(panelMeter.replace(/[^0-9.]/g, ""))).toBeGreaterThan(0.03);
    await page.locator("#recMonitor").check();
    await page.waitForTimeout(500);
    const monitored = await page.evaluate(() => window.__outLevel(1500));
    log("(b) output, monitor off:", fmt(silent.rms), "| monitor on:", fmt(monitored.rms), "peak", fmt(monitored.peak));
    expect(silent.rms).toBeLessThan(-90);
    expect(monitored.rms - silent.rms).toBeGreaterThan(20);
    await page.locator("#recMonitor").uncheck();
    await page.keyboard.press("Escape");

    // (c) The take.
    const [take] = await recordArmed(page, takesLog);
    const raw = wavStats(rawTake(take.id));
    log("(c) take raw:", raw.perChannel.map((c) => `rms ${fmt(c.rms)} peak ${fmt(c.peak)}`).join(" | "));
    expect(raw.perChannel[0].rms).toBeGreaterThan(-80);
    await page.locator("#recKeep").click();
    await page.keyboard.press("Escape");
    await waitForEngine(page);

    // (d) The kept take plays in its lane: solo it and play.
    await trackRows(page).first().locator(".solo").click();
    await page.evaluate(async () => {
      const st = await import("/js/state.js");
      st.audioEngine?.setTime?.(0);
    });
    await page.locator("#t-play").click();
    await page.waitForTimeout(800);
    const played = await page.evaluate(() => window.__outLevel(2500));
    await page.locator("#t-play").click();
    log("(d) output playing the soloed take:", fmt(played.rms), "peak", fmt(played.peak));
    expect(played.rms - silent.rms).toBeGreaterThan(20);
  });
});

test.describe("real audio hardware: the track rows", () => {
  test("a voice track and an instrument track, as the user sees them", async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 1000 });
    await openStudio(page);
    await addTrack(page);
    await waitForEngine(page);
    await addTrack(page);
    await waitForEngine(page);
    const options = await trackRows(page).first().locator(".track-input").evaluate((s) => [...s.querySelectorAll("optgroup")].map((g) => ({
      label: g.label, options: [...g.querySelectorAll("option")].map((o) => o.textContent),
    })));
    log("track input list:", JSON.stringify(options));
    expect(options.map((g) => g.options.length)).toEqual([1, 3]);
    await chooseTrackInput(page, trackRows(page).nth(0), IFACE, "Input 1");
    await waitForEngine(page);
    await chooseTrackInput(page, trackRows(page).nth(1), MIC, null);
    await waitForEngine(page);
    await trackRows(page).nth(0).locator(".track-arm").click();
    await page.waitForTimeout(1500);
    // Both choices read in full: nothing is cut to an ellipsis.
    for (const i of [0, 1]) {
      const fits = await trackRows(page).nth(i).locator(".track-input").evaluate((s) => s.scrollWidth <= s.clientWidth + 1);
      expect(fits, `track ${i + 1}'s input reads in full`).toBe(true);
    }
    const job = await jobState(page);
    log("stored inputs:", JSON.stringify(job.tracks.map((t) => t.input)));
    expect(job.tracks[0].input).toMatchObject({ device_label: IFACE, channels: [0] });
    expect(job.tracks[1].input.channels).toEqual([0]);
    await page.screenshot({ path: shot("track-rows.png"), clip: await rowClip(page) });
    // Survives a reload: the inputs come back from the server.
    await page.reload();
    await openStudio(page);
    await expect.poll(() => trackRows(page).nth(0).locator(".track-input option:checked").textContent(), { timeout: 15000 }).toBe(`${IFACE} · Input 1`);
    await expect(trackRows(page).nth(1).locator(".track-input option:checked")).toContainText(MIC);
  });
});

test.describe("real audio hardware: Settings > Recording", () => {
  test("the interface picker lists voice and instrument, and its meter moves", async ({ page }) => {
    await page.setViewportSize({ width: 1400, height: 900 });
    await openStudio(page);
    await page.locator("#settingsBtn").click();
    await page.locator('.settings-tab[data-tab="recording"]').click();
    const sel = page.locator("#setRecInterface");
    await expect.poll(async () => sel.locator("optgroup").count(), { timeout: 15000 }).toBe(2);
    const groups = await sel.evaluate((s) => [...s.querySelectorAll("optgroup")].map((g) => ({
      label: g.label, options: [...g.querySelectorAll("option")].map((o) => o.textContent),
    })));
    log("Settings input list:", JSON.stringify(groups));
    // No alias rows, no USB ids, each device once.
    const all = groups.flatMap((g) => g.options);
    expect(all.some((o) => /default|communications|\([0-9a-f]{4}:[0-9a-f]{4}\)/i.test(o))).toBe(false);
    expect(new Set(all).size).toBe(all.length);
    expect(groups[0].options.some((o) => o.includes(MIC))).toBe(true);
    expect(groups[1].options.some((o) => o.includes(IFACE))).toBe(true);

    const value = await sel.evaluate((s, dev) => [...s.options].find((o) => o.textContent.includes(dev))?.value, IFACE);
    await sel.selectOption(value);
    await page.waitForTimeout(2000);
    let best = 0;
    for (let i = 0; i < 15; i++) {
      // The second meter: the microphone's comes first.
      const tr = await page.locator(".rec-settings-meter .rec-meter-fill").nth(1).evaluate((e) => e.style.transform);
      best = Math.max(best, parseFloat(tr.replace(/[^0-9.]/g, "")) || 0);
      await page.waitForTimeout(100);
    }
    log("Settings meter on the interface, best of 15 samples:", best);
    expect(best).toBeGreaterThan(0.03);
    await page.locator(".library-editor").first().screenshot({ path: shot("settings-recording.png") }).catch(async () => {
      await page.screenshot({ path: shot("settings-recording.png") });
    });
  });
});
