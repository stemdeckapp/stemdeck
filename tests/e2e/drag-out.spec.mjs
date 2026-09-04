// Dragging a stem or a loop region out to a DAW or a folder.
//
// The OS drag itself cannot be tested from a browser: it is started in Rust
// (desktop/src-tauri/src/dragout.rs) and lands in another application. What is
// testable, and what actually breaks, is everything on this side of that call.
//
// Two things in particular:
//
// The grip sits inside #loop-region, which already runs its own pointer drag
// for moving the selection and adjusting both edges. An HTML5 drag and a
// pointer drag on one element fight, and the visible result is the region
// sliding away while it is being dragged out. The exclusion that prevents that
// is one line in transport.js and nothing else would notice if it were lost.
//
// And the payload. A dragged file lands in a folder nothing ever cleans up,
// and the Rust side reuses a file that is already there, so two regions of one
// song sharing a filename would mean the second drag silently handing over the
// first one's audio.

import { test, expect } from "@playwright/test";
import { openStudio } from "./helpers.mjs";

const GRIP = "[data-loop-drag-out]";

// Percentages of the ruler, so this does not depend on the fixture's duration.
async function markLoop(page, fromFrac = 0.2, toFrac = 0.6) {
  const ruler = await page.locator("#ruler-time").boundingBox();
  const y = ruler.y + ruler.height / 2;
  await page.mouse.move(ruler.x + ruler.width * fromFrac, y);
  await page.mouse.down();
  await page.mouse.move(ruler.x + ruler.width * toFrac, y, { steps: 8 });
  await page.mouse.up();
  await expect(page.locator("#t-loop")).toHaveClass(/active/);
}

const loopBounds = (page) =>
  page.evaluate(() => {
    const el = document.getElementById("loop-region");
    const parent = el.parentElement.getBoundingClientRect();
    const box = el.getBoundingClientRect();
    return {
      start: +((box.left - parent.left) / parent.width).toFixed(4),
      end: +((box.right - parent.left) / parent.width).toFixed(4),
    };
  });

// A real HTML5 drag cannot be driven from Playwright, and it is not what is
// under test: the delegated listener is. Dispatching the event it listens for
// exercises exactly the code this feature adds.
const fireDragStart = (page, selector) =>
  page.evaluate((sel) => {
    document.querySelector(sel).dispatchEvent(new DragEvent("dragstart", { bubbles: true }));
  }, selector);

const dragCalls = (page) => page.evaluate(() => window.__e2e.callsFor("start_audio_drag"));

test.describe("dragging audio out", () => {
  test("the grip appears only where a drag can actually happen", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await expect(page.locator("body")).toHaveClass(/can-drag-out/);
    await markLoop(page);
    await expect(page.locator(GRIP)).toBeVisible();
  });

  test("served in a browser, nothing offers a gesture that would do nothing", async ({ page }) => {
    await openStudio(page);
    await expect(page.locator("body")).not.toHaveClass(/can-drag-out/);
    await markLoop(page);
    // Present in the markup, but display:none without the class, so it can be
    // neither seen nor grabbed.
    await expect(page.locator(GRIP)).toBeHidden();
  });

  test("grabbing the grip does not drag the region with it", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await markLoop(page);
    const before = await loopBounds(page);

    const grip = await page.locator(GRIP).boundingBox();
    await page.mouse.move(grip.x + grip.width / 2, grip.y + grip.height / 2);
    await page.mouse.down();
    await page.mouse.move(grip.x + 220, grip.y + grip.height / 2, { steps: 10 });
    await page.mouse.up();

    expect(await loopBounds(page)).toEqual(before);
  });

  test("the region drag carries the loop bounds in its filename", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await markLoop(page, 0.2, 0.6);
    await fireDragStart(page, GRIP);

    const calls = await dragCalls(page);
    expect(calls).toHaveLength(1);
    const { url, filename } = calls[0].args;
    expect(url).toContain("/mixdown.wav");
    expect(url).toMatch(/[?&]start=/);
    expect(url).toMatch(/[?&]end=/);
    expect(filename).toMatch(/_region_[\d_]+-[\d_]+\.wav$/);
  });

  test("two different regions of one song are two different files", async ({ page }) => {
    await openStudio(page, { tauri: true });

    await markLoop(page, 0.1, 0.3);
    await fireDragStart(page, GRIP);
    await markLoop(page, 0.5, 0.9);
    await fireDragStart(page, GRIP);

    const [first, second] = await dragCalls(page);
    expect(first.args.filename).not.toBe(second.args.filename);
    expect(first.args.url).not.toBe(second.args.url);
  });

  test("a stem lane drags the stem, under the name its download uses", async ({ page }) => {
    await openStudio(page, { tauri: true });
    // Not simply the first lane: rows for stems that are absent keep href="#"
    // and are covered by the next test.
    const real = 'a.lane-dl:not([href="#"])';
    const lane = page.locator(real).first();
    await expect(lane).toHaveAttribute("download", /\.wav$/);
    const expected = await lane.getAttribute("download");

    await fireDragStart(page, real);

    const calls = await dragCalls(page);
    expect(calls).toHaveLength(1);
    // The name the click path saves under, not a second one derived here.
    // Deriving it again once prefixed the song title twice.
    expect(calls[0].args.filename).toBe(expected);
    expect(calls[0].args.url).toContain("/stems/");
  });

  test("a placeholder lane for an absent stem drags nothing", async ({ page }) => {
    await openStudio(page, { tauri: true });
    // It carries a download name but no real href. Handing that to the OS
    // would drag the page's own URL, which is worse than doing nothing.
    const placeholder = 'a.lane-dl[href="#"]';
    await expect(page.locator(placeholder).first()).toBeAttached();

    await fireDragStart(page, placeholder);

    expect(await dragCalls(page)).toHaveLength(0);
  });

  test("no loop, no drag", async ({ page }) => {
    await openStudio(page, { tauri: true });
    // The grip is inside the region, which is hidden until a loop is marked,
    // so there is nothing to grab and nothing is invoked.
    await expect(page.locator("#loop-region")).toHaveClass(/hidden/);
    expect(await dragCalls(page)).toHaveLength(0);
  });
});
