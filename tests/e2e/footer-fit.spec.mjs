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
import { openStudio, seedLibrary, stubUpdateCheck, waitForClickTrack } from "./helpers.mjs";

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
    await waitForClickTrack(page, { revealOptions: false });

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
    await waitForClickTrack(page, { revealOptions: false });

    expect(await stripFits(page)).toBe(true);
  });

  test("a row that is only a little short keeps the options inline", async ({ page }) => {
    // The first thing a short row does is wrap the options to a measured
    // width, not take them away: #269 decided they stay on screen, and being
    // 100px short of the space for one long line is not a reason to walk that
    // back.
    //
    // Asserts the property rather than the collapse class. Whether this width
    // needs to wrap at all depends on the content, and the seeded fixture's
    // strip is narrower than a real six-stem track's.
    await page.setViewportSize({ width: 1728, height: 900 });
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page, { revealOptions: false });

    await expect(page.locator("#t-metro-bar")).toBeVisible();
    await expect(page.locator("#t-metro-more")).toBeHidden();
    expect(await stripFits(page)).toBe(true);
  });

  test("collapsing puts every click control one click away, not out of reach", async ({ page }) => {
    // Past the width two rows can absorb, the options move into a popover,
    // opened from a disclosure beside the on/off pill. Nothing is removed and
    // nothing stops working --
    // that is the whole difference between this and hiding them.
    await page.setViewportSize({ width: 1280, height: 800 });
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page, { revealOptions: false });

    expect(await collapseLevels(page)).toContain("collapse-click");

    const more = page.locator("#t-metro-more");
    await expect(more).toBeVisible();
    await expect(page.locator("#t-metro-bar")).toBeHidden();

    await more.click();
    for (const id of ["#t-metro-countin", "#t-metro-bar", "#t-metro-edit", "#t-metro-half"]) {
      await expect(page.locator(id)).toBeVisible();
    }
    const half = page.locator("#t-metro-half");
    await half.click();
    await expect(half).toHaveAttribute("aria-checked", "true");
    // Still open: a rate button is not a reason to close the panel you picked
    // it from.
    await expect(page.locator("#t-metro-bar")).toBeVisible();

    await page.keyboard.press("Escape");
    await expect(page.locator("#t-metro-bar")).toBeHidden();
    await expect(more).toHaveAttribute("aria-expanded", "false");
  });

  test("the options popover is a solid panel above its button with no track loaded", async ({ page }) => {
    // #697. With nothing loaded the options are .unavailable, which faded the
    // whole panel to 40% and made it click-through. Inline that is right. As
    // the popover it faded the box too, leaving a see-through ghost over the
    // lanes that read as misplaced, and a click inside it hit the lane below.
    await page.setViewportSize({ width: 1280, height: 800 });
    await seedLibrary(page);
    await stubUpdateCheck(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await expect.poll(() => collapseLevels(page), { timeout: 5000 }).toContain("collapse-click");

    const more = page.locator("#t-metro-more");
    await more.click();
    const panel = page.locator("#t-metro-panel");
    await expect(panel).toHaveClass(/\bunavailable\b/);
    await expect(panel).toBeVisible();

    const geo = await page.evaluate(() => {
      const p = document.querySelector("#t-metro-panel");
      const pr = p.getBoundingClientRect();
      const br = document.querySelector("#t-metro-more").getBoundingClientRect();
      const hit = document.elementFromPoint(pr.left + 4, pr.top + 4);
      return {
        opacity: getComputedStyle(p).opacity,
        above: pr.bottom <= br.top,
        overlapsX: pr.left < br.right && pr.right > br.left,
        hitsPanel: p.contains(hit),
      };
    });
    expect(geo).toEqual({ opacity: "1", above: true, overlapsX: true, hitsPanel: true });

    // A click on the panel itself is not a click away from it.
    await panel.click({ position: { x: 4, y: 4 } });
    await expect(more).toHaveAttribute("aria-expanded", "true");
  });

  test("the strip opens back up when the room comes back", async ({ page }) => {
    // The fit re-measures from the uncollapsed state every time, so this is the
    // direction that catches a one-way ratchet.
    await page.setViewportSize({ width: 1280, height: 800 });
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page, { revealOptions: false });
    expect(await collapseLevels(page)).toContain("collapse-click");

    await page.setViewportSize({ width: 2200, height: 900 });
    await expect.poll(() => collapseLevels(page), { timeout: 5000 }).toEqual([]);
    expect(await stripFits(page)).toBe(true);
  });

  // Deleted: "the page itself never scrolls sideways".
  //
  // It could not fail. `html, body { overflow: hidden }` (daw.css:7) means the
  // document has no horizontal scroll to report whatever the footer does, and
  // .footer-clusters is its own `overflow-x: auto` box so its overflow never
  // reaches an ancestor's scrollWidth either. Verified by disabling the fix
  // entirely: it still passed, at the cost of four full openStudio loads.
  //
  // "the reporter's geometry no longer overflows" above is the one that
  // actually fails without the fix, and it is the same property measured
  // where it is observable.
});
