// Camera to sound sync of a take, measured in the rendered MP4.
//
// Chromium's file-based fake devices stand in for the camera and the
// microphone, with a test signal written here:
//   - camera (--use-file-for-fake-video-capture): a Y4M of 60 frames at
//     30 fps, frame 0 full white and the rest dark grey. Chromium plays the
//     file in a loop from its first frame when the device opens, and shows
//     frame 0 twice at every wrap, so one loop lasts 61 frames (2.033 s);
//   - microphone (--use-file-for-fake-audio-capture): a WAV whose loop is
//     also 61/30 s, with a 40 ms 1 kHz burst at its start. It loops too, from
//     when the device opens.
// The two devices open at different moments, so the flash and the burst are
// some unknown distance apart. That distance is taken from Chromium's own
// MediaRecorder over the same two tracks (it places audio and video by their
// capture timestamps): the reference. The take then goes through the real
// chain (camera recorder, part upload, finish, server render), and the
// flash-to-burst distance in the rendered MP4 must match the reference.
//
// Measured on the maintainer's Windows machine (0.21.0 overnight log, task
// C): the camera is placed within -4.5 to +2.4 ms of the reference over 9
// takes in three codecs, and the 30 fps video grid adds at most half a frame
// (16.7 ms). The bound below, 40 ms, catches what has gone wrong before (a
// wrong sign, a latency counted twice, the recorder's start event standing
// in for the first frame), not one frame of drift.
//
// Needs ffmpeg with libx264 on the server (as video-take.spec.mjs does) and
// an ffmpeg here to read the files back: STEMDECK_FFMPEG_DIR, or ffmpeg on
// PATH. Skipped without one.

