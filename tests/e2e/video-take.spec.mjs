// Camera in the record flow, and "Make video" for a take that has one.
//
// Nothing is stubbed here. Chromium's fake devices stand in for the microphone
// and the camera (--use-fake-device-for-media-stream, with the prompt answered
// by --use-fake-ui-for-media-stream), and the takes API, the finish render and
// the video render are the real backend. So this is the whole chain: Record
// arms the take, the camera recorder starts on the same frame 0, camera parts
// go up in order, the sidecar carries the video block, Keep shows the take
// with Make video, the nudge is saved, ffmpeg renders, and the MP4 downloads.
//
// The render needs an ffmpeg with libx264 and AAC on the server. Without one
// the endpoint answers 422 and this test fails at "Download MP4"; that is the
// right outcome, not a flake.
//
// What this cannot check: whether picture and sound line up to the eye, real
// camera latency, framing, and the desktop webviews' permission prompts.

import { test, expect } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { openStudio, JOB_ID } from "./helpers.mjs";
import { trackRows, removeTracks, addTrack, waitForEngine, watchTakes as watchTakeRequests } from "./recording-helpers.mjs";

test.use({
  launchOptions: {
    args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--mute-audio"],
  },
  permissions: ["microphone", "camera"],
});

// Every track and take recorded here is removed again: the fixture job is
// shared with the rest of the suite, and an extra lane would change what
// other specs see.
test.afterEach(async ({ request }) => removeTracks(request));

const recBtn = (page) => page.locator("#t-rec");
const panel = (page) => page.locator("#videoPanel");

/** Watch what the page sends to the takes API, from the real requests. */
function watchTakes(page) {
  const log = { camera: [], audio: 0, finish: null, finishStatus: null, patches: [], renders: [] };
  page.on("request", (req) => {
    const url = new URL(req.url());
    const m = url.pathname.match(/\/takes\/(t[0-9a-f]{8})(\/.*)?$/);
    if (!m) return;
    const rest = m[2] || "";
    if (rest === "/parts/camera") {
      log.camera.push({
        seq: Number(url.searchParams.get("seq")),
        bytes: req.postDataBuffer()?.length ?? 0,
        type: req.headers()["content-type"],
      });
    } else if (rest === "/parts/audio") {
      log.audio++;
    } else if (rest === "/finish" && req.method() === "POST") {
      log.finish = req.postDataJSON();
    } else if (rest === "" && req.method() === "PATCH") {
      log.patches.push(req.postDataJSON());
    } else if (rest === "/render" && req.method() === "POST") {
      log.renders.push(req.postDataJSON());
    }
  });
  page.on("response", (res) => {
    if (/\/takes\/t[0-9a-f]{8}\/finish$/.test(new URL(res.url()).pathname)) log.finishStatus = res.status();
  });
  return log;
}

async function openRecordPanel(page) {
  // A take records into a recording track. Add it before the panel opens:
  // the click on "+ Track" is a click outside the panel, which closes it and
  // turns the camera off.
  if (!(await trackRows(page).count())) {
    await addTrack(page);
    await waitForEngine(page);
  }
  // The camera lives inside the recording panel, opened by the arrow beside Record.
  await page.locator("#t-rec-opts").click();
  await expect(page.locator("#recPanel")).toBeVisible();
  await expect(panel(page)).toBeVisible();
}

async function cameraOn(page) {
  const toggle = panel(page).locator(".vid-toggle");
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-pressed", "true");
  await page.waitForFunction(() => document.querySelector("#videoPanel video.vid-preview")?.videoWidth > 0);
}

/**
 * Press Record. The panel helper added the one recording track, and Record
 * arms the only track there is.
 */
async function pressRecord(page) {
  await recBtn(page).click();
  await expect(recBtn(page)).toHaveClass(/recording/);
}

async function recordAndStop(page, ms) {
  await pressRecord(page);
  await page.waitForFunction(async () => (await import("/js/state.js")).audioEngine?.isPlaying(), null, { timeout: 10000 });
  await page.waitForTimeout(ms);
  await recBtn(page).click();
  await expect(recBtn(page)).not.toHaveClass(/recording/);
}

