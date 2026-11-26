// Performance Mode's speed control: in the clock's card of the strip under
// the windows (Take Timeline, undo and redo, the clock). It is the
// transport's one speed (transport.js), the footer's, hidden while
// Performance is open: its box is wired by transport.wireSpeedBox, its steps
// go through transport.stepSpeed, and it shows transport.getSpeed. So a speed
// set here plays and shows in the footer's box, and a speed set anywhere
// else shows here.
//
// A take records at 1x only and a change of rate ends it, so the control is
// locked while a take is open (streamerModel.speedView, tested in Node in
// tests/js/streamer-model.test.mjs), with the reason on the group.
//
// In a narrow strip (under 560 px) the clock and the speed take a line of
// their own under the title and undo / redo; nothing scrolls sideways at a
// phone's width.

import { test, expect } from "@playwright/test";
import { JOB_ID, openStudio, chooseMode } from "./helpers.mjs";
import { removeTracks } from "./recording-helpers.mjs";

// A take recorded here makes a Performance track on the shared backend.
test.afterEach(async ({ request }) => removeTracks(request));

const ev = (page, fn, arg) => page.evaluate(fn, arg);
const sm = (page, sel) => page.locator(`#streamer ${sel}`);
const box = (page) => sm(page, ".sm-speed-input");
const rate = (page) => ev(page, async () => (await import("/js/state.js")).playbackSpeed);
const type = async (page, text) => {
  await box(page).fill(text);
  await box(page).press("Enter");
};

async function openPerformance(page) {
  await chooseMode(page, "streamer");
  await expect(page.locator("#streamer")).toBeVisible();
}

