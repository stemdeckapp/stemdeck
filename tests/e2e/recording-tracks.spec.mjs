// Recording tracks, DAW style (static/js/capture/takeLanes.js).
//
// "+ Track" adds an empty track below the stems; each track has an arm
// button, an input (one channel or a stereo pair of a device), a menu, and a
// folder of its takes (take-folders.spec.mjs covers the folder itself). Record records into every armed track; each recording is a new
// take that becomes the track's active take, and earlier takes stay
// selectable. Export sends the track, which the server resolves to its active
// take.
//
// Chromium's fake capture devices ("Fake Audio Input 1" and 2) are stereo and
// have no microphone in their name, so they are listed as instruments:
// "Fake Audio Input 1 · Input 1", "· Input 2" and "· Input 1+2". A device is
// added to the device list where a test needs another one.
import { test, expect } from "@playwright/test";
import { openStudio, JOB_ID } from "./helpers.mjs";
import {
  recBtn, trackRows, removeTracks, jobState, addTrack, waitForEngine, recordForAWhile, watchTakes,
} from "./recording-helpers.mjs";

test.use({
  launchOptions: {
    args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--mute-audio"],
  },
  permissions: ["microphone"],
});

test.afterEach(async ({ request }) => removeTracks(request));

/** Every visible mixer row and waveform lane, top to bottom. */
async function lanes(page) {
  return page.evaluate(() => {
    const visible = (sel) => [...document.querySelectorAll(sel)]
      .filter((e) => !e.classList.contains("hidden") && e.getBoundingClientRect().height > 0)
      .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
    const top = (e) => Math.round(e.getBoundingClientRect().top);
    const mix = visible(".mixer-column .lane-header[data-stem]");
    const wave = visible(".stem-waveform-row[data-stem]");
    return {
      mixNames: mix.map((e) => e.dataset.stem),
      waveNames: wave.map((e) => e.dataset.stem),
      mixTops: mix.map(top),
      waveTops: wave.map(top),
    };
  });
}

test("+ Track adds an empty track below the stems, aligned with its lane", async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 1000 });
  await openStudio(page);
  await expect(page.locator("#addTrackBtn")).toBeEnabled();
  const id1 = await addTrack(page);
  await waitForEngine(page);
  const id2 = await addTrack(page);
  await waitForEngine(page);
  expect(id1).toMatch(/^k[0-9a-f]{8}$/);

  await expect(trackRows(page).locator(".mx-name")).toHaveText(["Track 1", "Track 2"]);
  await expect(trackRows(page).first()).toHaveClass(/track-empty/);
  const job = await jobState(page);
  expect(job.tracks.map((t) => t.name)).toEqual(["Track 1", "Track 2"]);

  // Below the stems, in track order, and row for row with the waveform lanes.
  await expect.poll(async () => (await lanes(page)).waveNames.length, { timeout: 10000 }).toBe(6);
  const g = await lanes(page);
  expect(g.mixNames).toEqual(["vocals", "drums", "bass", "other", id1, id2]);
  expect(g.waveNames).toEqual(g.mixNames);
  for (let i = 0; i < g.mixTops.length; i++) {
    expect(Math.abs(g.mixTops[i] - g.waveTops[i]), `row ${i}`).toBeLessThanOrEqual(1);
  }

  // Rename from the track menu.
  await trackRows(page).nth(1).locator(".take-menu-btn").click();
  await page.locator(".take-menu .take-name-input").fill("Guitar");
  await page.locator(".take-menu .take-apply").click();
  await expect(trackRows(page).nth(1).locator(".mx-name")).toHaveText("Guitar", { timeout: 20000 });
});

test("the input selector lists each channel and the stereo pair", async ({ page }) => {
  await openStudio(page);
  await addTrack(page);
  await waitForEngine(page);
  const select = trackRows(page).first().locator(".track-input");
  const dev = "Fake Audio Input 1";
  await expect.poll(() => select.locator("option").allTextContents(), { timeout: 10000 })
    .toEqual(expect.arrayContaining([`${dev} · Input 1`, `${dev} · Input 2`, `${dev} · Input 1+2`]));
  // Grouped by kind; no alias row ("Default", "Communications") and no repeat.
  const texts = await select.locator("option").allTextContents();
  expect(new Set(texts).size).toBe(texts.length);
  expect(texts.some((x) => /default|communications/i.test(x))).toBe(false);
  await expect(select.locator("optgroup")).toHaveAttribute("label", "Instrument (audio interface)");
  // A new track records one input, the first: a one-cable source recorded
  // as the pair played on one speaker. The pair is chosen by hand.
  await expect(select.locator("option:checked")).toHaveText(`${dev} · Input 1`);

  // Choosing a single channel is stored with the track and records mono.
  const log = watchTakes(page);
  const value = await select.locator("option", { hasText: /^Fake Audio Input 1 · Input 2$/ }).getAttribute("value");
  await select.selectOption(value);
  await expect.poll(async () => (await jobState(page)).tracks[0].input?.channels).toEqual([1]);
  await recordForAWhile(page, 1500);
  await expect.poll(() => log.finishStatus.at(-1), { timeout: 20000 }).toBe(200);
  expect(log.finishes.at(-1).body.capture.channels).toBe(1);
});