test("camera on, record, keep, nudge, make the video and download it", async ({ page }) => {
  // Recording, the server-side take render and an x264 render of the whole
  // 6 second song, back to back. The suite's 45 s is for one UI action.
  test.setTimeout(120_000);
  const log = watchTakes(page);
  await openStudio(page);
  await openRecordPanel(page);

  // ── Camera on: live 720p preview, mirrored (the preview only) ──
  await cameraOn(page);
  const preview = panel(page).locator("video.vid-preview");
  expect(await preview.evaluate((v) => [v.videoWidth, v.videoHeight])).toEqual([1280, 720]);
  await expect(preview).toHaveClass(/vid-mirrored/);

  // ── Record: the camera records from the take's frame 0 until Stop ──
  await pressRecord(page);
  await expect(panel(page).locator(".vid-rec-badge")).toBeVisible();
  await expect(panel(page).locator(".vid-toggle")).toBeDisabled();
  await page.waitForFunction(async () => (await import("/js/state.js")).audioEngine?.isPlaying(), null, { timeout: 10000 });
  await page.waitForTimeout(3000);
  await recBtn(page).click();
  await expect(recBtn(page)).not.toHaveClass(/recording/);
  await expect.poll(() => log.finishStatus, { timeout: 30000 }).toBe(200);
  await expect(panel(page).locator(".vid-rec-badge")).toBeHidden();

  // Camera parts: raw bytes, in order from 0, each within the 4 MiB cap, all
  // sent before finish closed the take (finish succeeded, so the server took them).
  expect(log.camera.length).toBeGreaterThan(0);
  expect(log.camera.map((p) => p.seq)).toEqual(log.camera.map((_, i) => i));
  for (const p of log.camera) {
    expect(p.type).toBe("application/octet-stream");
    expect(p.bytes).toBeGreaterThan(0);
    expect(p.bytes).toBeLessThanOrEqual(4 * 1024 * 1024);
  }
  expect(log.audio).toBeGreaterThan(0);

  // The sidecar's video block, as the server validates it.
  const video = log.finish.video;
  expect(video).toBeTruthy();
  expect(video.mime).toMatch(/^video\/(mp4|webm)/);
  expect(video.parts).toBe(log.camera.length);
  expect([video.w, video.h]).toEqual([1280, 720]);
  expect(video.mirrorPreview).toBe(true);
  expect(video.cameraOffsetMs).toBe(0);
  expect(video.startedAtAudioCtx).toBeCloseTo(log.finish.timeline.ctx_time_at_frame0, 3);
  // The camera starts on the take's frame 0, within a few hundred ms. It can
  // read slightly negative: perfMsAtFrame0 maps the worklet's frame through
  // getOutputTimestamp, which runs behind the processing clock by the output
  // latency (measured here: -1.6 ms). The server accepts either sign.
  expect(video.firstFrameAtMs).toBeGreaterThan(-200);
  expect(video.firstFrameAtMs).toBeLessThan(1000);

  // ── Keep: the take now offers Make video ──
  await expect(page.locator("#recReview")).toBeVisible({ timeout: 20000 });
  await page.locator("#recKeep").click();
  const row = panel(page).locator(".vid-take");
  await expect(row).toHaveCount(1, { timeout: 15000 });
  const takeId = await row.getAttribute("data-take-id");
  expect(takeId).toMatch(/^t[0-9a-f]{8}$/);
  const job = await (await page.request.get(`/api/jobs/${JOB_ID}`)).json();
  const entry = job.takes.find((tk) => tk.id === takeId);
  expect(entry.has_camera).toBe(true);
  // Named with its track: "Track 1 · Take 1".
  await expect(row.locator(".vid-take-name")).toHaveText(`Track 1 \u00B7 ${entry.label}`);
  const cam = await page.request.get(entry.camera_url);
  expect(cam.ok()).toBe(true);
  expect(cam.headers()["content-type"]).toMatch(/^video\/(webm|mp4)/);

  // ── Nudge: one frame by button, then an exact value ──
  const offset = row.locator(".vid-offset");
  await expect(offset).toHaveValue("0");
  await row.locator(".vid-step").last().click();
  await expect(offset).toHaveValue("33");
  await offset.fill("47");

  // ── Length: the take's own span by default, the whole song on request ──
  const span = row.locator(".vid-span-select");
  await expect(span).toHaveValue("take");
  await expect(span.locator("option")).toHaveText(["Take only", "Whole song"]);

  // ── Make video: real render, progress, then Download MP4 ──
  await row.locator(".vid-make").click();
  await expect(row.locator(".vid-make")).toBeDisabled();
  await expect(row.locator(".vid-download")).toBeVisible({ timeout: 90000 });
  await expect(row.locator(".vid-progress")).toBeHidden();
  await expect(row.locator(".vid-make")).toBeEnabled();

  expect(log.patches).toEqual([{ camera_offset_ms: 47 }]);
  expect(log.renders.length).toBe(1);
  const req = log.renders[0];
  expect(req.camera_offset_ms).toBe(47);
  expect(req.span).toBe("take");
  expect(req.canvas).toBe("16:9");
  expect(req.mirror).toBe(false);
  // The backing as heard: the fixture stems, never the take itself; the take
  // rides on take_gain.
  expect(req.stems.length).toBeGreaterThan(0);
  expect(req.stems).not.toContain(takeId);
  // Nor its track, which plays this take: the take rides on take_gain once.
  expect(req.stems).not.toContain(entry.track_id);
  expect(req.gains.length).toBe(req.stems.length);
  expect(req.take_gain).toBeGreaterThan(0);
  const saved = await (await page.request.get(`/api/jobs/${JOB_ID}`)).json();
  expect(saved.takes.find((tk) => tk.id === takeId).camera_offset_ms).toBe(47);

  const [download] = await Promise.all([
    page.waitForEvent("download"),
    row.locator(".vid-download").click(),
  ]);
  expect(download.url()).toContain(`/takes/${takeId}/render.mp4`);
  // The server's name (song, then take). The page asks the desktop save
  // dialog for the same one; that branch is not driven here.
  expect(download.suggestedFilename()).toBe("E2E_Fixture_Track_Take_1.mp4");
  const bytes = await readFile(await download.path());
  // An MP4 (ftyp first) with the index up front (+faststart: moov before mdat).
  expect(bytes.subarray(4, 8).toString("latin1")).toBe("ftyp");
  const moov = bytes.indexOf("moov");
  const mdat = bytes.indexOf("mdat");
  expect(moov).toBeGreaterThan(0);
  expect(moov).toBeLessThan(mdat);

  // The download's own <a> click must not close the panel under the button.
  await expect(page.locator("#recPanel")).toBeVisible();

  // The whole song on request, remembered for the next video.
  await span.selectOption("song");
  await row.locator(".vid-make").click();
  await expect(row.locator(".vid-download")).toBeVisible({ timeout: 90000 });
  expect(log.renders.length).toBe(2);
  expect(log.renders[1].span).toBe("song");
  expect(await page.evaluate(() => localStorage.getItem("stemdeck:video:span"))).toBe("song");

  // Stop: a render can be stopped while it runs. The status polls are held
  // at "running" so the click lands mid-render whatever the machine's speed;
  // the stop itself goes to the real server.
  const statusUrl = new RegExp(`/takes/${takeId}/render$`);
  await page.route(statusUrl, (route) => (route.request().method() === "GET"
    ? route.fulfill({ json: { status: "running", progress: 0.4, error: null, url: null } })
    : route.continue()));
  const cancelled = page.waitForRequest((r) => r.url().endsWith(`/takes/${takeId}/render/cancel`) && r.method() === "POST");
  await row.locator(".vid-make").click();
  await expect(row.locator(".vid-stop")).toBeVisible();
  await row.locator(".vid-stop").click();
  await cancelled;
  await page.unroute(statusUrl);
  await expect(row.locator(".vid-state")).toHaveText("Stopped. No video was made.", { timeout: 15000 });
  await expect(row.locator(".vid-stop")).toBeHidden();
  await expect(row.locator(".vid-download")).toBeHidden();
  await expect(row.locator(".vid-make")).toBeEnabled();

  // ── Camera off releases the device ──
  await panel(page).locator(".vid-toggle").click();
  await expect(panel(page).locator(".vid-toggle")).toHaveAttribute("aria-pressed", "false");
  await expect(preview).toBeHidden();
});

