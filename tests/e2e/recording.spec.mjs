// Recording a take over the song (static/js/capture/).
//
// Chromium's fake capture device stands in for a microphone, so the whole
// browser side runs for real: permission, the capture graph in the playback
// AudioContext, the recorder worklet, the chunked part upload, the sidecar,
// and the take coming back as its recording track's audio, which mixes and
// exports like a stem. The takes and tracks API is the real backend
// (app/api/takes.py); every test removes what it recorded.
//
// Recording tracks themselves (adding, arming, inputs, several takes) are
// covered in recording-tracks.spec.mjs.
//
// What this cannot check: alignment by ear, real device latency, and the
// desktop webviews' permission prompts (no Tauri in the loop).
import { test, expect } from "@playwright/test";
import { openStudio, waitForClickTrack } from "./helpers.mjs";
import {
  recBtn, trackRows, removeTracks, jobState, addTrack, waitForEngine, pressRecord, recordForAWhile, watchTakes,
} from "./recording-helpers.mjs";

test.use({
  launchOptions: {
    args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--mute-audio"],
  },
  permissions: ["microphone"],
});

test.afterEach(async ({ request }) => removeTracks(request));

const lastFinish = (log) => log.finishes.at(-1)?.body ?? null;

test.describe("recording a take", () => {
  test("record, stop, keep: the take plays on its track, mixes and exports", async ({ page }) => {
    const log = watchTakes(page);
    await openStudio(page, { tauri: true });

    // The panel opens the input: a live meter needs it.
    await page.locator("#t-rec-opts").click();
    await expect(page.locator("#recPanel")).toBeVisible();
    await expect(page.locator("#recPermission")).toBeHidden();
    // The input and the latency test live in Settings now; the panel links there.
    await expect(page.locator("#recSettingsLink")).toBeVisible();
    await expect(page.locator("#recPanel select, #recPanel .rec-cal-progress")).toHaveCount(0);
    // Choose a lane to mute while recording.
    await page.locator('#recMuteList input[value="drums"]').check();
    await page.keyboard.press("Escape");

    await recordForAWhile(page);

    // Parts went up as raw float32, in order, from seq 0, under 4 MB.
    await expect.poll(() => log.finishStatus.at(-1), { timeout: 20000 }).toBe(200);
    const sc = lastFinish(log);
    const audio = log.parts;
    expect(audio.length).toBeGreaterThan(0);
    expect(audio.map((p) => p.seq)).toEqual(audio.map((_, i) => i));
    for (const p of audio) {
      expect(p.bytes).toBeLessThanOrEqual(4 * 1024 * 1024);
      expect(p.bytes % (4 * sc.capture.channels)).toBe(0);
    }

    // The sidecar: the take began before the song (it was armed first), at
    // the context rate, with the estimate labelled as such.
    expect(sc.v).toBe(1);
    expect([44100, 48000]).toContain(sc.capture.rate);
    expect([1, 2]).toContain(sc.capture.channels);
    expect(sc.timeline.tempo).toBe(1);
    expect(sc.timeline.first_sample_song_sec).toBeLessThanOrEqual(0);
    expect(sc.timeline.first_sample_song_sec).toBeGreaterThan(-1);
    expect(sc.latency.source).toBe("estimated");
    expect(sc.latency.user_nudge_ms).toBe(0);
    expect(sc.latency.round_trip_ms).toBeGreaterThan(0);
    const totalBytes = audio.reduce((s, p) => s + p.bytes, 0);
    const seconds = totalBytes / (4 * sc.capture.channels * sc.capture.rate);
    expect(seconds).toBeGreaterThan(2);
    expect(sc.timeline.end_song_sec - sc.timeline.first_sample_song_sec).toBeCloseTo(seconds, 2);

    // Review: the track already plays the take.
    await expect(page.locator("#recReview")).toBeVisible();
    await expect(page.locator("#recReviewText")).toContainText("Track 1: Take 1");
    const job = await jobState(page);
    const track = job.tracks[0];
    expect(track.active_take_id).toBe(job.takes[0].id);
    await expect(trackRows(page)).toHaveCount(1);
    await expect(trackRows(page).first().locator(".mx-name")).toHaveText("Track 1");
    await page.waitForFunction(
      async (id) => !!(await import("/js/state.js")).audioEngine?.getAnalyser(id),
      track.id,
      { timeout: 20000 },
    );
    // The mute for the take did not stick: the lanes reloaded from the saved mix.
    expect(await page.evaluate(async () => (await import("/js/state.js")).mixerState.drums.muted)).toBe(false);

    await page.locator("#recKeep").click();
    await expect(page.locator("#recReview")).toBeHidden();
    await expect(page.locator("#recStatus")).toHaveText("Take kept.");
    // The panel stays open after Keep; close it, as clicking away does, so
    // it is not over the lanes.
    await page.keyboard.press("Escape");
    await expect(page.locator("#recPanel")).toBeHidden();

    // Mute and solo work on the track lane like on a stem.
    const row = trackRows(page).first();
    await row.locator(".solo").click();
    expect(await page.evaluate(async (id) => (await import("/js/state.js")).mixerState[id].soloed, track.id)).toBe(true);
    await row.locator(".solo").click();
    await row.locator(".mute").click();
    expect(await page.evaluate(async (id) => (await import("/js/state.js")).mixerState[id].muted, track.id)).toBe(true);
    await row.locator(".mute").click();

    // Export: the track is a lane in the mixdown request.
    await page.locator("#t-export-btn").click();
    await page.locator("#t-export-mix").click();
    await page.evaluate(() => window.__e2e.choosePath());
    await expect.poll(() => page.evaluate(() => window.__e2e.callsFor("download_to_path").length)).toBe(1);
    const url = await page.evaluate(() => window.__e2e.callsFor("download_to_path")[0].args.url);
    expect(new URL(url).searchParams.get("stems").split(",")).toContain(track.id);
  });

  test("with a count-in, the take starts before the song and keeps the count", async ({ page }) => {
    const log = watchTakes(page);
    await openStudio(page);
    await addTrack(page);
    await waitForEngine(page);
    await waitForClickTrack(page);
    await page.locator("#t-metro-countin").selectOption("1");
    await page.evaluate(async () => {
      const st = await import("/js/state.js");
      window.__countIns = 0;
      const m = st.metronome;
      const handOver = m.playCountIn.bind(m);
      m.playCountIn = (clicks) => { window.__countIns++; return handOver(clicks); };
    });
    await recordForAWhile(page, 3500);
    await expect.poll(() => log.finishStatus.at(-1), { timeout: 20000 }).toBe(200);
    // The transport's own count-in ran, unchanged.
    expect(await page.evaluate(() => window.__countIns)).toBe(1);
    const sc = lastFinish(log);
    const tl = sc.timeline;
    expect(tl.count_in_sec).toBeGreaterThan(0.3);
    // Frame 0 sits one count-in (plus the round trip and the worklet's
    // latency, both small) before song time 0.
    const lat = (sc.latency.round_trip_ms + sc.latency.pipeline_latency_ms) / 1000;
    expect(tl.first_sample_song_sec).toBeCloseTo(-tl.count_in_sec - lat, 1);
  });

  test("the full-decode engine records and places a take too", async ({ page }) => {
    // Both engines expose getOutputMapping(); this runs the other one.
    await page.addInitScript(() => localStorage.setItem("stemdeck.audioEngine", "fulldecode"));
    const log = watchTakes(page);
    await openStudio(page);
    await recordForAWhile(page, 2000);
    await expect.poll(() => log.finishStatus.at(-1), { timeout: 20000 }).toBe(200);
    const sc = lastFinish(log);
    expect(sc.timeline.first_sample_song_sec).toBeLessThanOrEqual(0);
    expect(sc.timeline.first_sample_song_sec).toBeGreaterThan(-1);
    await expect(trackRows(page).first()).not.toHaveClass(/track-empty/, { timeout: 20000 });
  });

  test("discard deletes the take and the track is empty again", async ({ page }) => {
    const log = watchTakes(page);
    await openStudio(page);
    await recordForAWhile(page, 1500);
    await expect(page.locator("#recReview")).toBeVisible({ timeout: 20000 });
    await expect(trackRows(page).first()).not.toHaveClass(/track-empty/, { timeout: 20000 });
    await page.locator("#recDiscard").click();
    await expect.poll(() => log.deletes.length).toBe(1);
    await expect(page.locator("#recStatus")).toHaveText("Take discarded.");
    // The track stays, with nothing on it.
    await expect(trackRows(page)).toHaveCount(1);
    await expect(trackRows(page).first()).toHaveClass(/track-empty/, { timeout: 20000 });
    const job = await jobState(page);
    expect(job.takes.length).toBe(0);
    expect(job.tracks[0].active_take_id).toBeNull();
  });

  test("a nudge from the take's menu is sent to the server and the lane reloads", async ({ page }) => {
    const log = watchTakes(page);
    await openStudio(page);
    await recordForAWhile(page, 1500);
    await expect(trackRows(page).first()).not.toHaveClass(/track-empty/, { timeout: 20000 });
    await page.locator("#recKeep").click();
    await page.keyboard.press("Escape");
    await expect(page.locator("#recPanel")).toBeHidden();
    const takeId = (await jobState(page)).takes[0].id;

    // One take: its folder starts closed; the arrow opens it.
    await trackRows(page).first().locator(".take-folder-toggle").click();
    await page.locator(`.take-folder-row[data-take-id="${takeId}"] .take-folder-menu-btn`).click();
    const menu = page.locator(".take-menu");
    await expect(menu).toBeVisible();
    await menu.locator(".take-nudge-input").fill("12");
    const reloaded = page.waitForRequest((r) => r.url().includes(`/takes/${takeId}/audio.wav?n=12`), { timeout: 20000 });
    await menu.locator(".take-apply").click();
    await expect.poll(() => log.patches.length).toBe(1);
    expect(log.patches[0]).toEqual({ user_nudge_ms: 12 });
    // The reloaded lane asks for the re-rendered file, not the cached one.
    await reloaded;
  });

  test("Record refuses a tempo other than 1x and creates nothing", async ({ page }) => {
    const log = watchTakes(page);
    await openStudio(page);
    await addTrack(page);
    await waitForEngine(page);
    // Through the speed control (which saves it with the song's mix), as a user changes it.
    await page.evaluate(async () => (await import("/js/transport.js")).setSpeed(0.5));
    await recBtn(page).click();
    await expect(page.locator("#recStatus")).toHaveText(/normal speed/);
    expect(log.created.length).toBe(0);
    await expect(recBtn(page)).not.toHaveClass(/recording/);
  });

  test("a stop before the song starts saves nothing", async ({ page }) => {
    const log = watchTakes(page);
    await openStudio(page);
    await addTrack(page);
    await waitForEngine(page);
    // Hold the engine's clock: the take can never be placed against the song.
    await page.evaluate(async () => {
      const st = await import("/js/state.js");
      await st.audioEngine.ready;
      st.audioEngine.isClockReady = () => false;
    });
    // Stopped at once: too little audio to keep. A take that captured more
    // is kept at an estimated place (recording-no-peaks.spec.mjs), because
    // real audio is never thrown away.
    await pressRecord(page);
    await recBtn(page).click();
    await expect(page.locator("#recStatus")).toHaveText(/nothing was saved/, { timeout: 10000 });
    expect(log.finishes.length).toBe(0);
    await expect.poll(() => log.deletes.length).toBe(1);
  });
});

