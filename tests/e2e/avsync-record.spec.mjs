// Performance take with the camera's microphone in the picture's file, and
// the server's measured picture offset (video.sync).
//
// Chromium's fake devices: the fake camera and the fake microphone are two
// devices, so the camera has no microphone of its own. The first test gives
// the fake microphone the camera's group id (enumerateDevices, patched before
// any script runs), so the app treats it as the camera's: the take's input is
// that same microphone, so its clone goes into the picture's file and the
// server's comparison is exact. The second test leaves the devices as they
// are: no camera microphone, the estimate, and the reason.
//
// Written for CI; not run locally (the project runs Playwright only on
// request).

import { test, expect } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { JOB_ID, openStudio, chooseMode } from "./helpers.mjs";
import { removeTracks } from "./recording-helpers.mjs";

// The fake microphone plays this file in a loop: 6 s of noise bursts at
// random moments. Chromium's own fake microphone beeps at a fixed interval,
// and a periodic sound matches at every period: the server rightly refuses
// it ("low-confidence"), which is not what this spec is about.
const RATE = 48000;
const wavPath = join(tmpdir(), `stemdeck-avsync-record-${process.pid}.wav`);
{
  mkdirSync(tmpdir(), { recursive: true });
  const n = RATE * 6;
  const data = Buffer.alloc(n * 2);
  let seed = 12345;
  const rnd = () => ((seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff);
  for (let t = 0.1; t < 5.8; t += 0.15 + rnd() * 0.5) {
    const at = Math.floor(t * RATE);
    for (let i = 0; i < 0.05 * RATE; i++) data.writeInt16LE(Math.round((rnd() * 2 - 1) * 20000), (at + i) * 2);
  }
  const h = Buffer.alloc(44);
  h.write("RIFF", 0);
  h.writeUInt32LE(36 + data.length, 4);
  h.write("WAVEfmt ", 8);
  h.writeUInt32LE(16, 16);
  h.writeUInt16LE(1, 20);
  h.writeUInt16LE(1, 22);
  h.writeUInt32LE(RATE, 24);
  h.writeUInt32LE(RATE * 2, 28);
  h.writeUInt16LE(2, 32);
  h.writeUInt16LE(16, 34);
  h.write("data", 36);
  h.writeUInt32LE(data.length, 40);
  writeFileSync(wavPath, Buffer.concat([h, data]));
}

test.use({ launchOptions: { args: [
  "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--mute-audio",
  "--autoplay-policy=no-user-gesture-required", `--use-file-for-fake-audio-capture=${wavPath}`,
] } });

test.afterEach(async ({ request }) => removeTracks(request));

const ev = (page, fn, arg) => page.evaluate(fn, arg);
const sm = (page, sel) => page.locator(`#streamer ${sel}`);
const recording = (page) => ev(page, () => document.querySelector("#streamer .sm-rec")?.classList.contains("recording"));

// The fake microphone joins the fake camera's group: the app's match
// (cameraAudio.matchCameraMic) then finds it as the camera's microphone.
const SHARE_GROUP = () => {
  const md = navigator.mediaDevices;
  const real = md.enumerateDevices.bind(md);
  md.enumerateDevices = async () => {
    const list = await real();
    const cam = list.find((d) => d.kind === "videoinput" && d.groupId);
    if (!cam) return list;
    return list.map((d) => (d.kind !== "audioinput" ? d : {
      deviceId: d.deviceId, kind: d.kind, label: d.label, groupId: cam.groupId, toJSON() { return this; },
    }));
  };
};

async function setUp(page) {
  await page.setViewportSize({ width: 1800, height: 1020 });
  await openStudio(page);
  await ev(page, async ([id]) => {
    const m = await import("/js/streamer/streamerModel.js");
    const { storeSet } = await import("/js/utils.js");
    await storeSet(`stemdeck:streamer:scene:${id}`, m.serializeScene({ ...m.defaultScene("16:9"), trim: { in: 0, out: 5.5 } }));
  }, [JOB_ID]);
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
  await page.waitForFunction(() => !/\/\s*00:00\s*$/.test(document.querySelector("#t-time")?.textContent || "00:00 / 00:00"));
  await chooseMode(page, "streamer");
  await expect(page.locator("#streamer")).toBeVisible();
  const allow = sm(page, ".sm-cam-act");
  if (await allow.isVisible()) await allow.click({ timeout: 3000 }).catch(() => {});
  await expect.poll(() => ev(page, async () => {
    const m = (await import("/js/streamer/streamerDevices.js")).getStreamerMedia();
    return Boolean(m.camera?.active && m.audio);
  }), { timeout: 15000 }).toBe(true);
}

async function recordTake(page) {
  await page.locator("#streamer").focus();
  await page.keyboard.press("r");
  await expect.poll(() => recording(page)).toBe(true);
  await expect.poll(() => recording(page), { timeout: 30000 }).toBe(false);
  await expect(sm(page, ".sm-export-go")).toBeVisible({ timeout: 30000 });
  const takes = await ev(page, async (id) => (await (await fetch(`/api/jobs/${id}`, { cache: "no-store" })).json()).takes, JOB_ID);
  return takes.reduce((a, b) => ((a?.created_at || 0) > b.created_at ? a : b), null);
}

test("the camera's microphone goes into the picture's file and the picture is measured", async ({ page }) => {
  await page.addInitScript(SHARE_GROUP);
  await setUp(page);
  const take = await recordTake(page);
  expect(take.has_camera).toBe(true);
  const sync = take.video_sync;
  expect(sync).toBeTruthy();
  // The take's input is the same fake microphone: the same sound on both sides.
  expect(sync.base).toBe("measured");
  expect(sync.reason).toBe("ok");
  expect(sync.confidence).toBeGreaterThanOrEqual(0.5);
  // The estimate was the recorder's start event; the measured offset is
  // where the picture really starts, and the review places it there.
  expect(Number.isFinite(sync.offsetMs)).toBe(true);
  // The drawn frames' age, when the engine reported it, is taken off.
  expect(sync.pictureOffsetMs).toBeCloseTo(sync.offsetMs - sync.drawAgeMs, 3);
  const got = await ev(page, async ([id, tid]) => (await fetch(`/api/jobs/${id}/takes/${tid}/sync`)).json(), [JOB_ID, take.id]);
  expect(got.video_sync.offsetMs).toBe(sync.offsetMs);
  // Never heard: no element plays the camera file with its sound on.
  const audible = await ev(page, () => [...document.querySelectorAll("video")].filter((v) => !v.muted && v.src && !v.paused).length);
  expect(audible).toBe(0);
});

test("a camera without a microphone keeps the estimate and says why", async ({ page }) => {
  await setUp(page);
  const take = await recordTake(page);
  expect(take.has_camera).toBe(true);
  expect(take.video_sync.base).toBe("estimated");
  expect(take.video_sync.reason).toBe("no-camera-mic");
  expect(take.video_sync.offsetMs).toBe(take.video_sync.estimateMs);
});

test("a nudge moves the picture and the export must render again", async ({ page }) => {
  await setUp(page);
  const take = await recordTake(page);
  const res = await ev(page, async ([id, tid]) => {
    const r = await fetch(`/api/jobs/${id}/takes/${tid}/sync`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ nudgeMs: 40 }),
    });
    return { status: r.status, body: await r.json() };
  }, [JOB_ID, take.id]);
  expect(res.status).toBe(200);
  expect(res.body.video_sync.source).toBe("manual");
  expect(res.body.camera_start_sec).toBeCloseTo(take.camera_start_sec + 0.04, 3);
});
