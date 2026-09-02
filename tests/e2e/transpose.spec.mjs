// The transpose controls: the transport stepper and the per-lane key steppers.
//
// The DSP is measured in tests/js/pitch-shift.test.mjs and the graph wiring in
// tests/js/audio-routing.test.mjs. What can only be checked in a real browser
// is that the controls reach a real Web Audio graph at all, that their readouts
// agree with what was sent, and that the ends of the range are reachable but
// not passable.

import { test, expect } from "@playwright/test";
import { JOB_ID, openStudio } from "./helpers.mjs";

const down = (page) => page.locator("#t-pitch-down");
const up = (page) => page.locator("#t-pitch-up");
const value = (page) => page.locator("#t-pitch-value");
const reset = (page) => page.locator("#t-pitch-reset");

const laneKey = (page, stem) => page.locator(`.lane-key[data-stem="${stem}"]`);
const laneUp = (page, stem) => laneKey(page, stem).locator(".lane-key-step.up");
const laneDown = (page, stem) => laneKey(page, stem).locator(".lane-key-step.down");
const laneValue = (page, stem) => laneKey(page, stem).locator(".lane-key-value");

// Input 6 is semitone 0, so input (6 + n) is a shift of n semitones.
const ZERO_INPUT = 6;

/**
 * Record which worklet input each lane gets wired to.
 *
 * A lane's transpose is a connection, not a parameter, so the only honest way
 * to check that a control reached the audio graph is to watch the graph being
 * rewired. Installed from the test side rather than exposing the engine on
 * `window`: a stepper that updates its own label while never reconnecting
 * anything would satisfy every other assertion here, and that is precisely the
 * wiring bug worth catching.
 */
async function recordRouting(page) {
  await page.addInitScript(() => {
    window.__transposeWorkletOptions = null;
    window.__routes = [];
    const busInput = new WeakMap();
    let workletNode = null;

    const Orig = window.AudioWorkletNode;
    if (Orig) {
      window.AudioWorkletNode = class extends Orig {
        constructor(...args) {
          super(...args);
          if (args[1] === "soundtouch-processor") {
            window.__transposeWorkletOptions = args[2] || null;
            workletNode = this;
          }
        }
      };
    }

    const origConnect = AudioNode.prototype.connect;
    AudioNode.prototype.connect = function connect(destination, output, input) {
      const result = origConnect.apply(this, arguments);
      if (workletNode && destination === workletNode) {
        // A bus announcing which input it owns.
        busInput.set(this, input || 0);
      } else if (busInput.has(destination)) {
        // A lane arriving on one of those buses.
        window.__routes.push(busInput.get(destination));
      }
      return result;
    };
  });
}

const clearRoutes = (page) => page.evaluate(() => { window.__routes.length = 0; });

/**
 * Wait for the engine to finish wiring every lane up, then start recording.
 *
 * Stems are decoded asynchronously and each one is routed as it lands, so a
 * test that clears the log the moment the page is interactive still catches
 * the tail of that initial wiring and reads it as a lane that moved.
 */
async function settleRoutes(page) {
  let previous = -1;
  await expect.poll(async () => {
    const count = await page.evaluate(() => window.__routes.length);
    const settled = count > 0 && count === previous;
    previous = count;
    return settled;
  }, { timeout: 20000 }).toBe(true);
  await clearRoutes(page);
}
const recentInputs = (page) => page.evaluate(
  () => [...new Set(window.__routes || [])].sort((a, b) => a - b),
);

