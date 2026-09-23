// Only the Split button submits the job form.
//
// The composer is a <form>, and the top bar rework moved the now-playing block
// and its favourite heart up into it. A <button> inside a form with no explicit
// type is a submit button, so pressing the heart submitted the form and started
// an extraction. The heart was harmless in the footer for as long as it was
// outside the form, which is why nothing about the heart itself changed on the
// day it broke.
//
// The structural test is the one that matters, because it covers the next
// control dropped into this bar rather than only the one that went wrong.
import { test, expect } from "@playwright/test";
import { openStudio } from "./helpers.mjs";

test.describe("the job form", () => {
  test("every button in it says what it is", async ({ page }) => {
    await openStudio(page, { tauri: true });

    const untyped = await page.evaluate(() =>
      [...document.querySelectorAll("#job-form button")]
        .filter((b) => !b.getAttribute("type"))
        .map((b) => b.id || b.className),
    );
    // A button with no type defaults to submit. In this form that means
    // starting an extraction.
    expect(untyped).toEqual([]);
  });

  test("the only submit is the one that splits stems", async ({ page }) => {
    await openStudio(page, { tauri: true });

    const submits = await page.evaluate(() =>
      [...document.querySelectorAll("#job-form button")]
        .filter((b) => (b.getAttribute("type") || "submit") === "submit")
        .map((b) => b.id),
    );
    expect(submits).toEqual(["submit"]);
  });

  test("pressing the favourite heart does not start a job", async ({ page }) => {
    // The now-playing block, heart included, is hidden below 1460px, so this
    // has to be a window wide enough to have one.
    await page.setViewportSize({ width: 1600, height: 900 });
    await openStudio(page, { tauri: true });

    const submitted = await page.evaluate(async () => {
      let fired = false;
      document.getElementById("job-form").addEventListener("submit", () => {
        fired = true;
      });
      document.getElementById("fav-btn").click();
      await new Promise((r) => setTimeout(r, 200));
      return fired;
    });
    expect(submitted).toBe(false);
  });

  test("the heart still toggles", async ({ page }) => {
    // Giving it a type must not cost it its own job.
    await page.setViewportSize({ width: 1600, height: 900 });
    await openStudio(page, { tauri: true });

    const heart = page.locator("#fav-btn");
    const before = await heart.getAttribute("aria-pressed");
    await heart.click();
    await expect.poll(() => heart.getAttribute("aria-pressed")).not.toBe(before);
  });
});
