// Performance Mode (static/js/streamer/) with a camera and a microphone:
// Chromium fake devices for the device lists, the meter, the camera release,
// recording a take and exporting it. These live in their own file because the
// fake-device launch flags force a new browser, and Playwright only allows
// that at the top of a file. The rest of Performance is in streamer.spec.mjs.

import { test, expect } from "@playwright/test";
import { JOB_ID, openStudio, chooseMode, leaveModeView } from "./helpers.mjs";
import { removeTracks } from "./recording-helpers.mjs";

test.use({ launchOptions: { args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--mute-audio", "--autoplay-policy=no-user-gesture-required"] } });

// A take recorded here makes a Performance track on the shared e2e backend,
// and a spec that runs next in the same shard then finds a track it did not
// make ("the only track is armed by Record" no longer holds). Each test takes
// its tracks and takes away again.
test.afterEach(async ({ request }) => removeTracks(request));

const ev = (page, fn, arg) => page.evaluate(fn, arg);
const sm = (page, sel) => page.locator(`#streamer ${sel}`);
const now = (page) => ev(page, async () => (await import("/js/transport.js")).transport().getCurrentTime());

async function openStreamer(page) {
  await chooseMode(page, "streamer");
  await expect(page.locator("#streamer")).toBeVisible();
  await expect(page.locator('#modeMenu .mode-item[data-mode="streamer"]')).toHaveAttribute("aria-checked", "true");
}

// Press "Allow camera and microphone" when it shows. With the fake-UI flag the
// permission is granted on its own as the view opens, so the button can hide
// between seeing it and clicking it: a click that finds it gone is not a
// failure (the tests then check the camera and the input themselves).
async function allowDevices(page) {
  const allow = sm(page, ".sm-cam-act");
  if (await allow.isVisible()) await allow.click({ timeout: 3000 }).catch(() => {});
}

async function closeStreamer(page) {
  await leaveModeView(page);
  await expect(page.locator("#streamer")).toBeHidden();
  await expect(page.locator('#modeMenu .mode-item[data-mode="streamer"]')).toHaveAttribute("aria-checked", "false");
}

// Phase 3: the camera, the input and the output settings, on Chromium's fake
// camera and microphone.
test.describe("Streamer Mode: devices and meter", () => {
  const media = (page) => ev(page, async () => {
    const m = (await import("/js/streamer/streamerDevices.js")).getStreamerMedia();
    return { camera: Boolean(m.camera?.active), audio: Boolean(m.audio) };
  });
  const savedSettings = (page) => ev(page, async () => (await import("/js/utils.js")).storeGet("stemdeck:streamer:recording", null));

  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1800, height: 1020 });
    await openStudio(page);
    await openStreamer(page);
    await allowDevices(page);
  });

  test("the device lists fill and the live camera and the meter run", async ({ page }) => {
    await expect.poll(() => page.locator("#streamerVideoSrc option").count()).toBeGreaterThan(2);
    await expect.poll(() => page.locator("#streamerAudioSrc option").count()).toBeGreaterThan(2);
    await expect.poll(async () => (await media(page)).camera).toBe(true);
    const meter = sm(page, ".sm-meter");
    await expect(meter).toHaveAttribute("role", "meter");
    await expect(meter).toHaveAttribute("aria-valuemin", "-60");
    await expect.poll(async () => Number(await meter.getAttribute("aria-valuenow"))).toBeGreaterThan(-60);
  });

  test("closing Streamer stops the camera and the input", async ({ page }) => {
    await expect.poll(async () => (await media(page)).camera).toBe(true);
    await ev(page, async () => {
      const m = (await import("/js/streamer/streamerDevices.js")).getStreamerMedia();
      window.__tracks = [...m.camera.getTracks(), ...(m.audio?.getTracks() || [])];
    });
    await closeStreamer(page);
    await expect.poll(() => ev(page, () => window.__tracks.every((tr) => tr.readyState === "ended"))).toBe(true);
  });

  test("choosing No camera releases it, and choosing a camera opens it again", async ({ page }) => {
    await expect.poll(async () => (await media(page)).camera).toBe(true);
    await page.locator("#streamerVideoSrc").selectOption("none");
    await expect.poll(async () => (await media(page)).camera).toBe(false);
    await page.locator("#streamerVideoSrc").selectOption("");
    await expect.poll(async () => (await media(page)).camera).toBe(true);
  });

  // The live window is the camera, full frame, with nothing over it: every
  // part of the picture is the fake camera's, the corners included (a layer
  // card or a lower third would cover a corner in the dark).
  test("the live window shows the camera's picture over the whole frame, at every aspect", async ({ page }) => {
    await expect.poll(async () => (await media(page)).camera).toBe(true);
    // The fake camera's frame, drawn alone: the reference the window must match.
    const sample = (aspect) => ev(page, async (a) => {
      const { fitAspect } = await import("/js/streamer/streamerModel.js");
      const c = document.querySelector("#streamer .sm-canvas");
      const ctx = c.getContext("2d");
      const f = fitAspect(c.width, c.height, a);
      const at = (fx, fy) => [...ctx.getImageData(Math.floor(f.x + f.w * fx), Math.floor(f.y + f.h * fy), 1, 1).data];
      const lit = (px) => px[0] + px[1] + px[2] > 60;
      const spots = [[0.03, 0.03], [0.97, 0.03], [0.03, 0.97], [0.97, 0.97], [0.85, 0.15], [0.85, 0.8], [0.1, 0.92]];
      return { all: spots.every(([x, y]) => lit(at(x, y))), outside: f.x > 2 ? lit(at(-0.02, 0.5)) : null };
    }, aspect);
    for (const aspect of ["16:9", "9:16", "1:1"]) {
      await sm(page, ".sm-aspect select").selectOption(aspect);
      await expect.poll(async () => (await sample(aspect)).all, { message: aspect }).toBe(true);
      // Pillarboxed: outside the picture stays black.
      if (aspect !== "16:9") expect((await sample(aspect)).outside).toBe(false);
    }
  });

  test("output settings are kept per user, 60 fps warns", async ({ page }) => {
    await expect(sm(page, ".sm-heavy")).toBeHidden();
    await page.locator("#streamerFps").selectOption("60");
    await expect(sm(page, ".sm-heavy")).toBeVisible();
    await page.locator("#streamerRes").selectOption("720");
    await page.locator("#streamerFps").selectOption("24");
    await expect(sm(page, ".sm-heavy")).toBeHidden();
    // Two switches: mixed audio and auto-stop. No metronome click.
    await expect(sm(page, ".sm-out-switches [data-setting]")).toHaveCount(2);
    await expect(sm(page, '[data-setting="includeClick"]')).toHaveCount(0);
    await sm(page, '[data-setting="autoStopAtEnd"]').click();
    await expect.poll(async () => (await savedSettings(page))?.resolution).toBe("720");
    expect((await savedSettings(page)).fps).toBe(24);
    expect((await savedSettings(page)).autoStopAtEnd).toBe(false);
    expect("includeClick" in (await savedSettings(page))).toBe(false);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    await openStreamer(page);
    await expect(page.locator("#streamerRes")).toHaveValue("720");
    await expect(page.locator("#streamerFps")).toHaveValue("24");
    await expect(sm(page, '[data-setting="autoStopAtEnd"]')).toHaveAttribute("aria-checked", "false");
  });
});

