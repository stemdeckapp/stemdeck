// The dot at the end of a library row.
//
// It used to be on every row: green for a finished import, which is what every
// row in a settled library is. A mark that never varies carries no information
// and is one more thing to read past on the way to the title (#636). It now
// appears only for the states that still say something.
//
// The first test is the behaviour a user sees. The second is a contract test
// on the stylesheet, and deliberately so: the dot is hidden by default and
// every state has to turn itself back on, so a state added later is invisible
// unless someone remembers that. Asserting the mapping is what remembers.
import { test, expect } from "@playwright/test";
import { openStudio } from "./helpers.mjs";

test.describe("library status dot", () => {
  test("a settled library shows no dots at all", async ({ page }) => {
    await openStudio(page);
    const dots = page.locator(".cat-item .cat-status");
    expect(await dots.count(), "rows exist to carry a dot").toBeGreaterThan(0);

    // Present in the DOM, not rendered. updateTrackStatus flips this element
    // live without a re-render, so removing it outright would cost that.
    for (let i = 0; i < (await dots.count()); i++) {
      await expect(dots.nth(i)).toBeHidden();
    }
  });

  test("every state that means something is visible", async ({ page }) => {
    await openStudio(page);
    const first = page.locator(".cat-item").first();

    for (const [onRow, onDot] of [
      [null, "processing"],
      [null, "unavailable"],
      ["queue-waiting", null],
    ]) {
      await first.evaluate(
        (el, [rowCls, dotCls]) => {
          el.className = `cat-item${rowCls ? " " + rowCls : ""}`;
          el.querySelector(".cat-status").className = `cat-status${dotCls ? " " + dotCls : ""}`;
        },
        [onRow, onDot],
      );
      await expect(
        first.locator(".cat-status"),
        `row="${onRow}" dot="${onDot}" should show a dot`,
      ).toBeVisible();
    }
  });
});
