// The footer control strip has to stay inside the width it has.
//
// It is `overflow-x: auto` with `flex: none` children, so when it stops fitting
// it does not reflow or complain, it just scrolls, and controls sit off the
// right-hand edge with nothing to say so. Measured before the fix against a
// real six-stem track: the strip wants 1443px, so it overflowed by 88px on a
// maximized 1080p window and by 472px at the 1536px logical viewport a 4K panel
// gets at 250% Windows scaling (#586). Nothing in the suite asserted on layout
// width at all, which is why it shipped.
//
// These run at the config's 1280x720 unless a test sets its own viewport.
import { test, expect } from "@playwright/test";
import { openStudio, waitForClickTrack } from "./helpers.mjs";

const stripFits = (page) =>
  page.evaluate(() => {
    const el = document.querySelector(".footer-clusters");
    // Sub-pixel layout means these are not exactly equal even when they fit.
    return el.scrollWidth - el.clientWidth <= 1;
  });

const collapseLevels = (page) =>
  page.evaluate(() =>
    [...document.querySelector(".footer-clusters").classList].filter((c) =>
      c.startsWith("collapse-"),
    ),
  );

test.describe("footer fit", () => {
  test("a wide window leaves the strip exactly as it was", async ({ page }) => {
    await page.setViewportSize({ width: 2200, height: 900 });
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page);

    expect(await collapseLevels(page)).toEqual([]);
    // display: contents, so the options are peers of the on/off pill: the #269
    // layout, untouched for anyone who has room for it.
    const display = await page.evaluate(
      () => getComputedStyle(document.querySelector("#t-metro-panel")).display,
    );
    expect(display).toBe("contents");
    expect(await stripFits(page)).toBe(true);
  });

  test("the reporter's geometry no longer overflows", async ({ page }) => {
    // 4K at 250% Windows scaling. This is the window from the bug report.
    //
    // Asserts that it fits rather than that it collapsed: whether a given width
    // needs to collapse depends on the content, and the seeded fixture's strip
    // is narrower than a real six-stem track's. Pinning the class here would be
    // pinning the fixture, not the behaviour.
    await page.setViewportSize({ width: 1536, height: 864 });
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page);

    expect(await stripFits(page)).toBe(true);
  });

  test("collapsing keeps every click control visible and working", async ({ page }) => {
    // The point of wrapping rather than hiding: #269 decided these stay on
    // screen, and a narrow window must not quietly walk that back.
    await page.setViewportSize({ width: 1280, height: 800 });
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page);

    expect(await collapseLevels(page)).toContain("collapse-click");

    for (const id of ["#t-metro-countin", "#t-metro-bar", "#t-metro-edit", "#t-metro-half"]) {
      await expect(page.locator(id)).toBeVisible();
    }
    const half = page.locator("#t-metro-half");
    await half.click();
    await expect(half).toHaveAttribute("aria-checked", "true");
  });

  test("the strip opens back up when the room comes back", async ({ page }) => {
    // The fit re-measures from the uncollapsed state every time, so this is the
    // direction that catches a one-way ratchet.
    await page.setViewportSize({ width: 1280, height: 800 });
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page);
    expect(await collapseLevels(page)).toContain("collapse-click");

    await page.setViewportSize({ width: 2200, height: 900 });
    await expect.poll(() => collapseLevels(page), { timeout: 5000 }).toEqual([]);
    expect(await stripFits(page)).toBe(true);
  });

  test("the page itself never scrolls sideways", async ({ page }) => {
    for (const width of [2200, 1920, 1536, 1280]) {
      await page.setViewportSize({ width, height: 800 });
      await openStudio(page, { tauri: true });
      await waitForClickTrack(page);
      const overflows = await page.evaluate(
        () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
      );
      expect(overflows, `body scrolls sideways at ${width}px`).toBe(false);
    }
  });
});
