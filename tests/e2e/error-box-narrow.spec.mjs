// The error box (#error, showError in job.js) beyond what error-banner.spec
// covers: a narrow window, where the old strip ran off the side; an import
// failure, which gets "Try again" beside the close button; and the "error"
// class every small status line uses, which the banner's rules once floated
// over the top bar.
//
// showError is called through the app's own module (the same URL main.js
// imports, so the same instance), rather than by provoking each failure: what
// is under test is the box, not the failures that open it.
//
// The phone UI (static/mobile/) has no such box: it reports failures in its
// own toasts and notes, which nothing in this range changed.

import { test, expect } from "@playwright/test";
import { openStudio } from "./helpers.mjs";

async function showError(page, message, detail, options) {
  await page.evaluate(
    async ([m, d, o]) => (await import("/js/job.js")).showError(m, d, o),
    [message, detail, options],
  );
  await expect(page.locator("#error")).toBeVisible();
}

test.describe("the error box, narrow and wide", () => {
  test("fits a narrow window, close button included", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 800 });
    await openStudio(page);
    await showError(page, "Export failed", "x".repeat(300), { retry: false });
    const view = page.viewportSize();
    const box = await page.locator("#error").boundingBox();
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(view.width);
    // A long unbroken detail wraps inside the card rather than widening it.
    const close = await page.locator("#error .error-close").boundingBox();
    expect(close.x + close.width).toBeLessThanOrEqual(view.width);
    await page.locator("#error .error-close").click();
    await expect(page.locator("#error")).toBeHidden();
  });

  test("an import failure offers Try again, which goes back to the link field", async ({ page }) => {
    await openStudio(page);
    await showError(page, "Could not start", "bad link", { retry: true });
    const box = page.locator("#error");
    await expect(box.locator(".retry-btn")).toBeVisible();
    await expect(box.locator(".error-close")).toBeVisible();
    await box.locator(".retry-btn").click();
    await expect(box).toBeHidden();
    await expect(page.locator("#url")).toBeFocused();
  });

  test("a second failure replaces the first, never stacks under it", async ({ page }) => {
    await openStudio(page);
    await showError(page, "First failure", "", { retry: true });
    await showError(page, "Second failure", "", { retry: false });
    const box = page.locator("#error");
    await expect(box).toContainText("Second failure");
    await expect(box).not.toContainText("First failure");
    await expect(box.locator(".error-close")).toHaveCount(1);
    await expect(box.locator(".retry-btn")).toHaveCount(0);
    await expect(box.locator(".error-icon")).toHaveCount(1);
  });

  test("the close button says what it does to a screen reader", async ({ page }) => {
    await openStudio(page);
    await showError(page, "Export failed", "", { retry: false });
    await expect(page.locator("#error").getByRole("button", { name: "Dismiss" })).toBeVisible();
  });

  test("a status line with the error class stays where it is", async ({ page }) => {
    await openStudio(page);
    const position = await page.evaluate(() => {
      const line = document.createElement("p");
      line.className = "lyrics-status error";
      line.textContent = "Couldn't reach LRCLIB";
      document.body.append(line);
      const s = getComputedStyle(line);
      const out = { position: s.position, zIndex: s.zIndex };
      line.remove();
      return out;
    });
    expect(position.position).toBe("static");
  });
});
