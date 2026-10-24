// The mixer strip and the waveform lanes are two columns of the same rows, and
// a stem's name has to sit on the same line as its own waveform at any window
// size. Nothing enforced that: the mixer stack is built from --lane-h while the
// waveform column took its height from the multitrack, whose lane height is
// fixed when the tracks are created. After a resize the two disagreed and the
// error accumulated down the stack, so the bottom lane was the worst.
//
// These tests are all measurements of that disagreement.
//
// Since the side-rail header (#776) the name sits above its fader, so it is a
// fixed distance above the waveform's centre line rather than on it. What
// must not happen is that distance changing from row to row or with the
// window: that is the drift these tests exist for.

import { test, expect } from "@playwright/test";
import { openStudio, waitForClickTrack } from "./helpers.mjs";

// Enough sizes to catch a rounding rule that only works for round numbers, and
// to cross the point where the lanes stop fitting and start overflowing (around
// 853 here) -- that transition is where the drift used to appear.
const HEIGHTS = [720, 768, 800, 853, 900, 947, 1000, 1013, 1080, 1150, 1237, 1300];

// How far each name sits from its waveform's centre line (signed, px), and
// how much that distance varies across the rows. Rows with no name (an EQ
// panel) are left out.
function nameOffsets(g) {
  const off = g.nameMids.flatMap((m, i) => (m === null || g.waveMids[i] === undefined ? [] : [m - g.waveMids[i]]));
  return { off, spread: off.length ? Math.max(...off) - Math.min(...off) : 0 };
}

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
    const perHeight = [];
    for (const height of HEIGHTS) {
      await page.setViewportSize({ width: 1600, height });
      await page.waitForTimeout(450);
      const g = await geometry(page);

      const rowDrift = g.mixTops.map((t, i) => Math.abs((g.waveTops[i] ?? t) - t));
      // One pixel of rounding is fine; a row is 72px or more, so anything the
      // eye can see is far larger.
      const worstRow = Math.max(...rowDrift);
      // Every name the same distance from its waveform's centre line. Two
      // pixels covers the rounding in both measurements.
      const { off, spread } = nameOffsets(g);
      perHeight.push(off[0]);

      if (worstRow > 1 || spread > 2) {
        offenders.push({ height, worstRow, nameSpread: spread, rowDrift, nameOffsets: off });
      }
    }
    expect(offenders).toEqual([]);
    // And the same distance at every window height.
    expect(Math.max(...perHeight) - Math.min(...perHeight)).toBeLessThanOrEqual(2);
  });

  // An open EQ panel is a row of its own in both columns (static/js/eq/),
  // taller than a lane and of a fixed height: the lanes share what it leaves.
  // The same measurement as above, with two panels open.
  test("every row lines up with EQ panels open, at any window height", async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page);
    for (const lane of ["drums", "bass"]) {
      await page.locator(`.eq-tab[data-eq-lane="${lane}"]`).click();
      await expect(page.locator(`.eq-panel-lane[data-eq-lane="${lane}"]`)).toHaveCount(1);
    }

    const offenders = [];
    for (const height of HEIGHTS) {
      await page.setViewportSize({ width: 1600, height });
      await page.waitForTimeout(450);
      const g = await geometry(page);
      const worstRow = Math.max(...g.mixTops.map((t, i) => Math.abs((g.waveTops[i] ?? t) - t)));
      const { spread } = nameOffsets(g);
      if (g.mixTops.length !== g.waveTops.length || worstRow > 1 || spread > 2) {
        offenders.push({ height, rows: [g.mixTops.length, g.waveTops.length], worstRow, nameSpread: spread });
      }
      expect(`${g.column}px`).toBe(g.stackVar);
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

  test("every stem name sits the same distance from its waveform centre line", async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 1013 });
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page);
    const g = await geometry(page);

    // #665 put the name on the centre line; the side-rail header (#776) puts
    // it above its fader instead. Either way it is one offset for every row,
    // so a name never wanders off its own lane as the lanes grow.
    const { off, spread } = nameOffsets(g);
    expect(off.length).toBeGreaterThan(1);
    expect(spread).toBeLessThanOrEqual(2);
  });
  // The first track of a session is the one whose lanes are built before the
  // click-track hint has arrived under the footer. That hint is text from the
  // beat analysis: it used to be display: none until it had something to say,
  // and then take 22px off the wave panel when it did. The lanes had already
  // baked in the taller row by then, while the mixer follows --lane-h and moved
  // to the shorter one, so the two drifted 3px a row down the stack.
  //
  // Reserving its line is the fix, so this asserts the thing that made the
  // difference: the wave panel is the same height before and after the hint
  // arrives, and the first track lines up like any other.
  test("the first track of a session lines up like the second", async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page, { revealOptions: false });

    const first = await geometry(page);
    expect(first.waveTops).toEqual(first.mixTops);
  });

  test("the click-track hint holds its line while it is empty", async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    await openStudio(page, { tauri: true });

    // Whatever it says, and whether it says anything at all, it occupies the
    // same space. An empty one that collapses is a layout change arriving a
    // second after the track opens, which is what the lanes cannot survive.
    const { empty, filled } = await page.evaluate(() => {
      const note = document.querySelector(".metro-note");
      const was = note.textContent;
      note.textContent = "";
      const empty = Math.round(note.getBoundingClientRect().height);
      note.textContent = was || "Clicking 120.0 BPM";
      const filled = Math.round(note.getBoundingClientRect().height);
      note.textContent = was;
      return { empty, filled };
    });
    expect(empty).toBe(filled);
  });
});
