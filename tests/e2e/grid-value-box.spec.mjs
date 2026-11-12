// Grid editor value boxes (#803): every value the toolbar shows must be fully
// readable, spinner included. The Bar box is the only value-carrying control
// (Ripple and Snap are checkboxes), and its width was 4ch with the browser's
// spin button taking a slice of it, so two digits were clipped.
//
// scrollWidth vs clientWidth cannot see this: a number input's text lives in a
// shadow inner editor, so the input itself never reports overflow. Instead the
// test measures the digits with the input's own computed font and compares
// them to the room left once padding, borders and the spin button (15px in
// Chromium, reserved even while it is invisible) are taken out.

import { test, expect } from "@playwright/test";
import { openStudio, waitForClickTrack } from "./helpers.mjs";

const SPIN = 15;
// Toolbar height per window width on unfixed big-release: the controls row
// already wraps in the narrow wave column, so the fix must not add a row.
const HEIGHT = { 1440: 63, 1280: 95, 1024: 127 };

async function measure(page, value) {
  const input = page.locator("#bg-barlen");
  await input.fill(String(value));
  await input.hover();
  return input.evaluate((el) => {
    const cs = getComputedStyle(el);
    const ctx = document.createElement("canvas").getContext("2d");
    ctx.font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
    const text = ctx.measureText(el.value).width;
    const pad = parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight);
    const room = el.clientWidth - pad;
    return { value: el.value, width: el.getBoundingClientRect().width, clientWidth: el.clientWidth, scrollWidth: el.scrollWidth, text, room };
  });
}

async function openGrid(page) {
  await openStudio(page, { tauri: true });
  await waitForClickTrack(page);
  await page.locator("#t-metro-edit").click();
  await expect(page.locator("#beatgrid-toolbar")).not.toHaveClass(/hidden/);
}

test.describe("grid value boxes", () => {
  test("the Bar box shows a two-digit value in full", async ({ page }) => {
    await openGrid(page);
    for (const v of [4, 12, 32]) {
      const m = await measure(page, v);
      console.log("MEASURE", JSON.stringify(m));
      expect.soft(m.text, `digits of ${v} fit beside the spinner`).toBeLessThanOrEqual(m.room - SPIN);
      expect.soft(m.scrollWidth, `${v} does not scroll inside the box`).toBeLessThanOrEqual(m.clientWidth);
    }
  });

  for (const width of [1440, 1280, 1024]) {
    test(`toolbar does not overflow at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 800 });
      await openGrid(page);
      await measure(page, 32);
      const r = await page.locator("#beatgrid-toolbar").evaluate((el) => {
        const box = el.getBoundingClientRect();
        const bar = el.querySelector("#bg-barlen").getBoundingClientRect();
        return {
          over: el.scrollWidth - el.clientWidth,
          docOver: document.documentElement.scrollWidth - document.documentElement.clientWidth,
          barInside: bar.left >= box.left && bar.right <= box.right,
          h: Math.round(box.height),
        };
      });
      console.log("TOOLBAR", width, JSON.stringify(r));
      expect(r.over).toBeLessThanOrEqual(0);
      expect(r.barInside).toBe(true);
      expect(r.docOver).toBeLessThanOrEqual(0);
      expect(r.h, "toolbar height (row count) unchanged").toBe(HEIGHT[width]);
    });
  }
});
