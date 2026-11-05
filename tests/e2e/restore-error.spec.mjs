// A restore import (importFromUrl in job.js: "Sync again", a missing track, a
// failed import tried again) that fails always says why. It used to show
// "Failed to restore track:" with nothing after the colon when the error had
// no message. The shapes of a server answer that gave "" are checked in
// tests/js/error-reason.test.mjs: a Playwright stub always carries a status
// text, so an answer without one cannot be reproduced here.
//
// POST /api/jobs never leaves the page, so nothing reaches YouTube.

import { test, expect } from "@playwright/test";
import { openStudio } from "./helpers.mjs";

const URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ";

async function restore(page) {
  return page.evaluate(async (url) => (await import("/js/job.js")).importFromUrl(url), URL);
}

test.describe("a failed restore import", () => {
  test("with an error that has no message shows a sentence and logs the error", async ({ page }) => {
    // The request rejects before it reaches the server, with an empty message.
    await page.addInitScript(() => {
      const realFetch = window.fetch;
      window.fetch = (input, init) => {
        const url = typeof input === "string" ? input : input?.url || "";
        if (url.endsWith("/api/jobs") && init?.method === "POST") return Promise.reject(new TypeError(""));
        return realFetch(input, init);
      };
    });
    const logged = [];
    page.on("console", (msg) => {
      if (msg.type() === "error") logged.push(msg.text());
    });
    await openStudio(page);

    expect(await restore(page)).toBeNull();
    const box = page.locator("#error");
    await expect(box).toBeVisible();
    await expect(box).toContainText("Failed to restore track. No reason was given; please try again.");
    await expect(box).not.toContainText("Failed to restore track:");
    expect(logged.some((text) => text.includes("import for restore failed"))).toBe(true);
  });
});