test("closing the recording panel turns the camera off", async ({ page }) => {
  await openStudio(page);
  await openRecordPanel(page);
  await cameraOn(page);
  const live = () => page.evaluate(() => {
    const v = document.querySelector("#videoPanel video.vid-preview");
    return Boolean(v?.srcObject?.getVideoTracks().some((tr) => tr.readyState === "live"));
  });
  expect(await live()).toBe(true);
  await page.keyboard.press("Escape");
  await expect(page.locator("#recPanel")).toBeHidden();
  await expect.poll(live).toBe(false);
  await openRecordPanel(page);
  await expect(panel(page).locator(".vid-toggle")).toHaveAttribute("aria-pressed", "false");
});

// The camera's settings live in Settings > Recording: which camera, its
// size, and the preview mirror, with a live preview to check framing. The
// recording panel's camera reads the same stored choices.
test("Settings > Recording previews the camera, and its choices drive the take's camera", async ({ page }) => {
  await openStudio(page);
  await page.locator("#settingsBtn").click();
  await page.locator('.settings-tab[data-tab="recording"]').click();
  const preview = page.locator("#setRecPreview");
  const size = () => preview.evaluate((v) => [v.videoWidth, v.videoHeight]);
  const live = () => preview.evaluate((v) => Boolean(v.srcObject?.getVideoTracks().some((tr) => tr.readyState === "live")));

  // Live preview at the default 720p, mirrored.
  await expect.poll(size, { timeout: 10000 }).toEqual([1280, 720]);
  await expect(preview).toHaveClass(/vid-mirrored/);
  await expect.poll(() => page.locator("#setRecCamera option").count()).toBeGreaterThan(1);

  // 1080p and no mirror, stored where the recorder reads them.
  await page.locator("#setRecQuality").selectOption("1080");
  await expect.poll(size, { timeout: 10000 }).toEqual([1920, 1080]);
  await page.locator("label.settings-switch:has(#setRecMirror)").click();
  await expect(preview).not.toHaveClass(/vid-mirrored/);
  expect(await page.evaluate(() => [localStorage.getItem("stemdeck:video:quality"), localStorage.getItem("stemdeck:video:mirrorPreview")]))
    .toEqual(["1080", "0"]);

  // Leaving the tab stops the camera; coming back starts it again.
  await page.locator('.settings-tab[data-tab="general"]').click();
  await expect.poll(live).toBe(false);
  await page.locator('.settings-tab[data-tab="recording"]').click();
  await expect.poll(live, { timeout: 10000 }).toBe(true);
  // Closing Settings stops it too.
  await page.evaluate(() => { window.__setPreviewStream = document.querySelector("#setRecPreview").srcObject; });
  await page.locator(".settings-done").click();
  await expect.poll(() => page.evaluate(() =>
    window.__setPreviewStream.getVideoTracks().every((tr) => tr.readyState === "ended"))).toBe(true);

  // The panel's camera follows the stored choices: 1080p, not mirrored, and
  // the panel itself no longer carries those controls.
  await openRecordPanel(page);
  await expect(panel(page).locator("select, .vid-check")).toHaveCount(0);
  await cameraOn(page);
  const panelPreview = panel(page).locator("video.vid-preview");
  expect(await panelPreview.evaluate((v) => [v.videoWidth, v.videoHeight])).toEqual([1920, 1080]);
  await expect(panelPreview).not.toHaveClass(/vid-mirrored/);
});

