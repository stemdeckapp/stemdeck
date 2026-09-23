// The Extract row is one line at every width, and folds its tail into a button
// rather than wrapping.
//
// Chips used to wrap to a second line as the window narrowed, which moved
// everything below them (#668). They now fold from the end into a popover
// behind a three dots button, and come back on the way out. Nothing tested
// any of that, and two separate bugs shipped underneath it:
//
//   - The row declares `flex-wrap: nowrap` for itself, and a leftover rule
//     from the earlier composer layout set `flex-wrap: wrap` one class deeper.
//     The more specific rule won, so the row never stopped wrapping. English
//     fit anyway; French and Polish did not, and went to two lines at 1280
//     (#673).
//   - The fold measured the row before revealing the button, so it kept one
//     chip too many: the button took its width out of the row the moment it
//     appeared, and the chip measured as fitting no longer fit.
//
// Both are invisible in English at a wide window, which is the state a
// maintainer looks at. Hence the languages below.
import { test, expect } from "@playwright/test";
import { openStudio } from "./helpers.mjs";

// A chip is 30px. Anything above this is a second line.
const ONE_LINE = 34;

const row = (page) =>
  page.evaluate(() => {
    const el = document.getElementById("stemChips");
    const btn = document.getElementById("stemMoreBtn");
    const panel = document.getElementById("stemOverflow");
    return {
      height: Math.round(el.getBoundingClientRect().height),
      // Overflow is hidden, so this is the only way to see a chip cut in half.
      clipped: Math.round(el.scrollWidth - el.clientWidth),
      inRow: el.querySelectorAll(".stem-choice, .stem-group").length,
      folded: panel.querySelectorAll(".stem-choice, .stem-group").length,
      moreShown: !btn.hidden,
    };
  });

async function atWidth(page, width) {
  await page.setViewportSize({ width, height: 900 });
  await page.waitForTimeout(400);
  return row(page);
}

const openIn = async (page, lang) => {
  await page.addInitScript(
    (l) => localStorage.setItem("stemdeck.language", JSON.stringify(l)),
    lang,
  );
  await openStudio(page, { tauri: true });
};

test.describe("extract row overflow", () => {
  test("a wide window folds nothing and hides the button", async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    await openIn(page, "en");

    const s = await atWidth(page, 1600);
    expect(s.folded).toBe(0);
    expect(s.moreShown).toBe(false);
    expect(s.height).toBeLessThanOrEqual(ONE_LINE);
  });

  test("narrowing folds from the end, and the row stays one line", async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    await openIn(page, "en");

    let previous = 0;
    for (const width of [1600, 1280, 1100, 1024, 960, 900]) {
      const s = await atWidth(page, width);
      expect(s.height, `two lines at ${width}`).toBeLessThanOrEqual(ONE_LINE);
      // The failure the budget bug produced: the fold ran, and the row was
      // still a chip wider than the space left over once the button appeared.
      expect(s.clipped, `a chip is cut off at ${width}`).toBeLessThanOrEqual(1);
      expect(s.folded, `folding went backwards at ${width}`).toBeGreaterThanOrEqual(previous);
      if (s.folded > 0) expect(s.moreShown, `nothing offers the folded chips at ${width}`).toBe(true);
      previous = s.folded;
    }
    // Something must have folded by 900, or this test is not exercising it.
    expect(previous).toBeGreaterThan(0);
  });

  test("widening gives every chip back", async ({ page }) => {
    // The fitter used to observe the row it was emptying, so once the row had
    // collapsed it never saw the window grow again (#671).
    await page.setViewportSize({ width: 1600, height: 900 });
    await openIn(page, "en");

    const narrow = await atWidth(page, 900);
    expect(narrow.folded).toBeGreaterThan(0);

    const wide = await atWidth(page, 1600);
    expect(wide.folded).toBe(0);
    expect(wide.moreShown).toBe(false);
    expect(wide.inRow).toBe(narrow.inRow + narrow.folded);
  });

  test("it holds one line in every language, not just English", async ({ page }) => {
    // English is the shortest of the ten and the only one anyone checks by eye.
    const offenders = [];
    for (const lang of ["en", "de", "pl", "pt", "fr", "es", "id", "ja", "ko", "zh-Hans"]) {
      await page.setViewportSize({ width: 1600, height: 900 });
      await openIn(page, lang);
      for (const width of [1440, 1280, 1100, 1024]) {
        const s = await atWidth(page, width);
        if (s.height > ONE_LINE || s.clipped > 1) {
          offenders.push({ lang, width, height: s.height, clipped: s.clipped });
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  test("a settled row stops moving chips", async ({ page }) => {
    // The fitter is driven by a ResizeObserver on the composer, and it now
    // changes layout while it measures: it hides and shows the button, and
    // folds one more chip at a time until the row is honest. Each of those is
    // a size change the observer can hear, so a wrong stopping condition is an
    // infinite loop that pins a core rather than a visible bug.
    await page.setViewportSize({ width: 1100, height: 900 });
    await openIn(page, "de");
    await page.waitForTimeout(800);

    const moves = await page.evaluate(async () => {
      let n = 0;
      const seen = new MutationObserver((records) => {
        n += records.length;
      });
      seen.observe(document.getElementById("stemChips"), { childList: true });
      seen.observe(document.getElementById("stemOverflow"), { childList: true });
      await new Promise((r) => setTimeout(r, 1500));
      seen.disconnect();
      return n;
    });
    expect(moves).toBe(0);
  });

  test("a folded chip still selects its stem", async ({ page }) => {
    // Folding is a place to put a control, not a way to retire it. The
    // listeners are bound per chip rather than delegated from the row, which is
    // what lets a chip keep working somewhere else in the document.
    await page.setViewportSize({ width: 1600, height: 900 });
    await openIn(page, "en");
    await atWidth(page, 900);

    const pressed = () =>
      page.evaluate(() =>
        [...document.querySelectorAll(".stem-choice[data-stem]")]
          .filter((b) => b.getAttribute("aria-pressed") === "true")
          .map((b) => b.dataset.stem)
          .sort(),
      );

    const chip = page.locator("#stemOverflow .stem-choice[data-stem]").first();
    const stem = await chip.getAttribute("data-stem");
    const before = await pressed();

    await page.locator("#stemMoreBtn").click();
    await chip.click();

    // Pressing a chip from the all-selected default means "only this one", so
    // what is asserted is that the press was heard, not that it toggled.
    const after = await pressed();
    expect(after).not.toEqual(before);
    expect(after).toContain(stem);
  });
});