test.describe("transpose", () => {
  test("the group is labelled as the global key", async ({ page }) => {
    await openStudio(page);
    // "Key" alone read as the only key control once the lanes gained their own.
    await expect(page.locator("#t-pitch-label")).toHaveText(/global/i);
  });

  test("starts at zero and is not lit", async ({ page }) => {
    await openStudio(page);
    await expect(value(page)).toHaveText("0");
    // The readout is only highlighted while the track is off its own key, so
    // "this is not the original key" is visible without reading the number.
    await expect(value(page)).not.toHaveClass(/active/);
    await expect(down(page)).toBeEnabled();
    await expect(up(page)).toBeEnabled();
  });

  test("steps up and down in semitones and signs the value", async ({ page }) => {
    await openStudio(page);
    await up(page).click();
    await up(page).click();
    await expect(value(page)).toHaveText("+2");
    await expect(value(page)).toHaveClass(/active/);

    for (let i = 0; i < 3; i++) await down(page).click();
    await expect(value(page)).toHaveText("-1");

    await up(page).click();
    await expect(value(page)).toHaveText("0");
    await expect(value(page)).not.toHaveClass(/active/);
  });

  test("the range ends are reachable and the buttons say so", async ({ page }) => {
    await openStudio(page);
    for (let i = 0; i < 6; i++) await up(page).click();
    await expect(value(page)).toHaveText("+6");
    // Disabled rather than merely inert: a stepper that stops responding with
    // no visible reason reads as broken.
    await expect(up(page)).toBeDisabled();
    await expect(down(page)).toBeEnabled();

    for (let i = 0; i < 12; i++) await down(page).click();
    await expect(value(page)).toHaveText("-6");
    await expect(down(page)).toBeDisabled();
    await expect(up(page)).toBeEnabled();
  });

  test("the production worklet has one input per semitone", async ({ page }) => {
    await recordRouting(page);
    await openStudio(page);
    await expect
      .poll(() => page.evaluate(() => window.__transposeWorkletOptions?.numberOfInputs ?? null))
      .toBe(13);
  });

  test("what the label shows is what the graph was rewired to", async ({ page }) => {
    await recordRouting(page);
    await openStudio(page);
    await settleRoutes(page);

    await up(page).click();
    await up(page).click();
    // Each step rewires, so the log is cleared just before the last one: what
    // is being checked is where the graph ended up, not the route it took.
    await clearRoutes(page);
    await up(page).click();
    await expect(value(page)).toHaveText("+3");

    // Every lane that moved went to the same input, and it is the one that
    // corresponds to the number on screen.
    await expect.poll(() => recentInputs(page)).toEqual([ZERO_INPUT + 3]);
  });

  test("transposing does not disturb the speed control", async ({ page }) => {
    // The two share a worklet, and the pitch stage changes the stretch ratio
    // the speed control also depends on. They have to stay independent.
    await openStudio(page);
    await page.locator("#t-speed-075").click();
    await expect(page.locator("#t-speed-075")).toHaveClass(/active/);

    await up(page).click();
    await expect(value(page)).toHaveText("+1");
    await expect(page.locator("#t-speed-075")).toHaveClass(/active/);
    await expect(page.locator("#t-speed-1")).not.toHaveClass(/active/);
  });

  test("reopening a track resets its displayed and applied key", async ({ page }) => {
    await recordRouting(page);
    await openStudio(page);
    await up(page).click();
    await up(page).click();
    await expect(value(page)).toHaveText("+2");

    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    await expect(value(page)).toHaveText("0");
    await expect(value(page)).not.toHaveClass(/active/);
  });

  test("disables transpose when AudioWorklet setup is unavailable", async ({ page }) => {
    await page.addInitScript(() => { window.AudioWorkletNode = undefined; });
    await openStudio(page);
    await expect(value(page)).toHaveText("0");
    await expect(down(page)).toBeDisabled();
    await expect(up(page)).toBeDisabled();
  });
});