test.describe("recording without a microphone", () => {
  test("a refused microphone says so, in the panel", async ({ page }) => {
    await page.addInitScript(() => {
      navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException("denied", "NotAllowedError"));
    });
    const log = watchTakes(page);
    await openStudio(page);
    await addTrack(page);
    await waitForEngine(page);
    await recBtn(page).click();
    await expect(page.locator("#recPanel")).toBeVisible();
    await expect(page.locator("#recPermission .capture-reason")).toHaveAttribute("data-reason", "denied");
    await expect(page.locator("#recPermission")).toContainText("Microphone access was blocked");
    expect(log.created.length).toBe(0);
  });

  test("no device found says so", async ({ page }) => {
    await page.addInitScript(() => {
      navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException("none", "NotFoundError"));
    });
    await openStudio(page);
    await addTrack(page);
    await waitForEngine(page);
    await recBtn(page).click();
    await expect(page.locator("#recPermission .capture-reason")).toHaveAttribute("data-reason", "no-device");
    // And points at where an input is chosen.
    await page.locator("#recPermission .rec-link").click();
    await expect(page.locator('.settings-tab[data-tab="recording"]')).toHaveClass(/active/);
  });
});

// Settings > Recording: the default input, the latency test and the camera
// are set there, once, and the recorder reads the same stored choices.
test.describe("Settings, Recording tab", () => {
  const openTab = async (page) => {
    await page.locator("#settingsBtn").click();
    await page.locator('.settings-tab[data-tab="recording"]').click();
    await expect(page.locator('.settings-pane[data-pane="recording"]')).toBeVisible();
  };

  test("picks an input, runs the latency test, and a stored calibration reaches the take", async ({ page }) => {
    const log = watchTakes(page);
    await openStudio(page);
    await openTab(page);

    // Picking the audio interface stores it under the key new audio tracks
    // default to. The first option is None.
    const sel = page.locator("#setRecInterface");
    await expect.poll(() => sel.locator("option").count(), { timeout: 10000 }).toBeGreaterThan(1);
    const id = await sel.locator("option").nth(1).getAttribute("value");
    await sel.selectOption(id);
    expect(await page.evaluate(() => localStorage.getItem("stemdeck:capture:device:audioinput:instrument"))).toBe(id);
    await expect(page.locator("#setRecLatency")).toHaveText(/estimated/, { timeout: 10000 });

    // The latency test plays its clicks and finishes. The fake device hears
    // no loopback, so the outcome here is one of the three answers, not a
    // number: the measurement itself is pinned by tests/js/take-math.test.mjs.
    await page.locator("#setRecCalibrate").click();
    await expect(page.locator("#setRecCalStatus")).toHaveText(/Listening/);
    await expect(page.locator("#setRecCalStatus")).toHaveText(
      /Round trip|No clicks were heard|did not line up/, { timeout: 15000 },
    );
    await expect(page.locator("#setRecCalibrate")).toBeEnabled();

    // One source of truth: a calibration stored for this input and rate (as
    // a successful test stores it) is what Settings shows and what a take on
    // a track using that input uses.
    await page.evaluate(async (deviceId) => {
      const { createPlaybackContext } = await import("/js/audioContext.js");
      const ctx = createPlaybackContext(window.AudioContext);
      const rate = ctx.sampleRate;
      ctx.close();
      const s = await navigator.mediaDevices.getUserMedia({ audio: { deviceId: { exact: deviceId } } });
      const real = s.getAudioTracks()[0].getSettings().deviceId || deviceId;
      s.getTracks().forEach((t) => t.stop());
      localStorage.setItem(`stemdeck:capture:latency:${real}@${rate}`,
        JSON.stringify({ roundTripMs: 12.3, driftPpm: 0, rate, measuredAt: Date.now() }));
    }, id);
    await page.locator('.settings-tab[data-tab="general"]').click();
    await page.locator('.settings-tab[data-tab="recording"]').click();
    await expect(page.locator("#setRecLatency")).toHaveText(/12\.3 ms, calibrated/, { timeout: 10000 });
    await page.locator(".settings-done").click();

    // A new track starts on that input.
    await recordForAWhile(page, 1500);
    await expect.poll(() => log.finishStatus.at(-1), { timeout: 20000 }).toBe(200);
    const sc = lastFinish(log);
    expect(sc.latency.source).toBe("calibrated");
    expect(sc.latency.round_trip_ms).toBeCloseTo(12.3, 1);
    expect((await jobState(page)).tracks[0].input.device_id).toBe(id);
  });

  test("leaving the tab or closing Settings releases the microphone", async ({ page }) => {
    await page.addInitScript(() => {
      window.__liveMics = [];
      const real = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
      navigator.mediaDevices.getUserMedia = async (c) => {
        const s = await real(c);
        if (c?.audio) window.__liveMics.push(s);
        return s;
      };
    });
    await openStudio(page);
    const live = () => page.evaluate(() => window.__liveMics.filter((s) => s.getTracks().some((t) => t.readyState === "live")).length);
    await openTab(page);
    await expect.poll(live, { timeout: 10000 }).toBe(1);
    await page.locator('.settings-tab[data-tab="export"]').click();
    await expect.poll(live).toBe(0);
    await page.locator('.settings-tab[data-tab="recording"]').click();
    await expect.poll(live, { timeout: 10000 }).toBe(1);
    await page.keyboard.press("Escape");
    await expect.poll(live).toBe(0);
  });
});