test("a second take becomes active, the first stays selectable, export sends the active one", async ({ page }) => {
  test.setTimeout(90_000);
  const log = watchTakes(page);
  await openStudio(page, { tauri: true });
  const trackId = await addTrack(page);
  await waitForEngine(page);

  await recordForAWhile(page, 1500);
  await expect.poll(() => log.finishStatus.length, { timeout: 20000 }).toBe(1);
  await page.locator("#recKeep").click();
  await page.keyboard.press("Escape");
  await waitForEngine(page);
  // Longer than the first, so the two takes cannot render to the same bytes:
  // the fake device plays the same beep each time, and two takes of one
  // length from one start point can come out identical.
  await recordForAWhile(page, 3500);
  await expect.poll(() => log.finishStatus.length, { timeout: 20000 }).toBe(2);
  await page.locator("#recKeep").click();
  await page.keyboard.press("Escape");

  // Both takes went to the same track; the second is active.
  expect(log.created.map((c) => c.track_id)).toEqual([trackId, trackId]);
  let job = await jobState(page);
  const [take1, take2] = job.takes.map((t) => t.id);
  expect(job.tracks[0].active_take_id).toBe(take2);

  // The track's take folder lists both, newest on top, the second playing;
  // playing the first makes it the track's take.
  await waitForEngine(page);
  const takeRows = page.locator(`.take-folder-row[data-folder-of="${trackId}"]`);
  await expect(takeRows).toHaveCount(2, { timeout: 15000 });
  await expect(takeRows.locator(".take-folder-name")).toHaveText(["Take 2", "Take 1"]);
  await expect(takeRows.nth(0).locator(".take-play")).toHaveAttribute("aria-pressed", "true");
  const reloaded = page.waitForRequest((r) => r.url().includes(`/takes/${take1}/audio.wav`), { timeout: 20000 });
  await takeRows.nth(1).locator(".take-play").click();
  await reloaded;
  job = await jobState(page);
  expect(job.tracks[0].active_take_id).toBe(take1);

  // Export names the track, not a take; the server mixes the active take.
  await waitForEngine(page);
  await page.locator("#t-export-btn").click();
  await page.locator("#t-export-mix").click();
  await page.evaluate(() => window.__e2e.choosePath());
  await expect.poll(() => page.evaluate(() => window.__e2e.callsFor("download_to_path").length)).toBe(1);
  const url = new URL(await page.evaluate(() => window.__e2e.callsFor("download_to_path")[0].args.url));
  const stems = url.searchParams.get("stems").split(",");
  expect(stems).toContain(trackId);
  expect(stems).not.toContain(take1);
  expect(stems).not.toContain(take2);
  // What the track lane renders is the active take, and only that one.
  const mix = async (lane) => (await page.request.get(`/api/jobs/${JOB_ID}/mixdown.wav?stems=${lane}&gains=1`)).body();
  const viaTrack = await mix(trackId);
  expect(viaTrack.equals(await mix(take1))).toBe(true);
  expect(viaTrack.equals(await mix(take2))).toBe(false);
});

