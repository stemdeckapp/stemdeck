// The practice core by hand: a loop dragged on the waveform or the ruler, I
// and O at the playhead, clearing the loop, and the transport shortcuts the
// Guide and the button titles name (QA catalogue PLY-10, PLY-11, KEY-01).
//
// Everything is done the way a person does it: the mouse drags across the
// lanes and the ruler, the keyboard presses the keys. Each check then reads
// both the screen (the loop region, the Loop button, the loop time fields, the
// footer clock) and the player state the other specs read (state.js and the
// transport clock), so a UI that looks right over a player that is not, or the
// other way round, fails.
//
// The song is the seeded fixture (tests/e2e/seed.py): real PCM16 stems, 6
// seconds long. Every span here fits inside it. What 6 seconds cannot show is
// whether a loop stays gapless over minutes; PLY-12 stays a human check.
import { test, expect } from "@playwright/test";
import { openStudio, waitForClickTrack } from "./helpers.mjs";

// The engine has to really play for "playback stays inside the loop" to mean
// anything. Muted, so a run makes no sound.
test.use({ launchOptions: { args: ["--mute-audio", "--autoplay-policy=no-user-gesture-required"] } });

// A drag lands on a pixel, and a pixel is a few milliseconds of a 6 second
// track at this width. 50 ms is far below anything a person would notice.
const DRAG_TOL_SEC = 0.05;
// The engine reports its clock between audio callbacks, so a sample taken
// just before the wrap can sit a little past the loop end.
const PLAY_TOL_SEC = 0.15;

async function player(page) {
  return page.evaluate(async () => {
    const s = await import("/js/state.js");
    const tx = (await import("/js/transport.js")).transport();
    return {
      loopEnabled: s.loopEnabled,
      loopStart: s.loopStart,
      loopEnd: s.loopEnd,
      duration: s.totalDuration,
      metronome: s.metronomeEnabled,
      time: tx.getCurrentTime(),
      playing: tx.isPlaying(),
    };
  });
}

/** "02.000" as the loop fields show it, back to seconds. */
const fieldSec = async (page, id) => Number((await page.locator(id).inputValue()).replace(/[^0-9.]/g, ""));

/** Where time `sec` is on screen. The ruler is the app's own time-to-pixel reference. */
async function xAt(page, sec) {
  const box = await page.locator("#ruler-time").boundingBox();
  const { duration } = await player(page);
  return box.x + (sec / duration) * box.width;
}

/** Middle of the lanes, where a person drags across the waveform. */
async function lanesY(page) {
  const box = await page.locator(".waves-column").boundingBox();
  return box.y + Math.min(box.height / 2, 40);
}

/** A point on the ruler itself, not on a tick label or the playhead marker. */
async function rulerY(page, x) {
  return page.evaluate((px) => {
    const ruler = document.getElementById("ruler-time");
    const r = ruler.getBoundingClientRect();
    for (let y = r.bottom - 2; y > r.top; y -= 1) {
      if (document.elementFromPoint(px, y) === ruler) return y;
    }
    return null;
  }, x);
}

async function drag(page, y, fromSec, toSec) {
  const x0 = await xAt(page, fromSec);
  const x1 = await xAt(page, toSec);
  await page.mouse.move(x0, y);
  await page.mouse.down();
  await page.mouse.move(x1, y, { steps: 12 });
  await page.mouse.up();
  return { x0, x1 };
}

/** Open the studio with nothing focused, so a key goes to the page. */
async function setUp(page) {
  await page.setViewportSize({ width: 1600, height: 900 });
  await openStudio(page);
  // openStudio returns once the transport shows a duration, which is before
  // the audio engine has read the stems. The click track is built only after
  // that, so its button coming on is the screen's own "ready to play".
  await waitForClickTrack(page, { revealOptions: false });
  // The "loading waveform" cover sits over the lanes for a moment longer and
  // takes the pointer while it does. A person waits for it to go; so does this.
  await expect(page.locator("#waveLoadingOverlay")).toBeHidden();
  // openStudio clicks a library row, which keeps the focus. A person's next
  // click lands on the lanes; blurring stands in for that without seeking.
  await page.evaluate(() => document.activeElement?.blur());
}

