// The automatic-deletion setting in Settings > General (#459).
//
// Worth driving in a real browser rather than asserting on the API alone,
// because the risk here is not the value, it is the control. This setting
// destroys work that cannot be recovered, and the days field is only live while
// the switch is on. If that coupling breaks, someone edits a number that does
// nothing, or cannot edit one that does -- while the API happily reports a
// value they never chose.
//
// The field is dimmed rather than removed. `hidden` would not have worked here
// in any case: base.css is not loaded by index.html and daw.css scopes its
// rule to `.daw`, while the settings overlay is appended to document.body.

import { test, expect } from "@playwright/test";
import { seedLibrary } from "./helpers.mjs";

/** Open Settings on the General tab, which is where this setting lives. */
async function openSettings(page) {
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  await expect(page.locator(".auto-delete-input")).toBeVisible();
}

/** Whatever the server currently holds, so each test starts from a known state. */
async function setRetention(page, patch) {
  await page.request.post("/api/settings", { data: patch });
}

// The checkbox itself sits under the styled switch track, so clicking it
// directly is intercepted. Clicking the label is both what Playwright can do
// and what a user actually does.
const switchFor = (page) => page.locator("label.settings-switch:has(.auto-delete-input)");

test.describe("automatic deletion", () => {
  test.beforeEach(async ({ page }) => {
    await setRetention(page, { auto_delete_jobs: false, auto_delete_days: 30 });
  });

  // The backend is shared across the suite and this setting deletes jobs.
  // Leaving it switched on would hand every later spec a server that is armed
  // to remove the fixture track they all depend on.
  test.afterEach(async ({ page }) => {
    await setRetention(page, { auto_delete_jobs: false, auto_delete_days: 30 });
  });

  test("is off, and leaves the days field inert until it is on", async ({ page }) => {
    await openSettings(page);

    // The default is the whole point of the setting: an install nobody has
    // configured must not delete anything.
    await expect(page.locator(".auto-delete-input")).not.toBeChecked();
    await expect(page.locator(".auto-delete-days-row")).toHaveClass(/disabled/);
    await expect(page.locator(".set-auto-delete-days")).toBeDisabled();

    await switchFor(page).click();
    await expect(page.locator(".auto-delete-days-row")).not.toHaveClass(/disabled/);
    await expect(page.locator(".set-auto-delete-days")).toBeEnabled();
    // Readable the whole time, so deciding whether to switch deletion on does
    // not require switching it on to find out what it would do.
    await expect(page.locator(".set-auto-delete-days")).toHaveValue("30");
  });

  test("the toggle reaches the server and survives a reopen", async ({ page }) => {
    await openSettings(page);
    await switchFor(page).click();

    await expect
      .poll(async () => (await (await page.request.get("/api/settings")).json()).auto_delete_jobs)
      .toBe(true);

    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator("#settingsBtn").click();
    await expect(page.locator(".auto-delete-input")).toBeChecked();
    await expect(page.locator(".auto-delete-days-row")).not.toHaveClass(/disabled/);
    await expect(page.locator(".set-auto-delete-days")).toBeEnabled();
  });

  test("turning it back off makes the field inert and stops deletion", async ({ page }) => {
    await setRetention(page, { auto_delete_jobs: true, auto_delete_days: 14 });
    await openSettings(page);
    await expect(page.locator(".auto-delete-input")).toBeChecked();
    await expect(page.locator(".set-auto-delete-days")).toHaveValue("14");

    await switchFor(page).click();
    await expect(page.locator(".auto-delete-days-row")).toHaveClass(/disabled/);
    await expect(page.locator(".set-auto-delete-days")).toBeDisabled();
    await expect
      .poll(async () => (await (await page.request.get("/api/settings")).json()).auto_delete_jobs)
      .toBe(false);
  });

  test("the server owns the ceiling, and says so in the field", async ({ page }) => {
    await openSettings(page);
    await switchFor(page).click();

    const days = page.locator(".set-auto-delete-days");
    await days.fill("9999");
    await days.blur();

    // Clamped by the server and written back, the same arrangement as max
    // track length. The field must not keep showing a number that is not what
    // the library will actually be kept for.
    await expect(days).toHaveValue("365");
  });

  test("the days field refuses non-digits as they are typed", async ({ page }) => {
    await openSettings(page);
    await switchFor(page).click();

    const days = page.locator(".set-auto-delete-days");
    await days.fill("");
    await days.pressSequentially("1a2");
    await expect(days).toHaveValue("12");
  });
});