import { test, expect } from "@playwright/test";
import { mkdirSync, writeFileSync, existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { openStudio, JOB_ID } from "./helpers.mjs";
import { trackRows, removeTracks, addTrack, waitForEngine } from "./recording-helpers.mjs";

const W = 320;
const H = 180;
const FPS = 30;
const LOOP_FRAMES = 60;
const RATE = 48000;
const LOOP_SAMPLES = (61 * RATE) / FPS; // 97600: Chromium shows frame 0 twice per loop

const dir = join(tmpdir(), `stemdeck-video-sync-${process.pid}`);
const y4mPath = join(dir, "sync.y4m");
const wavPath = join(dir, "sync.wav");

function writeMedia() {
  mkdirSync(dir, { recursive: true });
  if (!existsSync(y4mPath)) {
    const header = Buffer.from(`YUV4MPEG2 W${W} H${H} F${FPS}:1 Ip A1:1 C420jpeg\n`);
    const ySize = W * H;
    const cSize = (W / 2) * (H / 2);
    const frames = [header];
    for (let i = 0; i < LOOP_FRAMES; i++) {
      frames.push(Buffer.from("FRAME\n"));
      frames.push(Buffer.alloc(ySize, i === 0 ? 235 : 40));
      frames.push(Buffer.alloc(cSize * 2, 128));
    }
    writeFileSync(y4mPath, Buffer.concat(frames));
  }
  if (!existsSync(wavPath)) {
    const data = Buffer.alloc(LOOP_SAMPLES * 4);
    const burst = Math.round(0.04 * RATE);
    for (let i = 0; i < burst; i++) {
      const v = Math.round(0.5 * Math.sin((2 * Math.PI * 1000 * i) / RATE) * 32767);
      data.writeInt16LE(v, i * 4);
      data.writeInt16LE(v, i * 4 + 2);
    }
    const h = Buffer.alloc(44);
    h.write("RIFF", 0);
    h.writeUInt32LE(36 + data.length, 4);
    h.write("WAVEfmt ", 8);
    h.writeUInt32LE(16, 16);
    h.writeUInt16LE(1, 20); // PCM
    h.writeUInt16LE(2, 22);
    h.writeUInt32LE(RATE, 24);
    h.writeUInt32LE(RATE * 4, 28);
    h.writeUInt16LE(4, 32);
    h.writeUInt16LE(16, 34);
    h.write("data", 36);
    h.writeUInt32LE(data.length, 40);
    writeFileSync(wavPath, Buffer.concat([h, data]));
  }
}
writeMedia();

const ffDir = process.env.STEMDECK_FFMPEG_DIR;
const FFMPEG = ffDir ? join(ffDir, process.platform === "win32" ? "ffmpeg.exe" : "ffmpeg") : "ffmpeg";
const haveFfmpeg = spawnSync(FFMPEG, ["-version"], { encoding: "utf8" }).status === 0;

test.use({
  launchOptions: {
    args: [
      "--use-fake-device-for-media-stream",
      "--use-fake-ui-for-media-stream",
      `--use-file-for-fake-video-capture=${y4mPath}`,
      `--use-file-for-fake-audio-capture=${wavPath}`,
      "--mute-audio",
    ],
  },
  permissions: ["microphone", "camera"],
});

test.afterEach(async ({ request }) => removeTracks(request));

/** Flash onsets in seconds: first bright frame of each flash, by its timestamp. */
function flashes(file) {
  const r = spawnSync(FFMPEG, [
    "-v", "info", "-nostdin", "-i", file, "-map", "0:v:0", "-fps_mode", "passthrough",
    "-vf", "scale=16:9,format=gray,showinfo", "-f", "rawvideo", "pipe:1",
  ], { maxBuffer: 1 << 28 });
  if (r.status !== 0) throw new Error(`ffmpeg failed on ${file}: ${r.stderr}`);
  const times = [...String(r.stderr).matchAll(/pts_time:\s*([-\d.]+)/g)].map((m) => Number(m[1]));
  const px = 16 * 9;
  const out = [];
  let prev = false;
  for (let k = 0; k < times.length; k++) {
    let sum = 0;
    for (let i = 0; i < px; i++) sum += r.stdout[k * px + i];
    const bright = sum / px > 150;
    if (bright && !prev) out.push(times[k]);
    prev = bright;
  }
  return out;
}

/** Burst onsets as sample indexes at `rate`, skipping one cut by the file start. */
function bursts(file, rate) {
  const r = spawnSync(FFMPEG, [
    "-v", "error", "-nostdin", "-i", file, "-map", "0:a:0", "-ac", "1", "-ar", String(rate), "-f", "f32le", "pipe:1",
  ], { maxBuffer: 1 << 28 });
  if (r.status !== 0) throw new Error(`ffmpeg failed on ${file}: ${r.stderr}`);
  const x = new Float32Array(r.stdout.buffer, r.stdout.byteOffset, r.stdout.length / 4);
  const out = [];
  let last = -Infinity;
  for (let i = 0; i < x.length; i++) {
    if (Math.abs(x[i]) > 0.08) {
      if (i - last > 0.5 * rate && i > 0.1 * rate) out.push(i);
      last = i;
    }
  }
  return out;
}

/** Mean of (flash - nearest burst) in ms over every flash with a burst within 0.5 s. */
function meanOffsetMs(flashSec, burstSec) {
  const d = [];
  for (const f of flashSec) {
    let best = null;
    for (const b of burstSec) if (best === null || Math.abs(b - f) < Math.abs(best - f)) best = b;
    if (best !== null && Math.abs(best - f) < 0.5) d.push((f - best) * 1000);
  }
  expect(d.length, "flash and burst pairs").toBeGreaterThan(0);
  return { mean: d.reduce((a, b) => a + b, 0) / d.length, n: d.length, all: d };
}

async function render(page, takeId, body, outFile) {
  const res = await page.request.post(`/api/jobs/${JOB_ID}/takes/${takeId}/render`, { data: body });
  expect(res.status(), await res.text()).toBe(202);
  await expect.poll(async () => (await (await page.request.get(`/api/jobs/${JOB_ID}/takes/${takeId}/render`)).json()).status,
    { timeout: 60000 }).toBe("done");
  const mp4 = await page.request.get(`/api/jobs/${JOB_ID}/takes/${takeId}/render.mp4`);
  writeFileSync(outFile, await mp4.body());
}

test("the camera and the sound of a take line up in the rendered video", async ({ page }) => {
  test.skip(!haveFfmpeg, "needs ffmpeg here to read the rendered file (STEMDECK_FFMPEG_DIR or PATH)");
  test.setTimeout(150_000);
  await page.addInitScript(() => {
    window.__gum = [];
    const orig = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async (c) => {
      const s = await orig(c);
      window.__gum.push(s);
      return s;
    };
  });
  let finish = null;
  page.on("request", (req) => {
    if (/\/takes\/t[0-9a-f]{8}\/finish$/.test(new URL(req.url()).pathname)) finish = req.postDataJSON();
  });

  await openStudio(page);
  if (!(await trackRows(page).count())) {
    await addTrack(page);
    await waitForEngine(page);
  }
  await page.locator("#t-rec-opts").click();
  await page.locator("#videoPanel .vid-toggle").click();
  await page.waitForFunction(() => document.querySelector("#videoPanel video.vid-preview")?.videoWidth > 0);

  await page.locator("#t-rec").click();
  await page.waitForFunction(async () => (await import("/js/state.js")).audioEngine?.isPlaying(), null, { timeout: 10000 });
  await page.waitForFunction(() => window.__gum.some((s) => s.getAudioTracks()[0]?.readyState === "live"));
  // The reference: Chromium's own recorder over the camera and microphone
  // tracks the take is using.
  await page.evaluate(() => {
    const cam = window.__gum.find((s) => s.getVideoTracks()[0]?.readyState === "live").getVideoTracks()[0];
    const mic = [...window.__gum].reverse().find((s) => s.getAudioTracks()[0]?.readyState === "live").getAudioTracks()[0];
    window.__refChunks = [];
    const r = new MediaRecorder(new MediaStream([cam, mic]), { mimeType: "video/webm;codecs=vp8,opus" });
    r.ondataavailable = (e) => window.__refChunks.push(e.data);
    r.start(500);
    window.__ref = r;
  });
  await page.waitForTimeout(4300);
  const refB64 = await page.evaluate(async () => {
    const stopped = new Promise((r) => window.__ref.addEventListener("stop", r, { once: true }));
    window.__ref.stop();
    await stopped;
    const buf = new Uint8Array(await new Blob(window.__refChunks).arrayBuffer());
    let s = "";
    for (let i = 0; i < buf.length; i += 0x8000) s += String.fromCharCode(...buf.subarray(i, i + 0x8000));
    return btoa(s);
  });
  await page.locator("#t-rec").click();
  await expect(page.locator("#recReview")).toBeVisible({ timeout: 30000 });
  await page.locator("#recKeep").click();
  expect(finish?.video, "the take has a camera block").toBeTruthy();
  const job = await (await page.request.get(`/api/jobs/${JOB_ID}`)).json();
  const take = job.takes.find((tk) => tk.has_camera);
  expect(take).toBeTruthy();

  const refFile = join(dir, `ref-${take.id}.webm`);
  writeFileSync(refFile, Buffer.from(refB64, "base64"));
  const reference = meanOffsetMs(flashes(refFile), bursts(refFile, RATE).map((i) => i / RATE));

  // The take's own sound only (no backing), so the bursts stand alone.
  const r0 = join(dir, `render-${take.id}-0.mp4`);
  const r120 = join(dir, `render-${take.id}-120.mp4`);
  await render(page, take.id, { stems: [], gains: [], camera_offset_ms: 0 }, r0);
  const at0 = meanOffsetMs(flashes(r0), bursts(r0, 44100).map((i) => i / 44100));
  console.log(`[video-sync] reference ${reference.mean.toFixed(1)} ms (${reference.n}), render ${at0.mean.toFixed(1)} ms (${at0.n})`);
  expect(Math.abs(at0.mean - reference.mean)).toBeLessThan(40);

  // +120 ms means the camera was late: the picture moves 120 ms earlier
  // (to within one frame: both renders round to the 30 fps grid).
  await render(page, take.id, { stems: [], gains: [], camera_offset_ms: 120 }, r120);
  const at120 = meanOffsetMs(flashes(r120), bursts(r120, 44100).map((i) => i / 44100));
  const shift = at120.mean - at0.mean;
  console.log(`[video-sync] +120 ms offset moved the picture by ${shift.toFixed(1)} ms`);
  expect(shift).toBeGreaterThan(-120 - 34);
  expect(shift).toBeLessThan(-120 + 34);

  // The take's sound in the video is the playable take to the sample: same
  // detector on both, the video starting at the take span's start.
  const tl = finish.timeline;
  const spanStart = Math.max(0, (tl.start_song_sec ?? 0) - (tl.count_in_sec ?? 0));
  const wavRes = await page.request.get(`/api/jobs/${JOB_ID}/takes/${take.id}/audio.wav`);
  const wavFile = join(dir, `take-${take.id}.wav`);
  writeFileSync(wavFile, await wavRes.body());
  const playable = bursts(wavFile, 44100).map((i) => i - Math.round(spanStart * 44100)).filter((i) => i > 0.1 * 44100);
  const inVideo = bursts(r0, 44100);
  expect(inVideo.length).toBeGreaterThan(0);
  for (const i of inVideo) {
    const nearest = playable.reduce((a, b) => (Math.abs(b - i) < Math.abs(a - i) ? b : a), Infinity);
    expect(Math.abs(nearest - i), "samples between the take and the video").toBeLessThanOrEqual(1);
  }
});
