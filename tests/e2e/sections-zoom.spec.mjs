// A section boundary has to sit on the same pixel column as the audio it marks.
//
// The ribbon positions blocks as a percentage of the track, but it used to do
// that inside a box the width of the window, while the waveform and ruler grow
// with `--zoom` and scroll. So the moment anyone zoomed, the two disagreed.
// Measured on a real 11-minute track at 2.7x, the first boundary sat 775px away
// from the moment it marked, and the drag maths, which measures the same box,
// moved a block 2.7x too far for the same gesture (#573).
//
// The ruler already solved this: a left-anchored inner track of
// `calc(100% * var(--zoom))`, translated by the wave's scrollLeft. This asserts
// the ribbon now does the same.
import { test, expect } from "@playwright/test";
import { openStudio, JOB_ID } from "./helpers.mjs";

// Gaps between them on purpose: a block hard against its neighbour is refused
// by _clampMove, which would make a broken drag look like a working one.
const SECTIONS = [
  { id: "s1", name: "Intro", start: 0, end: 1, color: "#4a7fff" },
  { id: "s2", name: "Verse", start: 2, end: 3, color: "#22c55e" },
  { id: "s3", name: "Chorus", start: 4, end: 5, color: "#f97316" },
];

async function seedSections(page) {
  const res = await page.request.patch(`/api/jobs/${JOB_ID}/sections`, {
    data: { sections: SECTIONS },
  });
  expect(res.ok(), "seeding sections").toBe(true);
}

/** How far each block's left edge is from where that time lands on the ruler. */
const alignment = (page) =>
  page.evaluate(() => {
    const ruler = document.querySelector(".lanes-ruler-time").getBoundingClientRect();
    return [...document.querySelectorAll(".section-block")].map((el) => {
      const fraction = parseFloat(el.style.left) / 100;
      const wantX = ruler.left + fraction * ruler.width;
      return Math.round(el.getBoundingClientRect().left - wantX);
    });
  });

const zoomLevel = (page) =>
  page.evaluate(
    () => parseFloat(getComputedStyle(document.getElementById("lanes")).getPropertyValue("--zoom")) || 1,
  );

async function zoomIn(page, notches) {
  const box = await page.locator("#wave-scroll").boundingBox();
  for (let i = 0; i < notches; i++) {
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.wheel(0, -120);
    await page.waitForTimeout(200);
  }
}