test("tracks on two different devices cannot be armed together", async ({ page }) => {
  // A microphone next to Chromium's fake devices.
  await page.addInitScript(() => {
    const real = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
    navigator.mediaDevices.enumerateDevices = async () => {
      const list = await real();
      const extra = {
        deviceId: "e2e-second-device", groupId: "e2e", kind: "audioinput", label: "Microphone (E2E Webcam) (046d:0893)",
        getCapabilities: () => ({ channelCount: { min: 1, max: 2 } }),
        toJSON() { return this; },
      };
      return [...list, extra];
    };
    // It opens as Chromium's fake device: arming opens the track's input.
    const gum = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = (c) => {
      if (c?.audio?.deviceId?.exact === "e2e-second-device") {
        const { deviceId: _d, ...rest } = c.audio;
        return gum({ ...c, audio: rest });
      }
      return gum(c);
    };
  });
  await openStudio(page);
  await addTrack(page);
  await waitForEngine(page);
  await addTrack(page);
  await waitForEngine(page);
  const [row1, row2] = [trackRows(page).nth(0), trackRows(page).nth(1)];

  // Track 2 records from the microphone: a voice input, one option, named
  // without the "Microphone (...)" wrapper and the USB id.
  const second = row2.locator('.track-input optgroup[label="Voice (microphone)"] option');
  await expect(second).toHaveText(["E2E Webcam"], { timeout: 10000 });
  await row2.locator(".track-input").selectOption(await second.first().getAttribute("value"));

  // Same device: both stay armed. Another device: the first is disarmed, and
  // the note says why.
  await row1.locator(".track-arm").click();
  await expect(row1.locator(".track-arm")).toHaveAttribute("aria-pressed", "true");
  await row2.locator(".track-arm").click();
  await expect(row2.locator(".track-arm")).toHaveAttribute("aria-pressed", "true");
  await expect(row1.locator(".track-arm")).toHaveAttribute("aria-pressed", "false");
  await expect(page.locator("#trackNote")).toContainText("same input device");

  // Back to the first device: both can be armed.
  const firstDev = await row2.locator(".track-input option", { hasText: /^Fake Audio Input 1 · Input 1\+2$/ }).getAttribute("value");
  await row2.locator(".track-input").selectOption(firstDev);
  await row1.locator(".track-arm").click();
  await expect(row1.locator(".track-arm")).toHaveAttribute("aria-pressed", "true");
  await expect(row2.locator(".track-arm")).toHaveAttribute("aria-pressed", "true");
});

test("two armed tracks on one device record together, one take each", async ({ page }) => {
  const log = watchTakes(page);
  await openStudio(page);
  const a = await addTrack(page);
  await waitForEngine(page);
  const b = await addTrack(page);
  await waitForEngine(page);
  // Track 1 on input 1, track 2 on input 2 of the same stereo device.
  for (const [i, label] of [[0, "Input 1"], [1, "Input 2"]]) {
    const sel = trackRows(page).nth(i).locator(".track-input");
    await expect.poll(() => sel.locator("option").count(), { timeout: 10000 }).toBeGreaterThan(2);
    const value = await sel.locator("option", { hasText: new RegExp(`^Fake Audio Input 1 · ${label}$`) }).getAttribute("value");
    await sel.selectOption(value);
  }
  await trackRows(page).nth(0).locator(".track-arm").click();
  await trackRows(page).nth(1).locator(".track-arm").click();

  await recBtn(page).click();
  await expect(recBtn(page)).toHaveClass(/recording/);
  await page.waitForTimeout(1500);
  await recBtn(page).click();
  await expect.poll(() => log.finishStatus.length, { timeout: 20000 }).toBe(2);
  expect(log.finishStatus).toEqual([200, 200]);
  expect(log.created.map((c) => c.track_id).sort()).toEqual([a, b].sort());
  for (const f of log.finishes) expect(f.body.capture.channels).toBe(1);
  await expect(page.locator("#recReviewText")).toContainText("new takes are saved");
  const job = await jobState(page);
  expect(job.tracks.every((t) => t.active_take_id)).toBe(true);
});

test("Record with no track offers to add one; with one track it arms it", async ({ page }) => {
  await openStudio(page);
  await recBtn(page).click();
  await expect(page.locator("#recStatus")).toContainText("Add a recording track");
  await expect(recBtn(page)).not.toHaveClass(/recording/);
  await page.locator("#recStatus .rec-link").click();
  await expect(trackRows(page)).toHaveCount(1, { timeout: 20000 });
  await waitForEngine(page);
  await recBtn(page).click();
  await expect(recBtn(page)).toHaveClass(/recording/, { timeout: 10000 });
  await expect(trackRows(page).first().locator(".track-arm")).toHaveAttribute("aria-pressed", "true");
  await recBtn(page).click();
});

test("Record with several tracks and none armed asks to arm one", async ({ page }) => {
  const log = watchTakes(page);
  await openStudio(page);
  await addTrack(page);
  await waitForEngine(page);
  await addTrack(page);
  await waitForEngine(page);
  await recBtn(page).click();
  await expect(page.locator("#recStatus")).toContainText("Arm the tracks to record into");
  expect(log.created.length).toBe(0);
});