test("with the camera off, a take sends no camera parts and offers no Make video", async ({ page }) => {
  test.setTimeout(60_000);
  const log = watchTakes(page);
  await openStudio(page);
  await openRecordPanel(page);
  await expect(panel(page).locator(".vid-toggle")).toHaveAttribute("aria-pressed", "false");

  await recordAndStop(page, 1500);
  await expect.poll(() => log.finishStatus, { timeout: 30000 }).toBe(200);
  expect(log.camera.length).toBe(0);
  expect(log.finish.video).toBeUndefined();

  await expect(page.locator("#recReview")).toBeVisible({ timeout: 20000 });
  await page.locator("#recKeep").click();
  await expect(page.locator("#recStatus")).toHaveText("Take kept.");
  const job = await (await page.request.get(`/api/jobs/${JOB_ID}`)).json();
  expect(job.takes.length).toBe(1);
  expect(job.takes[0].has_camera).toBe(false);
  await expect(panel(page).locator(".vid-take")).toHaveCount(0);
});

test("with two tracks armed, the camera films the first track's take only", async ({ page }) => {
  test.setTimeout(60_000);
  const log = watchTakeRequests(page);
  await openStudio(page);
  const first = await addTrack(page);
  await waitForEngine(page);
  await addTrack(page);
  await waitForEngine(page);
  // Both on the same stereo device: one channel each.
  for (const [i, label] of [[0, "Input 1"], [1, "Input 2"]]) {
    const sel = trackRows(page).nth(i).locator(".track-input");
    await expect.poll(() => sel.locator("option").count(), { timeout: 10000 }).toBeGreaterThan(2);
    await sel.selectOption(await sel.locator("option", { hasText: new RegExp(`^Fake Audio Input 1 · ${label}$`) }).getAttribute("value"));
    await trackRows(page).nth(i).locator(".track-arm").click();
  }
  await openRecordPanel(page);
  await cameraOn(page);
  await recordAndStop(page, 2000);
  await expect.poll(() => log.finishStatus.length, { timeout: 30000 }).toBe(2);
  expect(log.finishStatus).toEqual([200, 200]);
  const withVideo = log.finishes.filter((f) => f.body.video);
  expect(withVideo.length).toBe(1);
  const job = await (await page.request.get(`/api/jobs/${JOB_ID}`)).json();
  const filmed = job.takes.find((tk) => tk.id === withVideo[0].take);
  expect(filmed.track_id).toBe(first);
  expect(filmed.has_camera).toBe(true);
  // Camera parts all went to that take.
  expect(new Set(log.camera.map((c) => c.take))).toEqual(new Set([withVideo[0].take]));
});

