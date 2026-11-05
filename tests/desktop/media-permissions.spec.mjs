// Microphone and camera in the real Windows desktop app (desktop/src-tauri/src/mediaperm.rs).
//
// Runs only against a desktop app that is already running with its WebView2
// DevTools port open, so CI never runs it (no desktop app there, and the root
// playwright.config.mjs only collects tests/e2e). Start the app with
//
//   WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS="--remote-debugging-port=9333 --mute-audio"
//
// (and, to keep the real profile and library out of it, LOCALAPPDATA,
// WEBVIEW2_USER_DATA_FOLDER and STEMDECK_DATA_DIR pointed at empty folders),
// wait for the studio to open, then:
//
//   STEMDECK_DESKTOP_CDP=http://127.0.0.1:9333 npx playwright test -c tests/desktop/playwright.desktop.config.mjs
//
// STEMDECK_DESKTOP_JOB=<job id of a finished song in that library> adds the
// track picker check. Every check needs a microphone and a camera plugged in;
// nothing is played.
//
// What a prompt would look like here: getUserMedia never settles while
// WebView2's own permission bubble waits for a click. So "no prompt" is
// "settles, granted, within a few seconds", and "refused by the handler" is
// "NotAllowedError at once".

import { test, expect, chromium } from "@playwright/test";

const CDP = process.env.STEMDECK_DESKTOP_CDP;
const JOB = process.env.STEMDECK_DESKTOP_JOB;

test.skip(!CDP, "set STEMDECK_DESKTOP_CDP to the running desktop app's DevTools address");

let browser;
let page;
let backend;

test.beforeAll(async () => {
  browser = await chromium.connectOverCDP(CDP);
  page = browser.contexts()[0].pages().find((p) => p.url().startsWith("http://127.0.0.1:"));
  if (!page) throw new Error("the app window is not on the backend page yet");
  backend = new URL(page.url()).origin;
});

test.afterAll(async () => {
  // Back where it started, then detach. close() on a CDP connection
  // disconnects; it does not close the app.
  if (page && backend && !page.url().startsWith(backend)) await page.goto(`${backend}/`);
  await browser?.close();
});

/** Ask for audio, then video, in the page; report what happened to each. */
function ask(p) {
  return p.evaluate(async () => {
    const out = {};
    for (const kind of ["audio", "video"]) {
      const t0 = performance.now();
      try {
        const stream = await Promise.race([
          navigator.mediaDevices.getUserMedia({ [kind]: true }),
          new Promise((_, reject) => setTimeout(() => reject(Object.assign(new Error("no answer"), { name: "Timeout" })), 8000)),
        ]);
        stream.getTracks().forEach((tr) => tr.stop());
        out[kind] = { result: "granted", ms: performance.now() - t0 };
      } catch (e) {
        out[kind] = { result: e.name, ms: performance.now() - t0 };
      }
    }
    return out;
  });
}

const micState = (p) => p.evaluate(async () => (await navigator.permissions.query({ name: "microphone" })).state);

test("the backend page gets the microphone and the camera without a prompt", async () => {
  await page.goto(`${backend}/`);
  // Nothing saved in the profile: a fresh page starts at "prompt".
  expect(await micState(page)).toBe("prompt");
  const res = await ask(page);
  expect(res.audio.result, JSON.stringify(res)).toBe("granted");
  expect(res.video.result, JSON.stringify(res)).toBe("granted");
  const devices = await page.evaluate(async () => (await navigator.mediaDevices.enumerateDevices())
    .map((d) => ({ kind: d.kind, label: d.label })));
  expect(devices.filter((d) => d.kind === "audioinput" && d.label).length).toBeGreaterThan(0);
  expect(devices.filter((d) => d.kind === "videoinput" && d.label).length).toBeGreaterThan(0);
});

test("another origin in the window is refused at once, and nothing is saved", async () => {
  const port = new URL(backend).port;
  // The same server under another name is another origin: only the exact
  // http://127.0.0.1:<port> the shell started is granted.
  await page.goto(`http://localhost:${port}/`);
  const res = await ask(page);
  for (const kind of ["audio", "video"]) {
    expect(res[kind].result, JSON.stringify(res)).toBe("NotAllowedError");
    expect(res[kind].ms, JSON.stringify(res)).toBeLessThan(2000);
  }
  await page.goto(`${backend}/`);
  expect(await micState(page)).toBe("prompt");
  expect((await ask(page)).audio.result).toBe("granted");
});

test("a new track lists the inputs by name on a fresh page", async () => {
  test.skip(!JOB, "set STEMDECK_DESKTOP_JOB to a finished song's job id");
  const tracksBefore = (await (await page.request.get(`${backend}/api/jobs/${JOB}`)).json()).tracks || [];
  await page.goto(`${backend}/`);
  await page.locator(`.cat-item[data-id="${JOB}"]`).first().click();
  await page.waitForFunction(
    () => !/\/\s*00:00\s*$/.test(document.querySelector("#t-time")?.textContent || "00:00 / 00:00"),
    null,
    { timeout: 30000 },
  );
  const rows = page.locator(".mixer-column .lane-header.track-lane:not(.hidden)");
  const before = await rows.count();
  await page.locator("#addTrackBtn").click();
  await expect(rows).toHaveCount(before + 1, { timeout: 20000 });
  const select = rows.nth(before).locator(".track-input");
  try {
    // Before the fix this read "No input found" until a track was armed.
    await expect(select).toBeEnabled({ timeout: 10000 });
    expect((await select.locator("option").allTextContents()).length).toBeGreaterThan(0);
  } finally {
    const after = (await (await page.request.get(`${backend}/api/jobs/${JOB}`)).json()).tracks || [];
    for (const tr of after) {
      if (!tracksBefore.some((b) => b.id === tr.id)) await page.request.delete(`${backend}/api/jobs/${JOB}/tracks/${tr.id}`);
    }
  }
});
