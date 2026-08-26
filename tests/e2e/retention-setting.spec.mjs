// The automatic-deletion setting in Settings > General (#459).
//
// Worth driving in a real browser rather than asserting on the API alone,
// because the risk here is not the value, it is the control. This setting
// destroys work that cannot be recovered, and the days field only exists while
// the switch is on. A reveal that fails leaves someone unable to see, let alone
// change, how long their library survives -- while the API happily reports a
// number they never chose.

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

  test("is off, and hides the days field until it is on", async ({ page }) => {
    await openSettings(page);

    // The default is the whole point of the setting: an install nobody has
    // configured must not delete anything.
    await expect(page.locator(".auto-delete-input")).not.toBeChecked();
    await expect(page.locator(".auto-delete-days-row")).toHaveClass(/hidden/);

    await switchFor(page).click();
    await expect(page.locator(".auto-delete-days-row")).not.toHaveClass(/hidden/);
    // Revealed with a value rather than empty, so there is nothing to mistake
    // for "no limit".
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
    await expect(page.locator(".auto-delete-days-row")).not.toHaveClass(/hidden/);
  });

  test("turning it back off hides the field and stops deletion", async ({ page }) => {
    await setRetention(page, { auto_delete_jobs: true, auto_delete_days: 14 });
    await openSettings(page);
    await expect(page.locator(".auto-delete-input")).toBeChecked();
    await expect(page.locator(".set-auto-delete-days")).toHaveValue("14");

    await switchFor(page).click();
    await expect(page.locator(".auto-delete-days-row")).toHaveClass(/hidden/);
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