test.describe("per-lane transpose", () => {
  test("every instrument lane carries a key stepper", async ({ page }) => {
    await openStudio(page);
    await expect(laneKey(page, "vocals")).toBeVisible();
    // Reads as a label until it is doing something, then as a number.
    await expect(laneValue(page, "vocals")).toHaveText("K");
    await expect(laneKey(page, "vocals")).not.toHaveClass(/active/);
  });

  test("the drum lane has the control, disabled, and says why", async ({ page }) => {
    await openStudio(page);
    // Present rather than absent: a missing button on one row reads as a
    // rendering bug, a disabled one that explains itself teaches the rule.
    await expect(laneKey(page, "drums")).toBeVisible();
    await expect(laneKey(page, "drums")).toHaveClass(/locked/);
    await expect(laneUp(page, "drums")).toBeDisabled();
    await expect(laneDown(page, "drums")).toBeDisabled();
    await expect(laneKey(page, "drums")).toHaveAttribute("title", /drum/i);
  });

  test("a lane steps, signs its value and lights up", async ({ page }) => {
    await openStudio(page);
    await laneUp(page, "vocals").click();
    await laneUp(page, "vocals").click();
    await expect(laneValue(page, "vocals")).toHaveText("+2");
    await expect(laneKey(page, "vocals")).toHaveClass(/active/);

    for (let i = 0; i < 3; i++) await laneDown(page, "vocals").click();
    await expect(laneValue(page, "vocals")).toHaveText("-1");

    await laneUp(page, "vocals").click();
    await expect(laneValue(page, "vocals")).toHaveText("K");
    await expect(laneKey(page, "vocals")).not.toHaveClass(/active/);
  });

  test("a lane offset rewires only that lane", async ({ page }) => {
    await recordRouting(page);
    await openStudio(page);
    await settleRoutes(page);

    await laneUp(page, "vocals").click();
    await expect(laneValue(page, "vocals")).toHaveText("+1");
    await expect.poll(() => recentInputs(page)).toEqual([ZERO_INPUT + 1]);
  });

  test("the global key carries every lane with it", async ({ page }) => {
    await openStudio(page);
    await up(page).click();
    await up(page).click();
    await expect(value(page)).toHaveText("+2");
    // A lane reads the key it is actually in, so moving the whole track moves
    // the numbers on the lanes too. There is never a second number to add.
    await expect(laneValue(page, "vocals")).toHaveText("+2");
    await expect(laneValue(page, "bass")).toHaveText("+2");
    // Except drums, which never move.
    await expect(laneValue(page, "drums")).toHaveText("K");
  });

  test("a lane keeps its distance when the global key moves", async ({ page }) => {
    await openStudio(page);
    await laneUp(page, "vocals").click();
    await laneUp(page, "vocals").click();
    await laneUp(page, "vocals").click();
    await expect(laneValue(page, "vocals")).toHaveText("+3");

    await up(page).click();
    // The global control nudges rather than overrides: a lane deliberately put
    // above the rest stays above the rest.
    await expect(laneValue(page, "vocals")).toHaveText("+4");
    await expect(laneValue(page, "bass")).toHaveText("+1");
  });

  test("a lane moved on its own rewires only itself", async ({ page }) => {
    await recordRouting(page);
    await openStudio(page);

    await settleRoutes(page);
    await up(page).click();
    await up(page).click();
    await expect(value(page)).toHaveText("+2");

    await clearRoutes(page);
    await laneDown(page, "vocals").click();
    await expect(laneValue(page, "vocals")).toHaveText("+1");
    await expect.poll(() => recentInputs(page)).toEqual([ZERO_INPUT + 1]);
  });

  test("a lane stops at the ends of the range", async ({ page }) => {
    await openStudio(page);
    for (let i = 0; i < 6; i++) await laneUp(page, "vocals").click();
    await expect(laneValue(page, "vocals")).toHaveText("+6");
    await expect(laneUp(page, "vocals")).toBeDisabled();
    await expect(laneDown(page, "vocals")).toBeEnabled();

    for (let i = 0; i < 12; i++) await laneDown(page, "vocals").click();
    await expect(laneValue(page, "vocals")).toHaveText("-6");
    await expect(laneDown(page, "vocals")).toBeDisabled();
  });

  test("reset returns the global key and every lane to the original", async ({ page }) => {
    await openStudio(page);
    await up(page).click();
    await up(page).click();
    await laneUp(page, "vocals").click();
    await laneDown(page, "bass").click();
    await expect(value(page)).toHaveText("+2");
    await expect(laneValue(page, "vocals")).toHaveText("+3");
    await expect(laneValue(page, "bass")).toHaveText("+1");

    await reset(page).click();
    // Everything, not just the global control: once lanes have been moved
    // individually, stepping the global back to zero cannot undo them.
    await expect(value(page)).toHaveText("0");
    await expect(value(page)).not.toHaveClass(/active/);
    await expect(laneValue(page, "vocals")).toHaveText("K");
    await expect(laneValue(page, "bass")).toHaveText("K");
    await expect(laneKey(page, "vocals")).not.toHaveClass(/active/);
  });

  test("reset is unavailable when transpose is", async ({ page }) => {
    await page.addInitScript(() => { window.AudioWorkletNode = undefined; });
    await openStudio(page);
    await expect(reset(page)).toBeDisabled();
  });

  test("lane keys are disabled when AudioWorklet is unavailable", async ({ page }) => {
    await page.addInitScript(() => { window.AudioWorkletNode = undefined; });
    await openStudio(page);
    await expect(laneKey(page, "vocals")).toHaveClass(/unsupported/);
    await expect(laneUp(page, "vocals")).toBeDisabled();
  });
});

