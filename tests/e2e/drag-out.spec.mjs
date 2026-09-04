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

  test("grabbing a lane nugget does not redraw the loop", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await markLoop(page);
    const before = await loopBounds(page);

    // wireLoopDrag turns a drag anywhere on the waves column into a new
    // selection, and calls preventDefault, which kills the HTML5 drag before
    // dragstart fires. The nuggets live in the waveform overlay inside that
    // column, so without an explicit exclusion grabbing one silently
    // redefines the loop instead of dragging the stem out.
    const nugget = await page.locator("[data-lane-drag-out]").first().boundingBox();
    await page.mouse.move(nugget.x + nugget.width / 2, nugget.y + nugget.height / 2);
    await page.mouse.down();
    await page.mouse.move(nugget.x + 200, nugget.y, { steps: 10 });
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
    // Absolute, not a bare path. validate_download_url on the Rust side takes
    // only http/https, so a relative URL rejects every region drag and the
    // gesture does nothing at all, with the error going only to a console
    // nobody can see in a release build.
    expect(url).toMatch(/^https?:\/\//);
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
    expect(calls[0].args.url).toMatch(/^https?:\/\//);
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

  // One nugget per lane. The grip above them carries the mix, which is not
  // something a single bar could ever say on its own.

  test("every lane that drew something gets its own nugget", async ({ page }) => {
    await openStudio(page, { tauri: true });

    // A lane with no audio draws no waveform and has nothing to hand over.
    const drawn = await page.locator(".stem-waveform-row[data-stem] svg").count();
    expect(drawn).toBeGreaterThan(1);
    await expect(page.locator("[data-lane-drag-out]")).toHaveCount(drawn);

    // Present from the first draw, but nothing to drag until a loop exists.
    await expect(page.locator("[data-lane-drag-out]").first()).toBeHidden();
    await markLoop(page);
    await expect(page.locator("[data-lane-drag-out]").first()).toBeVisible();
  });

  // The first version of this gated the nuggets on the selection being wider
  // than 2% of the track, which hides them for exactly the short loops people
  // work with and does it silently. Note that the fixture is 6 seconds and
  // MIN_LOOP_SEC is 0.2s, so every loop it can make is already over 3% -- this
  // suite could not have caught that, and the gate is gone rather than tuned.
  test("the nuggets come back after the loop is redrawn several times", async ({ page }) => {
    await openStudio(page, { tauri: true });

    for (const [from, to] of [[0.1, 0.8], [0.2, 0.3], [0.6, 0.95], [0.4, 0.44]]) {
      await markLoop(page, from, to);
      await expect(page.locator("[data-lane-drag-out]").first()).toBeVisible();
    }
  });

  test("the nuggets survive a zoom, which redraws every row", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await markLoop(page);
    const before = await page.locator("[data-lane-drag-out]").count();

    // A redraw rewrites each row's innerHTML. Anything living in there is gone
    // unless it is put back, and the second wheel notch is where that shows.
    for (let i = 0; i < 6; i++) {
      await page.mouse.move(900, 400);
      await page.mouse.wheel(0, -240);
    }

    await expect(page.locator("[data-lane-drag-out]")).toHaveCount(before);
    await expect(page.locator("[data-lane-drag-out]").first()).toBeVisible();
  });

  test("a lane nugget drags that stem alone, at unity gain", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await markLoop(page);

    const nugget = page.locator("[data-lane-drag-out]").first();
    const stem = await nugget.getAttribute("data-lane-drag-out");
    await fireDragStart(page, "[data-lane-drag-out]");

    const calls = await dragCalls(page);
    expect(calls).toHaveLength(1);
    const url = new URL(calls[0].args.url);
    expect(url.searchParams.get("stems")).toBe(stem);
    // Its own stem, not the balance: the lane download gives the stem, and so
    // does this. The mix is the grip's job.
    expect(url.searchParams.get("gains")).toBe("1.000");
    expect(url.searchParams.get("start")).toBeTruthy();
    expect(url.searchParams.get("end")).toBeTruthy();
    expect(calls[0].args.filename).toContain(stem);
    expect(calls[0].args.filename).toMatch(/_region_[\d_]+-[\d_]+\.wav$/);
  });

  test("the drag carries a preview naming the lane", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await markLoop(page);
    await fireDragStart(page, "[data-lane-drag-out]");

    const { icon } = (await dragCalls(page))[0].args;
    // A null icon is not an error in Rust: it falls back to the app badge and
    // the drag still works, which is exactly why it has to be asserted here.
    // An earlier version rendered the lane's SVG glyph without an xmlns, so
    // the image never loaded and every drag silently carried the app badge.
    expect(typeof icon).toBe("string");
    expect(icon.length).toBeGreaterThan(100);
    // base64 of a PNG: every one of them starts with this signature.
    expect(icon.startsWith("iVBORw0KGgo")).toBe(true);
  });

  test("the mix grip keeps the app icon, so the two drags differ in flight", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await markLoop(page);
    await fireDragStart(page, GRIP);

    expect((await dragCalls(page))[0].args.icon).toBeNull();
  });

  test("a muted lane still drags its own stem", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await markLoop(page);
    const stem = await page.locator("[data-lane-drag-out]").first().getAttribute("data-lane-drag-out");
    await page.locator(`.lane-header[data-stem="${stem}"] .mute`).click();

    await fireDragStart(page, "[data-lane-drag-out]");

    // Muting is a monitoring choice. Asking for that stem explicitly is not
    // the same as asking for a mix it happens to be silent in.
    const url = new URL((await dragCalls(page))[0].args.url);
    expect(url.searchParams.get("stems")).toBe(stem);
  });

  // Mute and solo are not re-implemented for the drag: it goes through the same
  // _effectiveMixGains the Export menu uses, which drops a silent lane from the
  // stem list rather than summing it at zero. These guard that it stays wired
  // to that and does not drift into exporting whatever happens to be loaded.

  const laneNames = (url) => new URL(url).searchParams.get("stems").split(",");

  const clickLane = async (page, stem, control) =>
    page.locator(`.lane-header[data-stem="${stem}"] .${control}`).click();

  test("a muted lane is left out of the region entirely", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await markLoop(page);
    await fireDragStart(page, GRIP);
    const before = laneNames((await dragCalls(page))[0].args.url);
    expect(before.length).toBeGreaterThan(1);

    const victim = before[0];
    await clickLane(page, victim, "mute");
    await fireDragStart(page, GRIP);

    const after = laneNames((await dragCalls(page))[1].args.url);
    // Absent, not present at gain 0. Summing silence is still summing.
    expect(after).not.toContain(victim);
    expect(after).toEqual(before.filter((n) => n !== victim));
  });

  test("soloing one lane drags only that lane", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await markLoop(page);
    await fireDragStart(page, GRIP);
    const all = laneNames((await dragCalls(page))[0].args.url);
    expect(all.length).toBeGreaterThan(1);

    const chosen = all[1];
    await clickLane(page, chosen, "solo");
    await fireDragStart(page, GRIP);

    expect(laneNames((await dragCalls(page))[1].args.url)).toEqual([chosen]);
  });

  test("no loop, no drag", async ({ page }) => {
    await openStudio(page, { tauri: true });
    // The grip is inside the region, which is hidden until a loop is marked,
    // so there is nothing to grab and nothing is invoked.
    await expect(page.locator("#loop-region")).toHaveClass(/hidden/);
    expect(await dragCalls(page)).toHaveLength(0);
  });
});