test.describe("Streamer Mode: camera refused", () => {
  test("a refusal shows the reason with Retry, in the preview and the panel", async ({ page }) => {
    await page.setViewportSize({ width: 1800, height: 1020 });
    await page.addInitScript(() => {
      navigator.mediaDevices.getUserMedia = () => Promise.reject(Object.assign(new Error("denied"), { name: "NotAllowedError" }));
    });
    await openStudio(page);
    await openStreamer(page);
    const act = sm(page, ".sm-cam-act");
    await expect(act).toBeVisible();
    await act.click();
    await expect(sm(page, ".sm-cam-text")).toContainText("blocked");
    await expect(act).toHaveText("Retry");
    await expect(sm(page, ".sm-dev-state").first()).toContainText("blocked");
  });
});

// Phase 4: recording the camera and exporting it through the server.
test.describe("Streamer Mode: recording and export", () => {
  const recording = (page) => ev(page, () => document.querySelector("#streamer .sm-rec")?.classList.contains("recording"));

  async function setUp(page, trim = { in: 1, out: 4 }) {
    await page.setViewportSize({ width: 1800, height: 1020 });
    await openStudio(page);
    await ev(page, async ([id, tr]) => {
      const m = await import("/js/streamer/streamerModel.js");
      const { storeSet } = await import("/js/utils.js");
      await storeSet(`stemdeck:streamer:scene:${id}`, m.serializeScene({ ...m.defaultScene("16:9"), trim: tr }));
    }, [JOB_ID, trim]);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    await page.waitForFunction(() => !/\/\s*00:00\s*$/.test(document.querySelector("#t-time")?.textContent || "00:00 / 00:00"));
    await openStreamer(page);
    await allowDevices(page);
  }

  test("R records from the trim's start, auto-stops at its end, and the take is kept", async ({ page }) => {
    await setUp(page);
    await page.locator("#streamer").focus();
    await page.keyboard.press("r");
    await expect.poll(() => recording(page)).toBe(true);
    // The take is open (the button turns red) a moment before the playhead
    // goes to the trim's start and plays: wait for that, then it is past 1 s,
    // never from the top of the song.
    await expect.poll(() => ev(page, async () => (await import("/js/transport.js")).transport().isPlaying())).toBe(true);
    expect(Math.abs((await now(page)) - 1)).toBeLessThan(1);
    // The take's shape is fixed while it records.
    await expect(sm(page, ".sm-aspect select")).toBeDisabled();
    await expect.poll(() => recording(page), { timeout: 20000 }).toBe(false);
    await expect(sm(page, ".sm-export-go")).toBeVisible({ timeout: 30000 });
    await expect(sm(page, ".sm-aspect select")).toBeEnabled();
  });

  test("speed not 100% refuses recording with the reason", async ({ page }) => {
    await setUp(page);
    await ev(page, async () => (await import("/js/transport.js")).setSpeed(0.75));
    await sm(page, ".sm-rec").click();
    await expect(page.locator("#streamerNote")).toContainText("100%");
    expect(await recording(page)).toBe(false);
  });

  test("export makes an MP4 named after the song", async ({ page }) => {
    await setUp(page);
    await sm(page, ".sm-rec").click();
    await expect.poll(() => recording(page), { timeout: 20000 }).toBe(false);
    await expect(sm(page, ".sm-export-go")).toBeEnabled({ timeout: 30000 });
    const download = page.waitForEvent("download", { timeout: 120000 });
    await sm(page, ".sm-export-go").click();
    const file = await download;
    // Streamer is called Performance now, in the file name too.
    expect(file.suggestedFilename()).toMatch(/\(Performance\)\.mp4$/);
    await expect(sm(page, ".sm-export-text")).toContainText("Saved");
    // No desktop app here: Show in folder is not offered.
    await expect(sm(page, ".sm-export-reveal")).toBeHidden();
  });

  test("Delete during a take does not trash the song", async ({ page }) => {
    await setUp(page, { in: 0, out: 5 });
    await sm(page, ".sm-rec").click();
    await expect.poll(() => recording(page)).toBe(true);
    await page.locator("#streamer").focus();
    await page.keyboard.press("Delete");
    await expect(page.locator(`#catalogList .cat-item[data-id="${JOB_ID}"]`).first()).not.toHaveClass(/in-trash/);
    await page.keyboard.press("r");
    await expect.poll(() => recording(page)).toBe(false);
  });
});

// The input a take records (fix: the drums on an audio interface were never
// recorded): the list is the studio's inputs, each with its channels. Moved
// here from streamer.spec.mjs: it needs a microphone to list.
test.describe("Streamer Mode input", () => {
  test("Audio Source lists the studio's inputs with their channels", async ({ page }) => {
    await page.setViewportSize({ width: 1800, height: 1020 });
    await openStudio(page);
    await openStreamer(page);
    await allowDevices(page);
    const opts = () => page.locator("#streamerAudioSrc option").evaluateAll((os) => os.map((o) => o.textContent));
    await expect.poll(async () => (await opts()).some((o) => /Input 1/.test(o))).toBe(true);
    expect((await opts())[0]).toBe("No audio input");
  });
});
