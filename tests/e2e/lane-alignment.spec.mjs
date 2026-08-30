// The mixer strip and the waveform lanes are two columns of the same rows, and
// a stem's name has to sit on the same line as its own waveform at any window
// size. Nothing enforced that: the mixer stack is built from --lane-h while the
// waveform column took its height from the multitrack, whose lane height is
// fixed when the tracks are created. After a resize the two disagreed and the
// error accumulated down the stack, so the bottom lane was the worst.
//
// These tests are all measurements of that disagreement.

import { test, expect } from "@playwright/test";
import { openStudio, waitForClickTrack } from "./helpers.mjs";

// Enough sizes to catch a rounding rule that only works for round numbers, and
// to cross the point where the lanes stop fitting and start overflowing (around
// 853 here) -- that transition is where the drift used to appear.
const HEIGHTS = [720, 768, 800, 853, 900, 947, 1000, 1013, 1080, 1150, 1237, 1300];

async function geometry(page) {
  return page.evaluate(() => {
    const mid = (el) => {
      const b = el.getBoundingClientRect();
      return { top: Math.round(b.top), mid: Math.round(b.top + b.height / 2) };
    };
    const mixRows = [...document.querySelectorAll(".mixer-column .lane-header:not(.hidden)")];
    const waveRows = [...document.querySelectorAll(".stem-waveform-row:not(.hidden)")];
    return {
      mixTops: mixRows.map((e) => mid(e).top),
      waveTops: waveRows.map((e) => mid(e).top),
      // The label the user actually reads, against the middle of the waveform
      // it belongs to.
      nameMids: mixRows.map((e) => {
        const n = e.querySelector(".mx-name");
        return n ? mid(n).mid : null;
      }),
      waveMids: waveRows.map((e) => mid(e).mid),
      column: Math.round(document.querySelector(".waves-column").getBoundingClientRect().height),
      stackVar: getComputedStyle(document.querySelector(".app"))
        .getPropertyValue("--wave-widget-track-stack-h").trim(),
    };
  });
}

test.describe("lane alignment", () => {
  test("every stem row lines up with its waveform at any window height", async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page);

    const offenders = [];
    for (const height of HEIGHTS) {
      await page.setViewportSize({ width: 1600, height });
      await page.waitForTimeout(450);
      const g = await geometry(page);

      const rowDrift = g.mixTops.map((t, i) => Math.abs((g.waveTops[i] ?? t) - t));
      // One pixel of rounding is fine; a row is 72px or more, so anything the
      // eye can see is far larger.
      const worstRow = Math.max(...rowDrift);
      // The name and its waveform share a centre line. Two pixels covers the
      // rounding in both measurements.
      const nameDrift = g.nameMids.map((m, i) => (m === null ? 0 : Math.abs((g.waveMids[i] ?? m) - m)));
      const worstName = Math.max(...nameDrift);

      if (worstRow > 1 || worstName > 2) {
        offenders.push({ height, worstRow, worstName, rowDrift, nameDrift });
      }
    }
    expect(offenders).toEqual([]);
  });

  test("the waveform column is the stack the mixer was built from", async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page);

    for (const height of [768, 900, 1150]) {
      await page.setViewportSize({ width: 1600, height });
      await page.waitForTimeout(450);
      const g = await geometry(page);
      // Taking its height from the multitrack instead is what let the two
      // columns disagree: at 768 after a resize from 900 the column stayed at
      // 498px against a 432px stack.
      expect(`${g.column}px`).toBe(g.stackVar);
    }
  });

  test("a stem name sits on its waveform centre line, not above it", async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 1013 });
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page);
    const g = await geometry(page);

    // The name and its meter are stacked and centred as a pair, which put the
    // name half a meter above the row's centre while the waveform beside it was
    // centred on that line. Invisible at 72px rows, obvious once a lane grows.
    for (const [i, nameMid] of g.nameMids.entries()) {
      if (nameMid === null) continue;
      expect(Math.abs(nameMid - g.waveMids[i])).toBeLessThanOrEqual(2);
    }
  });
});