// AudioWorklet is a secure-context API, so a client reaching a networked
// StemDeck over http://<lan-ip> is never given it and transpose cannot work
// there at all. Nothing can be done about that in the page; what the page owes
// the user is the actual reason, because "needs Web Audio" sent people looking
// for a missing browser feature while Web Audio was working fine (#552).
//
// isSecureContext is read-only, so it is redefined before any app script runs.
// The engine is separately denied its worklet: on the localhost origin these
// tests run from, the real one would load and there would be nothing to
// explain.
test.describe("transpose over an insecure origin", () => {
  const asInsecureClient = (page) =>
    page.addInitScript(() => {
      Object.defineProperty(window, "isSecureContext", { value: false, configurable: true });
      window.AudioWorkletNode = undefined;
    });

  test("the transport control says the connection is the problem", async ({ page }) => {
    await asInsecureClient(page);
    await openStudio(page);
    await expect(down(page)).toBeDisabled();
    // The reason sits on the group: a disabled button does not reliably fire
    // the pointer events a tooltip needs.
    await expect(page.locator("#t-pitch-wrap")).toHaveAttribute("title", /network connection/i);
  });

  test("a lane stepper gives the same answer, not a different one", async ({ page }) => {
    await asInsecureClient(page);
    await openStudio(page);
    await expect(laneKey(page, "vocals")).toHaveAttribute("title", /network connection/i);
  });

  test("the speed control admits it will move the key too", async ({ page }) => {
    await asInsecureClient(page);
    await openStudio(page);
    // Speed still works here, it just resamples -- the one degradation that is
    // otherwise completely silent.
    await expect(page.locator("#t-speed-wrap")).toHaveAttribute("title", /key|pitch/i);
  });

  test("a secure origin that still cannot build the worklet says something else", async ({ page }) => {
    await page.addInitScript(() => { window.AudioWorkletNode = undefined; });
    await openStudio(page);
    const title = await page.locator("#t-pitch-wrap").getAttribute("title");
    expect(title).toBeTruthy();
    expect(title).not.toMatch(/network connection/i);
  });

  test("nothing is explained when transpose works", async ({ page }) => {
    await openStudio(page);
    await expect(up(page)).toBeEnabled();
    await expect(page.locator("#t-pitch-wrap")).not.toHaveAttribute("title", /.+/);
    await expect(page.locator("#t-speed-wrap")).not.toHaveAttribute("title", /.+/);
  });
});
