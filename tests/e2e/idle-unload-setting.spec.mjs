// "Free memory when idle" in Settings > General (#975): how many minutes
// without a new track before the separation models are unloaded, 0 for never.
//
// Driven in a real browser because the risk is the field, not the value: 0 is a
// real choice here (never unload), so a field that treats it as empty would
// show the wrong number or post a default the user never picked. The unload
// itself is tested in tests/test_idle_unload.py; no browser can observe it.

import { test, expect } from "@playwright/test";
import { openStudio, seedLibrary } from "./helpers.mjs";

const field = (page) => page.locator(".set-idle-unload");

async function openSettings(page) {
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  await expect(field(page)).toBeVisible();
}

const saved = async (page) =>
  (await (await page.request.get("/api/settings")).json()).idle_unload_minutes;

test.describe("free memory when idle", () => {
  // The backend is shared across the suite: every test leaves the default.
  test.beforeEach(async ({ page }) => {
    await page.request.post("/api/settings", { data: { idle_unload_minutes: 10 } });
  });
  test.afterEach(async ({ page }) => {
    await page.request.post("/api/settings", { data: { idle_unload_minutes: 10 } });
  });

  test("shows ten minutes, and a new value reaches the server and survives a reopen", async ({ page }) => {
    await openSettings(page);
    await expect(field(page)).toHaveValue("10");
    await expect(page.locator(".settings-row:has(.set-idle-unload) .settings-unit")).toHaveText("min");

    await field(page).fill("25");
    await field(page).blur();
    await expect.poll(() => saved(page)).toBe(25);

    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator("#settingsBtn").click();
    await expect(field(page)).toHaveValue("25");
  });

  test("0 is kept as never, not taken for an empty field", async ({ page }) => {
    await openSettings(page);
    await field(page).fill("0");
    await field(page).blur();
    await expect.poll(() => saved(page)).toBe(0);

    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator("#settingsBtn").click();
    await expect(field(page)).toHaveValue("0");
  });

  test("the server owns the ceiling, and the field shows what it kept", async ({ page }) => {
    await openSettings(page);
    await field(page).fill("9999");
    await field(page).blur();
    await expect(field(page)).toHaveValue("1440");
    expect(await saved(page)).toBe(1440);
  });

  test("refuses non-digits as they are typed", async ({ page }) => {
    await openSettings(page);
    await field(page).fill("");
    await field(page).pressSequentially("1a5");
    await expect(field(page)).toHaveValue("15");
  });

  test("an emptied field goes back to the saved value and sends nothing", async ({ page }) => {
    await page.request.post("/api/settings", { data: { idle_unload_minutes: 30 } });
    await openSettings(page);
    await expect(field(page)).toHaveValue("30");
    const posts = [];
    page.on("request", (r) => {
      if (r.method() === "POST" && r.url().endsWith("/api/settings")) posts.push(r.postData());
    });
    await field(page).fill("");
    await field(page).blur();
    await expect(field(page)).toHaveValue("30");
    expect(posts).toEqual([]);
    expect(await saved(page)).toBe(30);
  });

  test("the Guide explains it", async ({ page }) => {
    await openStudio(page);
    await page.locator("#helpModeBtn").click();
    await expect(page.locator("body")).toHaveClass(/help-mode/);
    await page.locator("#settingsBtn").hover();
    const tip = page.locator("#helpTip");
    await expect(tip).toContainText("Free memory when idle");
    await expect(tip).toContainText("Frees the memory the separation models hold");
  });

  test("is translated", async ({ page }) => {
    await seedLibrary(page);
    await page.addInitScript(() => localStorage.setItem("stemdeck.language", JSON.stringify("de")));
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.locator("#settingsBtn").click();
    await expect(page.locator(".settings-row:has(.set-idle-unload) .settings-row-title")).toHaveText(
      "Speicher im Leerlauf freigeben",
    );
  });
});
