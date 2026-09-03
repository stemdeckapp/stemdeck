// Transpose on the phone UI.
//
// A phone does not get the studio. main.py routes it to static/mobile by user
// agent, so whatever the engine can do is irrelevant there unless this screen
// has a control for it, and it had none. That makes this the only place the
// mobile half of transpose is checked at all.
//
// Transpose needs AudioWorklet, which browsers only expose on a secure origin,
// so a phone reaching StemDeck over plain http://<lan-ip> cannot have it. The
// control has to be honest about that rather than absent or silently dead,
// which is the second half of what these tests cover.

import { test, expect } from "@playwright/test";
import { JOB_ID } from "./helpers.mjs";

const keyValue = (page) => page.locator(".key-row .speed-row-val");
const keyUp = (page) => page.locator('.key-row [data-key-step="1"]');
const keyDown = (page) => page.locator('.key-row [data-key-step="-1"]');

/**
 * Land on the Library tab, which the app does not open on.
 *
 * `?ui=mobile` rather than a phone user agent: the routing is the backend's
 * business and is not what this file is about, and pinning a UA string here
 * would make these tests fail the next time that list is edited.
 */
async function gotoLibrary(page) {
  await page.goto("/?ui=mobile", { waitUntil: "domcontentloaded" });
  await page.locator('[data-action="tab"][data-tab="library"]').first().click();
  await page.locator(`.track[data-id="${JOB_ID}"]`).first().waitFor({ timeout: 20000 });
}

/** Strip the secure-context APIs, the way a plain http origin does. */
const asInsecureOrigin = (page) =>
  page.addInitScript(() => {
    Object.defineProperty(window, "isSecureContext", { value: false, configurable: true });
    for (const Ctor of [window.AudioContext, window.webkitAudioContext]) {
      if (Ctor) {
        Object.defineProperty(Ctor.prototype, "audioWorklet", {
          get: () => undefined,
          configurable: true,
        });
      }
    }
    window.AudioWorkletNode = undefined;
  });

/** Open the fixture track on the phone UI and wait for its engine. */
async function openOnPhone(page) {
  await gotoLibrary(page);
  await page.locator(`.track[data-id="${JOB_ID}"]`).first().click();
  // The key steppers stay disabled until the engine reports a pitch stage, so
  // an enabled stepper is exactly "the audio is ready and transpose is real".
  await expect(keyUp(page)).toBeEnabled({ timeout: 20000 });
}

test("the phone UI has a transpose control at all", async ({ page }) => {
  await openOnPhone(page);
  await expect(keyValue(page)).toHaveText("0");
});

test("stepping it moves the key", async ({ page }) => {
  await openOnPhone(page);
  await keyUp(page).click();
  await expect(keyValue(page)).toHaveText("+1");
  await keyDown(page).click();
  await keyDown(page).click();
  await expect(keyValue(page)).toHaveText("-1");
});

test("the control reaches the audio graph, not just the label", async ({ page }) => {
  await page.addInitScript(() => {
    window.__connects = 0;
    const orig = AudioNode.prototype.connect;
    // A lane's transpose is a connection, not a parameter, so the only honest
    // evidence the control did anything is the graph being rewired.
    AudioNode.prototype.connect = function connect(...args) {
      window.__connects++;
      return orig.apply(this, args);
    };
  });
  await openOnPhone(page);
  const before = await page.evaluate(() => window.__connects);
  await keyUp(page).click();
  await expect(keyValue(page)).toHaveText("+1");
  expect(await page.evaluate(() => window.__connects)).toBeGreaterThan(before);
});

test("the range stops at the ends rather than wrapping", async ({ page }) => {
  await openOnPhone(page);
  // Six clicks is the whole range. The seventh is not a no-op that still
  // fires: the button is gone, which is what "stops at the end" has to mean on
  // a touch screen where a held thumb repeats.
  for (let i = 0; i < 6; i++) await keyUp(page).click();
  await expect(keyValue(page)).toHaveText("+6");
  await expect(keyUp(page)).toBeDisabled();
  for (let i = 0; i < 12; i++) await keyDown(page).click();
  await expect(keyValue(page)).toHaveText("-6");
  await expect(keyDown(page)).toBeDisabled();
});

test("a new track starts back at its own key", async ({ page }) => {
  await openOnPhone(page);
  await keyUp(page).click();
  await expect(keyValue(page)).toHaveText("+1");
  await page.locator('[data-action="tab"][data-tab="library"]').first().click();
  await page.locator(".track[data-id]").nth(1).click();
  await expect(keyValue(page)).toHaveText("0");
});

test("over a plain http origin it is disabled and says why", async ({ page }) => {
  // The common case on a phone today, and the one that must not look like a
  // bug in the app: a dead stepper with no explanation is indistinguishable
  // from a broken build.
  await asInsecureOrigin(page);
  await gotoLibrary(page);
  await page.locator(`.track[data-id="${JOB_ID}"]`).first().click();
  await expect(page.locator(".key-row")).toHaveAttribute("title", /not available/i);
  await expect(keyUp(page)).toBeDisabled();
  await expect(keyDown(page)).toBeDisabled();
});
