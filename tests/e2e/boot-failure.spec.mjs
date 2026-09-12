// A failure while the app wires itself up must be visible and quotable.
//
// Before boot-diagnostics.js existed, both cases below produced an app that
// hovered and accepted typing but whose buttons did nothing, with nothing
// logged anywhere, because main.js registers its own error handlers only after
// the wiring run that failed (#618, reported as #617).
//
// Both tests fail without static/js/boot-diagnostics.js loaded first.

import { test, expect } from "@playwright/test";

const BANNER = "#boot-failure";

test("a module that fails to load is reported in the UI", async ({ page }) => {
  // The missing-file case: a JS file quarantined by antivirus or lost in a
  // partial unzip. main.js cannot resolve the import and never evaluates, so
  // nothing it would have registered exists.
  await page.route("**/js/catalog.js", (route) => route.fulfill({ status: 404, body: "" }));

  await page.goto("/");

  const banner = page.locator(BANNER);
  await expect(banner).toBeVisible();
  await expect(banner).toContainText("StemDeck did not finish starting up");

  // The banner names main.js, not catalog.js, and that is the honest limit of
  // what the page can know: the module loader fetches dependencies itself, so a
  // dependency that 404s produces no per-file error event. What surfaces is the
  // entry module whose graph failed to resolve.
  //
  // Which file is missing is answerable, just not from here: the 404 is a
  // uvicorn access line in backend.log. That is why the banner points at
  // /api/logs/backend rather than pretending to know.
  await expect(banner).toContainText("Failed to load");
  await expect(banner).toContainText("main.js");
  await expect(banner).toContainText("/api/logs/backend");
});

test("a throw during wiring is reported in the UI", async ({ page }) => {
  // The other cause with the same symptom: every file loads, and one of the
  // wireX() calls throws.
  await page.route("**/js/main.js", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/javascript",
      body: 'throw new Error("wiring blew up");',
    }),
  );

  await page.goto("/");

  const banner = page.locator(BANNER);
  await expect(banner).toBeVisible();
  await expect(banner).toContainText("wiring blew up");
});

test("the detail can be selected, and the banner can be dismissed", async ({ page }) => {
  // The text exists to be copied into a bug report, and the app sets
  // user-select: none in places, so the banner sets it back explicitly.
  await page.route("**/js/main.js", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/javascript",
      body: 'throw new Error("selectable please");',
    }),
  );

  await page.goto("/");

  const pre = page.locator(`${BANNER} pre`);
  await expect(pre).toHaveCSS("user-select", "text");

  await page.locator(`${BANNER} button`).click();
  await expect(page.locator(BANNER)).toHaveCount(0);
});

test("a healthy page shows no banner", async ({ page }) => {
  // Guards the obvious regression: a banner that appears when nothing is wrong
  // would be worse than no banner at all.
  await page.goto("/");
  await expect(page.locator("#url")).toBeVisible();
  await expect(page.locator(BANNER)).toHaveCount(0);
});
