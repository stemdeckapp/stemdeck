// Trashing the song that is open empties the studio (#1072).
//
// Deleting a song used to forget it in the library list only. The mixer, the
// audio engine and the now-playing card kept showing it, and the Extract stems
// button stayed aimed at a song that was gone.
//
// The server's trash endpoint is stubbed and recorded: the real one would leave
// the shared fixture job trashed on the server, and the next spec's startup
// sync would move it out of its library.

import { test, expect } from "@playwright/test";

import { JOB_ID, SIBLING_JOB_ID, openStudio } from "./helpers.mjs";

test("trashing the open song empties the studio", async ({ page }) => {
  const trashed = [];
  await page.route(/\/api\/jobs\/[^/]+\/trash$/, (route) => {
    trashed.push(route.request().url().match(/\/api\/jobs\/([^/]+)\/trash$/)[1]);
    return route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
  });
  await openStudio(page);
  await expect(page.locator(".app")).not.toHaveClass(/no-track/);

  // The row's actions only take the pointer on hover (#724).
  const row = page.locator(`.cat-item[data-id="${JOB_ID}"]`).first();
  await row.hover();
  await row.locator(".cat-del").click();

  await expect(page.locator(".app")).toHaveClass(/no-track/);
  await expect(page.locator("#t-time")).toHaveText(/00:00\s*\/\s*00:00/);
  await expect(page.locator(".cat-item.active")).toHaveCount(0);
  await expect(page.locator("#submit")).toHaveAttribute("data-resplit-job", "");
  expect(trashed).toContain(JOB_ID);
});

test("trashing a song that is not open leaves the open one playing", async ({ page }) => {
  await page.route(/\/api\/jobs\/[^/]+\/trash$/, (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "{}" }),
  );
  await openStudio(page);

  const other = page.locator(`.cat-item[data-id="${SIBLING_JOB_ID}"]`).first();
  await other.hover();
  await other.locator(".cat-del").click();

  await expect(page.locator(`.cat-item[data-id="${JOB_ID}"]`).first()).toHaveClass(/active/);
  await expect(page.locator(".app")).not.toHaveClass(/no-track/);
});
