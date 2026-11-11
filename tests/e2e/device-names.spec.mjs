// Device names before any permission is saved. The desktop app's shell
// grants its page the microphone and the camera without asking but saves
// nothing (desktop/src-tauri/src/mediaperm.rs), so every load starts with
// nameless devices that have no ids. Settings > Recording then offered only
// None, and Performance waited for Allow. In a browser nothing may open the
// microphone on its own: the picker says it is not allowed yet.
//
// Chromium's fake-UI flag names every device before any grant, so the
// nameless start is put back here: until this page has opened a kind with
// getUserMedia, enumerateDevices lists that kind with no name and no id, the
// way WebView2 does on every load. getUserMedia itself is the real one (the
// fake-UI flag grants it), and each call is counted per kind.
//
// Own file: the fake-device launch flags force a new browser, and Playwright
// only allows that at the top of a file.

import { test, expect } from "@playwright/test";
import { openStudio, chooseMode } from "./helpers.mjs";

test.use({
  launchOptions: {
    args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--mute-audio"],
  },
});

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const md = navigator.mediaDevices;
    if (!md) return;
    const opened = new Set();
    window.__mediaOpens = { audioinput: 0, videoinput: 0 };
    const realList = md.enumerateDevices.bind(md);
    const realOpen = md.getUserMedia.bind(md);
    md.enumerateDevices = async () => (await realList()).map((d) => (opened.has(d.kind)
      ? d : { kind: d.kind, deviceId: "", groupId: "", label: "", getCapabilities: () => ({}) }));
    md.getUserMedia = async (c) => {
      const stream = await realOpen(c);
      for (const kind of [c?.audio && "audioinput", c?.video && "videoinput"].filter(Boolean)) {
        opened.add(kind);
        window.__mediaOpens[kind] += 1;
      }
      return stream;
    };
  });
});

const opens = (page) => page.evaluate(() => window.__mediaOpens);

async function openRecordingSettings(page) {
  await page.locator("#settingsBtn").click();
  await page.locator('.settings-tab[data-tab="recording"]').click();
}

test("desktop app: Settings > Recording names the inputs on first open, with no track", async ({ page }) => {
  await openStudio(page, { tauri: true });
  expect((await opens(page)).audioinput).toBe(0);
  await openRecordingSettings(page);
  for (const id of ["#setRecMic", "#setRecInterface"]) {
    await expect.poll(() => page.locator(`${id} option`).allTextContents(), { timeout: 10000 })
      .toEqual(["None", "Fake Audio Input 1", "Fake Audio Input 2"]);
    await expect(page.locator(id)).toBeEnabled();
  }
  // The camera needs no unlock: the tab opens it for the preview, and the
  // picker reads the names again once it is open.
  await expect.poll(() => page.locator("#setRecCamera option").allTextContents())
    .toEqual(["Default device", "fake_device_0"]);
});

test("browser without permission: the pickers say the microphone is not allowed yet", async ({ page }) => {
  await openStudio(page);
  await openRecordingSettings(page);
  for (const id of ["#setRecMic", "#setRecInterface"]) {
    await expect.poll(() => page.locator(`${id} option`).allTextContents(), { timeout: 10000 })
      .toEqual(["Microphone not allowed yet"]);
    await expect(page.locator(id)).toBeDisabled();
  }
  await expect(page.locator(".rec-settings .rec-permission .capture-reason[data-reason='not-granted']").first())
    .toContainText("may not use the microphone yet");
  // The microphone was never opened to read the names.
  expect((await opens(page)).audioinput).toBe(0);
});

test("desktop app: Performance lists the devices by name without Allow", async ({ page }) => {
  await page.setViewportSize({ width: 1800, height: 1020 });
  await openStudio(page, { tauri: true });
  await chooseMode(page, "streamer");
  await expect(page.locator("#streamer")).toBeVisible();
  // An interface lists each input: "Fake Audio Input 1 · Input 1", ...
  await expect.poll(async () => (await page.locator("#streamerAudioSrc option").allTextContents())
    .filter((s) => s.startsWith("Fake Audio Input")).length, { timeout: 10000 }).toBeGreaterThan(1);
  await expect.poll(() => page.locator("#streamerVideoSrc option").allTextContents())
    .toEqual(expect.arrayContaining(["fake_device_0"]));
  // No Allow step: the camera opens, and the button over the live window stays hidden.
  await expect.poll(() => page.evaluate(async () => Boolean((await import("/js/streamer/streamerDevices.js")).getStreamerMedia().camera?.active))).toBe(true);
  await expect(page.locator("#streamer .sm-cam-act")).toBeHidden();
});