test.describe("sections and zoom", () => {
  test("boundaries stay on the audio they mark as the timeline zooms", async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 950 });
    await seedSections(page);
    await openStudio(page, { tauri: true });
    await page.waitForSelector(".section-block", { timeout: 20000 });

    // Unzoomed is the baseline. Compared against it rather than against zero,
    // because the ribbon and the ruler can sit at slightly different origins
    // and that constant is not what this is about: the bug is that zooming
    // *introduced* drift on top of it.
    const base = await alignment(page);

    await zoomIn(page, 4);
    expect(await zoomLevel(page)).toBeGreaterThan(1);

    // Every boundary, including any scrolled past the left edge, which is where
    // the old behaviour drifted furthest.
    const zoomed = await alignment(page);
    zoomed.forEach((off, i) => {
      expect(Math.abs(off - base[i]), `boundary ${i} drifted when zoomed`).toBeLessThanOrEqual(1);
    });
  });

  test("the ribbon scrolls with the waveform", async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 950 });
    await seedSections(page);
    await openStudio(page, { tauri: true });
    await page.waitForSelector(".section-block", { timeout: 20000 });
    const base = await alignment(page);
    await zoomIn(page, 4);

    const shift = () =>
      page.evaluate(() => {
        const track = document.getElementById("daw-sections-track");
        const ruler = document.querySelector(".lanes-ruler-time");
        return {
          track: track.style.transform,
          ruler: ruler.style.transform,
          scrollLeft: Math.round(document.getElementById("wave-scroll").scrollLeft),
        };
      });

    await page.evaluate(() => {
      const s = document.getElementById("wave-scroll");
      s.scrollLeft = Math.round(s.scrollWidth / 3);
      s.dispatchEvent(new Event("scroll"));
    });
    await page.waitForTimeout(250);

    const after = await shift();
    expect(after.scrollLeft, "the wave actually scrolled").toBeGreaterThan(0);
    // Same translate as the ruler: they are two strips over one timeline.
    expect(after.track).toBe(after.ruler);
    const scrolled = await alignment(page);
    scrolled.forEach((off, i) => {
      expect(Math.abs(off - base[i]), `boundary ${i} drifted when scrolled`).toBeLessThanOrEqual(1);
    });
  });

  test("focusing a control cannot scroll the ribbon out of alignment", async ({ page }) => {
    // The ribbon is positioned by transform and nothing resets scrollLeft, so
    // if its container is scrollable at all, one focus is enough to offset it
    // from the waveform permanently. Renaming a section past the right edge
    // does exactly that: the browser scrolls the box to reveal the input.
    //
    // Caught in review of this change, where the container was `overflow:
    // hidden`, which is a scroll container. It scrolled 521px.
    await page.setViewportSize({ width: 1500, height: 620 });
    await seedSections(page);
    await openStudio(page, { tauri: true });
    await page.waitForSelector(".section-block", { timeout: 20000 });
    await zoomIn(page, 5);

    const base = await alignment(page);
    const scrolled = await page.evaluate(() => {
      const area = document.querySelector(".daw-sections-area");
      const last = [...document.querySelectorAll(".section-block")].pop();
      const input = document.createElement("input");
      last.appendChild(input);
      input.focus();
      const left = area.scrollLeft;
      input.remove();
      return left;
    });

    expect(scrolled, "the ribbon must not be scrollable").toBe(0);
    const after = await alignment(page);
    after.forEach((off, i) => {
      expect(Math.abs(off - base[i]), `boundary ${i} drifted after a focus`).toBeLessThanOrEqual(1);
    });
  });

  test("dragging a section moves it by what the cursor travelled", async ({ page }) => {
    // The other half of the bug. The drag converts pixels to seconds using the
    // width of the element the blocks live in, so before the fix a zoomed drag
    // moved the block by the zoom factor too far.
    await page.setViewportSize({ width: 1600, height: 950 });
    await seedSections(page);
    await openStudio(page, { tauri: true });
    await page.waitForSelector(".section-block", { timeout: 20000 });
    await zoomIn(page, 3);

    // The ruler is the ground truth for how wide the zoomed timeline is. Using
    // the ribbon's own width instead would be self-consistent and prove
    // nothing: the bug was that the ribbon's width disagreed with the
    // timeline's, and a drag converted pixels to seconds using the wrong one.
    const rulerWidth = await page.evaluate(
      () => document.querySelector(".lanes-ruler-time").getBoundingClientRect().width,
    );
    // A block that is fully on screen at this zoom. Zooming keeps the cursor's
    // position fixed, so the earliest sections scroll off the left, and a drag
    // aimed at one of those never starts and would read as a passing zero.
    const area = await page.locator("#daw-sections").boundingBox();
    const count = await page.locator(".section-block").count();
    let block = null;
    let box = null;
    for (let i = 0; i < count; i++) {
      const candidate = page.locator(".section-block").nth(i);
      const bb = await candidate.boundingBox();
      if (bb && bb.x > area.x + 10 && bb.x + bb.width < area.x + area.width - 60) {
        block = candidate;
        box = bb;
        break;
      }
    }
    expect(block, "a section fully on screen to drag").not.toBeNull();
    const before = await block.evaluate((el) => parseFloat(el.style.left));

    const dx = 40;
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width / 2 + dx, box.y + box.height / 2, { steps: 10 });
    await page.mouse.up();
    await page.waitForTimeout(200);

    const after = await block.evaluate((el) => parseFloat(el.style.left));

    // Dragging dx pixels must move the section by the slice of the track those
    // pixels cover on the zoomed timeline. Before the fix the conversion used
    // the unzoomed ribbon width, so the block followed the cursor on screen but
    // landed on a moment `zoom` times further through the song.
    const movedFraction = (after - before) / 100;
    const wantFraction = dx / rulerWidth;
    expect(Math.abs(movedFraction - wantFraction)).toBeLessThanOrEqual(0.002);
  });
});
