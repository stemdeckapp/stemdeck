// The Signalsmith tempo stage loads in a real browser (#729).
//
// tests/js/signalsmith-stage.test.mjs checks the stage itself in node. What
// only a browser can show is that it gets that far at all: the core is a
// WebAssembly module compiled inside the audio worklet, which the page's CSP
// blocks unless script-src allows 'wasm-unsafe-eval'. Blocked, the processor
// quietly keeps WSOLA and every other test still passes, so this watches the
// processor say which stage it is running.

import { test, expect } from "@playwright/test";
import { openStudio } from "./helpers.mjs";

async function recordWorkletMessages(page) {
  await page.addInitScript(() => {
    window.__workletMessages = [];
    const Native = window.AudioWorkletNode;
    window.AudioWorkletNode = class extends Native {
      constructor(...args) {
        super(...args);
        this.port.addEventListener("message", (event) => window.__workletMessages.push(event.data));
      }
    };
  });
}

test("slowed playback runs on the Signalsmith stage", async ({ page }) => {
  const failures = [];
  page.on("console", (msg) => {
    if (msg.type() === "warning" && msg.text().includes("[tempoStage]")) failures.push(msg.text());
  });
  await recordWorkletMessages(page);
  await openStudio(page);

  await page.locator("#t-speed-input").fill("0.75");
  await page.locator("#t-speed-input").press("Enter");
  await page.locator("#t-play").click();

  await expect.poll(
    () => page.evaluate(() => window.__workletMessages.filter((m) => m?.type === "tempoStage").at(-1)?.stage),
    { timeout: 10000 },
  ).toBe("signalsmith");
  expect(failures).toEqual([]);
});