/** Record the transport clock while it plays, to see where playback went. */
async function startSampling(page) {
  await page.evaluate(async () => {
    const tr = await import("/js/transport.js");
    clearInterval(window.__loopSampler);
    window.__loopSamples = [];
    window.__loopSampler = setInterval(() => {
      const tx = tr.transport();
      if (tx?.isPlaying()) window.__loopSamples.push(tx.getCurrentTime());
    }, 20);
  });
}
const samples = (page) => page.evaluate(() => window.__loopSamples.slice());
const wraps = (s) => s.filter((t, i) => i > 0 && t < s[i - 1] - 0.5).length;

async function expectRegionOver(page, x0, x1) {
  const region = page.locator("#loop-region");
  await expect(region).toBeVisible();
  const box = await region.boundingBox();
  expect(Math.abs(box.x - Math.min(x0, x1))).toBeLessThan(3);
  expect(Math.abs(box.x + box.width - Math.max(x0, x1))).toBeLessThan(3);
}

test.describe("loop and transport by hand", () => {
  test("PLY-10 dragging across the waveform loops that part, and playback stays in it", async ({ page }) => {
    await setUp(page);
    const { x0, x1 } = await drag(page, await lanesY(page), 1, 4);

    // The screen: the region spans the drag, the Loop button is lit, and the
    // loop fields read the dragged times.
    await expectRegionOver(page, x0, x1);
    await expect(page.locator("#t-loop")).toHaveClass(/active/);
    expect(await fieldSec(page, "#t-loop-start")).toBeCloseTo(1, 1);
    expect(await fieldSec(page, "#t-loop-end")).toBeCloseTo(4, 1);
    // The player.
    const p = await player(page);
    expect(p.loopEnabled).toBe(true);
    expect(Math.abs(p.loopStart - 1)).toBeLessThan(DRAG_TOL_SEC);
    expect(Math.abs(p.loopEnd - 4)).toBeLessThan(DRAG_TOL_SEC);

    await startSampling(page);
    await page.locator("#t-play").click();
    await expect(page.locator("#t-play")).toHaveClass(/playing/);
    // Twice round the loop: the jump back has to have happened, not only the
    // first pass through it.
    await expect.poll(async () => wraps(await samples(page)), { timeout: 20_000 }).toBeGreaterThanOrEqual(2);
    const s = await samples(page);
    expect(Math.min(...s)).toBeGreaterThanOrEqual(p.loopStart - PLAY_TOL_SEC);
    expect(Math.max(...s)).toBeLessThanOrEqual(p.loopEnd + PLAY_TOL_SEC);

    // Loop turns it off, and L back on, with the same bounds.
    await page.locator("#t-loop").click();
    await expect(page.locator("#t-loop")).not.toHaveClass(/active/);
    await expect(page.locator("#loop-region")).toBeHidden();
    expect((await player(page)).loopEnabled).toBe(false);
    await page.evaluate(() => document.activeElement?.blur());
    await page.keyboard.press("l");
    await expect(page.locator("#t-loop")).toHaveClass(/active/);
    await expectRegionOver(page, x0, x1);
    const back = await player(page);
    expect(back.loopEnabled).toBe(true);
    expect(back.loopStart).toBeCloseTo(p.loopStart, 3);
    expect(back.loopEnd).toBeCloseTo(p.loopEnd, 3);
  });

  test("PLY-10 dragging on the ruler loops that part, right to left as well", async ({ page }) => {
    await setUp(page);
    const y = await rulerY(page, await xAt(page, 4.5));
    expect(y, "a point on the ruler that is not a tick label").not.toBeNull();
    // Backwards: the start and end are sorted, whichever way the drag went.
    const { x0, x1 } = await drag(page, y, 4.5, 2);

    await expectRegionOver(page, x0, x1);
    await expect(page.locator("#t-loop")).toHaveClass(/active/);
    expect(await fieldSec(page, "#t-loop-start")).toBeCloseTo(2, 1);
    expect(await fieldSec(page, "#t-loop-end")).toBeCloseTo(4.5, 1);
    const p = await player(page);
    expect(p.loopEnabled).toBe(true);
    expect(Math.abs(p.loopStart - 2)).toBeLessThan(DRAG_TOL_SEC);
    expect(Math.abs(p.loopEnd - 4.5)).toBeLessThan(DRAG_TOL_SEC);
  });

  test("PLY-10 a click on the waveform clears the loop and plays on past it", async ({ page }) => {
    await setUp(page);
    const y = await lanesY(page);
    await drag(page, y, 1, 2.5);
    await expect(page.locator("#loop-region")).toBeVisible();

    // A press that does not move, outside the region: a seek, which clears
    // the loop rather than leaving one armed behind it.
    await page.mouse.click(await xAt(page, 3), y);

    await expect(page.locator("#loop-region")).toBeHidden();
    await expect(page.locator("#t-loop")).not.toHaveClass(/active/);
    const p = await player(page);
    expect(p.loopEnabled).toBe(false);
    expect(Math.abs(p.time - 3)).toBeLessThan(DRAG_TOL_SEC);
    await expect(page.locator("#footer-time-elapsed")).toHaveText("00:03");

    // Playing from there runs on to the end instead of jumping back to 1 s.
    await startSampling(page);
    await page.keyboard.press("Space");
    await expect.poll(async () => Math.max(0, ...(await samples(page))), { timeout: 15_000 }).toBeGreaterThan(4.5);
    const s = await samples(page);
    expect(wraps(s)).toBe(0);
    expect(Math.min(...s)).toBeGreaterThanOrEqual(3 - PLAY_TOL_SEC);
  });

  test("PLY-11 I and O set the loop start and end at the playhead while playing", async ({ page }) => {
    await setUp(page);
    await drag(page, await lanesY(page), 0.5, 5.5);
    await expect(page.locator("#t-loop")).toHaveClass(/active/);

    await page.keyboard.press("Space");
    await expect(page.locator("#t-play")).toHaveClass(/playing/);

    // Where the playhead was at the moment each key went down, read in the
    // capture phase, before the app's own handler runs.
    await page.evaluate(async () => {
      const tr = await import("/js/transport.js");
      window.__pressedAt = {};
      window.addEventListener("keydown", (e) => {
        window.__pressedAt[e.code] = tr.transport().getCurrentTime();
      }, true);
    });
    const pressedAt = (code) => page.evaluate((c) => window.__pressedAt[c], code);

    // I, once the playhead is past 2 s.
    await expect.poll(async () => (await player(page)).time, { timeout: 15_000 }).toBeGreaterThan(2);
    await page.keyboard.press("i");
    const atI = await pressedAt("KeyI");
    const afterI = await player(page);
    expect(atI).toBeGreaterThan(2);
    expect(Math.abs(afterI.loopStart - atI)).toBeLessThan(0.02);
    expect(afterI.loopEnd).toBeCloseTo(5.5, 1);

    // O, a second and a half later. Once the end is behind the playhead,
    // playback jumps straight back to the new start.
    const inAt = afterI.loopStart;
    await expect.poll(async () => (await player(page)).time, { timeout: 15_000 }).toBeGreaterThan(inAt + 1.5);
    await page.keyboard.press("o");
    const atO = await pressedAt("KeyO");
    const afterO = await player(page);
    expect(atO).toBeGreaterThan(inAt + 1.5);
    expect(Math.abs(afterO.loopEnd - atO)).toBeLessThan(0.02);
    expect(afterO.loopStart).toBeCloseTo(inAt, 3);

    // The screen follows: the fields and the region show the new bounds.
    await expect.poll(() => fieldSec(page, "#t-loop-start")).toBeCloseTo(afterO.loopStart, 2);
    await expect.poll(() => fieldSec(page, "#t-loop-end")).toBeCloseTo(afterO.loopEnd, 2);
    await expectRegionOver(page, await xAt(page, afterO.loopStart), await xAt(page, afterO.loopEnd));

    // And playback now repeats the new loop.
    await startSampling(page);
    await expect.poll(async () => wraps(await samples(page)), { timeout: 15_000 }).toBeGreaterThanOrEqual(1);
    const s = await samples(page);
    expect(Math.min(...s)).toBeGreaterThanOrEqual(afterO.loopStart - PLAY_TOL_SEC);
    expect(Math.max(...s)).toBeLessThanOrEqual(afterO.loopEnd + PLAY_TOL_SEC);
  });

  test("PLY-11 scrolling over the loop times moves them", async ({ page }) => {
    await setUp(page);
    await drag(page, await lanesY(page), 1, 3);
    const before = await player(page);

    // Over the whole seconds of each field: a tenth of a second per notch.
    for (const [id, key] of [["#t-loop-start", "loopStart"], ["#t-loop-end", "loopEnd"]]) {
      const box = await page.locator(id).boundingBox();
      await page.mouse.move(box.x + box.width * 0.2, box.y + box.height / 2);
      await page.mouse.wheel(0, -100);
      await expect.poll(async () => (await player(page))[key]).toBeCloseTo(before[key] + 0.1, 3);
      await expect.poll(() => fieldSec(page, id)).toBeCloseTo(before[key] + 0.1, 2);
      await page.mouse.wheel(0, 100);
      await expect.poll(async () => (await player(page))[key]).toBeCloseTo(before[key], 3);
    }
  });

  test("KEY-01 Space plays and pauses", async ({ page }) => {
    await setUp(page);
    await page.keyboard.press("Space");
    await expect(page.locator("#t-play")).toHaveClass(/playing/);
    await expect.poll(async () => (await player(page)).time, { timeout: 15_000 }).toBeGreaterThan(0.5);
    expect((await player(page)).playing).toBe(true);

    await page.keyboard.press("Space");
    await expect(page.locator("#t-play")).not.toHaveClass(/playing/);
    const paused = await player(page);
    expect(paused.playing).toBe(false);
    // Paused means the clock holds still, over a few frames.
    const later = await page.evaluate(async () => {
      await new Promise((r) => setTimeout(r, 300));
      return (await import("/js/transport.js")).transport().getCurrentTime();
    });
    expect(later).toBeCloseTo(paused.time, 2);
  });

  test("KEY-01 [ and ] jump 5 seconds back and forward", async ({ page }) => {
    await setUp(page);
    const elapsed = page.locator("#footer-time-elapsed");
    expect((await player(page)).time).toBeCloseTo(0, 2);

    await page.keyboard.press("]");
    await expect.poll(async () => (await player(page)).time).toBeCloseTo(5, 2);
    await expect(elapsed).toHaveText("00:05");

    // Against the end of the 6 second track: clamped, not past it.
    await page.keyboard.press("]");
    await expect.poll(async () => (await player(page)).time).toBeCloseTo(6, 2);

    await page.keyboard.press("[");
    await expect.poll(async () => (await player(page)).time).toBeCloseTo(1, 2);
    await expect(elapsed).toHaveText("00:01");

    await page.keyboard.press("[");
    await expect.poll(async () => (await player(page)).time).toBeCloseTo(0, 2);
    await expect(elapsed).toHaveText("00:00");
  });

  test("KEY-01 L toggles the loop and K the click", async ({ page }) => {
    await setUp(page);

    // L with no loop drawn arms the default one, a quarter to a half of the song.
    await page.keyboard.press("l");
    await expect(page.locator("#t-loop")).toHaveClass(/active/);
    await expect(page.locator("#loop-region")).toBeVisible();
    let p = await player(page);
    expect(p.loopEnabled).toBe(true);
    expect(p.loopStart).toBeCloseTo(p.duration * 0.25, 2);
    expect(p.loopEnd).toBeCloseTo(p.duration * 0.5, 2);
    await page.keyboard.press("l");
    await expect(page.locator("#t-loop")).not.toHaveClass(/active/);
    await expect(page.locator("#loop-region")).toBeHidden();
    expect((await player(page)).loopEnabled).toBe(false);

    const metro = page.locator("#t-metro");
    await page.keyboard.press("k");
    await expect(metro).toHaveClass(/active/);
    await expect(metro).toHaveAttribute("aria-pressed", "true");
    expect((await player(page)).metronome).toBe(true);
    await page.keyboard.press("k");
    await expect(metro).not.toHaveClass(/active/);
    await expect(metro).toHaveAttribute("aria-pressed", "false");
    expect((await player(page)).metronome).toBe(false);
  });

  test("KEY-01 I and O do nothing with no loop on", async ({ page }) => {
    await setUp(page);
    await page.keyboard.press("]");
    await expect.poll(async () => (await player(page)).time).toBeCloseTo(5, 2);
    const before = await player(page);

    await page.keyboard.press("i");
    await page.keyboard.press("o");

    const after = await player(page);
    expect(after.loopEnabled).toBe(false);
    expect(after.loopStart).toBe(before.loopStart);
    expect(after.loopEnd).toBe(before.loopEnd);
    await expect(page.locator("#t-loop")).not.toHaveClass(/active/);
  });

  test("KEY-01 none of the shortcuts act while typing in a field", async ({ page }) => {
    await setUp(page);
    const before = await player(page);

    const search = page.locator("#catalogSearch");
    await search.click();
    await page.keyboard.type("[]lkio");
    await page.keyboard.press("Space");

    // Space is left out of the value on purpose: the next test is about where
    // it goes. This one is only about what the transport did.
    await expect(search).toHaveValue(/^\[\]lkio/);
    const after = await player(page);
    expect(after.playing).toBe(false);
    expect(after.time).toBeCloseTo(before.time, 2);
    expect(after.loopEnabled).toBe(before.loopEnabled);
    expect(after.metronome).toBe(before.metronome);
    await expect(page.locator("#t-play")).not.toHaveClass(/playing/);
    await expect(page.locator("#t-loop")).not.toHaveClass(/active/);
    await expect(page.locator("#t-metro")).not.toHaveClass(/active/);
  });

  // Known bug, found by this spec: the search box sits inside #catalogToggle,
  // a role="button" whose keydown handler (static/js/catalog.js,
  // wireCatalogToggle) calls preventDefault on Space for every key that
  // bubbles up from the box, so "daft punk" is typed as "daftpunk". The other
  // specs fill() the box, which sends no keys, so none of them could see it.
  // Marked as failing so the suite stays green while it is open; once the
  // handler ignores keys from the input, this reports an unexpected pass and
  // the test.fail comes off.
  // Known bug, found by this spec. The transport shows the song's length (from
  // its peaks) before the streaming engine has read the stems' headers, and
  // Play works from that moment. Pressed in that window, the engine's play()
  // runs with its duration still 0 (static/js/chunkedAudioEngine.js, play),
  // so its first tick takes the "reached the end" branch: nothing plays, and
  // onEnded fires the stemdeck:track-ended event, which in Karaoke moves the
  // queue on to the next song. Holding the stem downloads makes the window
  // wide enough to hit every time. Marked as failing until that is fixed.
  test.fail("KEY-01 Space pressed while the stems are still loading does not end the song", async ({ page }) => {
    let release;
    const held = new Promise((r) => { release = r; });
    await page.route("**/stems/*.wav", async (route) => {
      await held;
      await route.continue().catch(() => {});
    });
    await page.addInitScript(() => {
      window.__ended = 0;
      document.addEventListener("stemdeck:track-ended", () => window.__ended++);
    });
    await page.setViewportSize({ width: 1600, height: 900 });
    await openStudio(page);
    await page.evaluate(() => document.activeElement?.blur());

    await page.keyboard.press("Space");
    // The engine's clock runs on animation frames (tickLoop.js); ten frames
    // is several ticks.
    await page.evaluate(() => new Promise((r) => {
      let n = 0;
      const step = () => (++n >= 10 ? r() : requestAnimationFrame(step));
      requestAnimationFrame(step);
    }));
    const ended = await page.evaluate(() => window.__ended);
    release();
    expect(ended, "track-ended fired by a Play pressed during loading").toBe(0);
  });

  test.fail("KEY-01 a space typed in the library search box reaches it", async ({ page }) => {
    await setUp(page);
    const search = page.locator("#catalogSearch");
    await search.click();
    await page.keyboard.type("daft punk");
    await expect(search).toHaveValue("daft punk", { timeout: 2_000 });
  });
});