test.describe("Performance speed", () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1366, height: 768 });
  });

  test("the steps and the box change the transport's one speed, and the footer's box follows", async ({ page }) => {
    await openStudio(page);
    await openPerformance(page);
    await expect(box(page)).toHaveValue("1.00");
    await expect(sm(page, ".sm-speed")).not.toHaveClass(/active/);
    await expect(sm(page, ".sm-speed")).toHaveAttribute("aria-label", "Playback speed");
    await expect(sm(page, ".sm-speed-down")).toHaveAttribute("aria-label", "Slower by 0.05x");

    await sm(page, ".sm-speed-down").click();
    await sm(page, ".sm-speed-down").click();
    await expect(box(page)).toHaveValue("0.90");
    await expect.poll(() => rate(page)).toBe(0.9);
    await expect(sm(page, ".sm-speed")).toHaveClass(/active/);
    // The footer's box (hidden here) reads the same value.
    await expect(page.locator("#t-speed-input")).toHaveValue("0.90");

    // Typed as in the footer: a percentage, then a step lands on a notch.
    await type(page, "83%");
    await expect.poll(() => rate(page)).toBe(0.83);
    await sm(page, ".sm-speed-up").click();
    await expect(box(page)).toHaveValue("0.85");
    await expect.poll(() => rate(page)).toBe(0.85);

    // The arrow keys move a hundredth, as in the footer's box.
    await box(page).focus();
    await page.keyboard.press("ArrowDown");
    await expect(box(page)).toHaveValue("0.84");
    await expect.poll(() => rate(page)).toBe(0.84);
    await page.keyboard.press("Escape");

    // The ends: each step stops there.
    await type(page, "0.2");
    await expect(box(page)).toHaveValue("0.50");
    await expect(sm(page, ".sm-speed-down")).toBeDisabled();
    await expect(sm(page, ".sm-speed-up")).toBeEnabled();
    await type(page, "2");
    await expect(box(page)).toHaveValue("1.50");
    await expect(sm(page, ".sm-speed-up")).toBeDisabled();
    await expect(sm(page, ".sm-speed-down")).toBeEnabled();

    // Kept in Performance's own record of the song, like any speed set here.
    await expect.poll(() => ev(page, async () => (await import("/js/mixer.js")).mixGlobals.speed)).toBe(1.5);
  });

  test("a speed set anywhere else shows here", async ({ page }) => {
    await openStudio(page);
    await openPerformance(page);
    await ev(page, async () => (await import("/js/transport.js")).setSpeed(0.7));
    await expect(box(page)).toHaveValue("0.70");
    await expect(sm(page, ".sm-speed")).toHaveClass(/active/);
  });

  test("locked while a take records, with the reason, and free again after", async ({ page }) => {
    await openStudio(page);
    await ev(page, async (id) => {
      const m = await import("/js/streamer/streamerModel.js");
      const { storeSet } = await import("/js/utils.js");
      await storeSet(`stemdeck:streamer:scene:${id}`, m.serializeScene({ ...m.defaultScene("16:9"), trim: { in: 0, out: 6 } }));
    }, JOB_ID);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    await openPerformance(page);
    await sm(page, ".sm-rec").click();
    await expect(sm(page, ".sm-rec")).toHaveClass(/recording/, { timeout: 15000 });
    await expect(box(page)).toBeDisabled();
    await expect(sm(page, ".sm-speed-down")).toBeDisabled();
    await expect(sm(page, ".sm-speed-up")).toBeDisabled();
    await expect(sm(page, ".sm-speed")).toHaveAttribute("title", /Locked during a take/);
    expect(await rate(page)).toBe(1);
    // It stops at the trim's end; the control is free again.
    await expect(sm(page, ".sm-rec")).not.toHaveClass(/recording/, { timeout: 30000 });
    await expect(box(page)).toBeEnabled();
    await expect(sm(page, ".sm-speed-down")).toBeEnabled();
    await expect(sm(page, ".sm-speed")).not.toHaveAttribute("title", /.+/);
  });

  // One line where there is room; two at a phone's width and in a narrow
  // window. The speed stays inside the clock's card, the clock is not cut,
  // and nothing scrolls sideways.
  for (const [w, h, rows] of [[1920, 1080, 1], [1366, 768, 1], [800, 900, 1], [600, 700, 2], [375, 812, 2]]) {
    test(`${w} x ${h}: the speed fits beside the clock (${rows} ${rows === 1 ? "line" : "lines"})`, async ({ page }) => {
      await page.setViewportSize({ width: w, height: h });
      await openStudio(page);
      await openPerformance(page);
      await expect.poll(() => ev(page, async () => (await import("/js/streamer/streamer.js")).reviewState().tier)).toMatch(/^[abcs]$/);
      await page.waitForTimeout(300);
      const L = await ev(page, () => {
        const q = (s) => document.querySelector(`#streamer ${s}`);
        const b = (s) => q(s).getBoundingClientRect();
        const clock = b(".sm-clock");
        const speed = b(".sm-speed");
        const time = q(".sm-clock-time");
        const center = q(".sm-center");
        const doc = document.scrollingElement;
        return {
          speedInClock: speed.left >= clock.left - 0.5 && speed.right <= clock.right + 0.5 && speed.top >= clock.top - 0.5 && speed.bottom <= clock.bottom + 0.5,
          timeCut: time.scrollWidth > time.clientWidth + 1,
          timeUnderSpeed: time.getBoundingClientRect().right > speed.left + 0.5,
          clockBelowTitle: clock.top >= b(".sm-strip-title").bottom - 0.5,
          stripH: b(".sm-strip").height,
          cardH: b(".sm-strip-title").height,
          rowOverflow: q(".sm-strip-row").scrollWidth > q(".sm-strip-row").clientWidth + 1,
          centerX: center.scrollWidth > center.clientWidth + 1,
          pageX: doc.scrollWidth > doc.clientWidth,
        };
      });
      expect(L.speedInClock).toBe(true);
      expect(L.timeCut).toBe(false);
      expect(L.timeUnderSpeed).toBe(false);
      expect(L.rowOverflow).toBe(false);
      expect(L.centerX).toBe(false);
      expect(L.pageX).toBe(false);
      expect(L.clockBelowTitle).toBe(rows === 2);
      expect(L.stripH).toBeGreaterThan(rows * L.cardH - 0.5);
      expect(L.stripH).toBeLessThan((rows + 1) * L.cardH);
    });
  }
});