test("delete track removes it and its takes", async ({ page }) => {
  await openStudio(page);
  await recordForAWhile(page, 1200);
  await expect(page.locator("#recReview")).toBeVisible({ timeout: 20000 });
  await page.locator("#recKeep").click();
  await page.keyboard.press("Escape");
  await waitForEngine(page);
  await trackRows(page).first().locator(".take-menu-btn").click();
  await page.locator(".take-menu .take-delete-track").click();
  await page.locator(".take-menu .take-delete-track").click();
  await expect(trackRows(page)).toHaveCount(0, { timeout: 20000 });
  const job = await jobState(page);
  expect(job.tracks.length).toBe(0);
  expect(job.takes.length).toBe(0);
});

test("arming opens the input: the armed track's meter shows it, disarming closes it", async ({ page }) => {
  await page.addInitScript(() => {
    // Count live microphone streams, to see the input close again.
    window.__liveMics = 0;
    const gum = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async (c) => {
      const s = await gum(c);
      for (const tr of s.getAudioTracks()) {
        window.__liveMics++;
        const stop = tr.stop.bind(tr);
        tr.stop = () => { if (tr.readyState === "live") window.__liveMics--; stop(); };
      }
      return s;
    };
  });
  await openStudio(page);
  await addTrack(page);
  await waitForEngine(page);
  const row = trackRows(page).first();
  const vuIn = () => row.locator(".lane-vu").evaluate((e) => parseFloat(e.style.getPropertyValue("--vu-in")) || 0);

  await row.locator(".track-arm").click();
  await expect(row).toHaveClass(/armed/);
  // Chromium's fake device beeps; the meter catches a beep within a second or two.
  await expect.poll(async () => {
    let best = 0;
    for (let i = 0; i < 10; i++) { best = Math.max(best, await vuIn()); await page.waitForTimeout(50); }
    return best;
  }, { timeout: 10000 }).toBeGreaterThan(0);
  expect(await page.evaluate(() => window.__liveMics)).toBe(1);

  await row.locator(".track-arm").click();
  await expect(row).not.toHaveClass(/armed/);
  await expect.poll(() => page.evaluate(() => window.__liveMics)).toBe(0);
  await expect.poll(vuIn).toBe(0);
});

test("a track whose device is not connected says so, and Record refuses it", async ({ page }) => {
  const log = watchTakes(page);
  await openStudio(page);
  const id = await addTrack(page);
  await page.request.patch(`/api/jobs/${JOB_ID}/tracks/${id}`, {
    data: { input: { device_id: "unplugged-id", device_label: "Scarlett 2i2", channels: [0] } },
  });
  await page.reload();
  await openStudio(page);
  const select = trackRows(page).first().locator(".track-input");
  await expect(select.locator("option:checked")).toHaveText("Scarlett 2i2 (not connected)", { timeout: 15000 });
  await expect(select).toHaveClass(/missing/);
  await waitForEngine(page);
  await recBtn(page).click();
  await expect(page.locator("#recStatus, #trackNote")).toContainText(["not connected"], { timeout: 10000 });
  await expect(recBtn(page)).not.toHaveClass(/recording/);
  expect(log.created.length).toBe(0);

  // Choosing a connected input clears it.
  const value = await select.locator("option", { hasText: /^Fake Audio Input 1 · Input 1$/ }).getAttribute("value");
  await select.selectOption(value);
  await expect.poll(async () => (await jobState(page)).tracks[0].input?.device_label).toBe("Fake Audio Input 1");
});

test("a track keeps its input when the device id changes (another browser), by name", async ({ page }) => {
  await openStudio(page);
  const id = await addTrack(page);
  await page.request.patch(`/api/jobs/${JOB_ID}/tracks/${id}`, {
    data: { input: { device_id: "id-from-another-browser", device_label: "Fake Audio Input 2", channels: [1] } },
  });
  await page.reload();
  await openStudio(page);
  await expect(trackRows(page).first().locator(".track-input option:checked"))
    .toHaveText("Fake Audio Input 2 · Input 2", { timeout: 15000 });
});

test("Settings > Recording lists each input once, by kind, without the default aliases", async ({ page }) => {
  await openStudio(page);
  await page.locator("#settingsBtn").click();
  await page.locator('.settings-tab[data-tab="recording"]').click();
  // Two pickers, the microphone and the audio interface: each offers None,
  // then every input once.
  for (const id of ["#setRecMic", "#setRecInterface"]) {
    const sel = page.locator(id);
    await expect.poll(() => sel.locator("option").allTextContents(), { timeout: 10000 })
      .toEqual(["None", "Fake Audio Input 1", "Fake Audio Input 2"]);
    await expect(sel.locator("optgroup")).toHaveAttribute("label", "Instrument (audio interface)");
  }
});
