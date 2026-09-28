// The AcoustID key in Settings > Song details (song identification).
//
// Driven in a browser because the risk is the control, not the value: the
// key is the user's, so the field is masked, it is never filled back in (the
// server never hands the key out, only its last four characters), a refused
// key has to say so, and Clear has to actually clear it. The line explaining
// that only a fingerprint is sent, and the link to register a key, are part
// of the contract.

import { test, expect } from "@playwright/test";
import { seedLibrary } from "./helpers.mjs";

const KEY = "Qa12Ws34Ed";

async function openSettings(page) {
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  await page.locator('.settings-tab[data-tab="details"]').click();
  await expect(page.locator(".set-acoustid-key")).toBeVisible();
}

const settings = async (page) => (await page.request.get("/api/settings")).json();

test.describe("AcoustID key", () => {
  test.beforeEach(async ({ page }) => {
    await page.request.post("/api/settings", { data: { acoustid_api_key: "" } });
  });

  // The backend is shared across the suite: leave no key behind.
  test.afterEach(async ({ page }) => {
    await page.request.post("/api/settings", { data: { acoustid_api_key: "" } });
  });

  test("starts empty and masked, and explains what is sent", async ({ page }) => {
    await openSettings(page);
    const input = page.locator(".set-acoustid-key");
    await expect(input).toHaveAttribute("type", "password");
    await expect(input).toHaveValue("");
    await expect(page.locator(".acoustid-key-msg")).toHaveText("No key saved.");
    await expect(page.locator(".set-acoustid-clear")).toBeDisabled();
    await expect(page.locator(".set-acoustid-save")).toBeDisabled();
    await expect(page.locator(".acoustid-row .settings-row-desc")).toContainText(
      "Only the fingerprint is sent to AcoustID, never the audio.",
    );
    const link = page.locator(".acoustid-register");
    await expect(link).toHaveAttribute("href", "https://acoustid.org/new-application");
    await expect(link).toHaveAttribute("target", "_blank");
  });

  test("saves a key, shows only its last four, and survives a reopen", async ({ page }) => {
    await openSettings(page);
    const input = page.locator(".set-acoustid-key");
    await input.fill(KEY);
    await expect(page.locator(".set-acoustid-save")).toBeEnabled();

    const [response] = await Promise.all([
      page.waitForResponse((r) => r.url().endsWith("/api/settings") && r.request().method() === "POST"),
      page.locator(".set-acoustid-save").click(),
    ]);
    expect(await response.text()).not.toContain(KEY);
    await expect(page.locator(".acoustid-key-msg")).toHaveText("A key ending in Ed is saved.");
    await expect(input).toHaveValue("");
    await expect(page.locator(".set-acoustid-clear")).toBeEnabled();

    const stored = await settings(page);
    expect(stored.acoustid_api_key_set).toBe(true);
    expect(JSON.stringify(stored)).not.toContain(KEY);

    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator("#settingsBtn").click();
    await page.locator('.settings-tab[data-tab="details"]').click();
    await expect(page.locator(".acoustid-key-msg")).toHaveText("A key ending in Ed is saved.");
    await expect(page.locator(".set-acoustid-key")).toHaveValue("");
  });

  test("a key that cannot be one is refused and nothing is saved", async ({ page }) => {
    await openSettings(page);
    await page.locator(".set-acoustid-key").fill("not a key!");
    await page.locator(".set-acoustid-save").click();
    await expect(page.locator(".acoustid-key-msg")).toHaveText(
      "That does not look like an AcoustID key.",
    );
    await expect(page.locator(".acoustid-key-msg")).toHaveClass(/error/);
    // Left in the field to be corrected.
    await expect(page.locator(".set-acoustid-key")).toHaveValue("not a key!");
    expect((await settings(page)).acoustid_api_key_set).toBe(false);
  });

  test("a key AcoustID refuses says which key to use instead", async ({ page }) => {
    // The server tries a key on AcoustID before keeping it; the browser tests
    // never reach AcoustID, so its refusal is answered here.
    await page.route("**/api/settings", (route) => (route.request().method() === "POST"
      ? route.fulfill({ status: 422, contentType: "application/json", body: JSON.stringify({ detail: "AcoustID does not accept this key" }) })
      : route.fallback()));
    await openSettings(page);
    await page.locator(".set-acoustid-key").fill(KEY);
    await page.locator(".set-acoustid-save").click();
    await expect(page.locator(".acoustid-key-msg")).toHaveText(
      "AcoustID does not accept this key. Use the key of an application you register at acoustid.org, not the user key on your profile.",
    );
    await expect(page.locator(".acoustid-key-msg")).toHaveClass(/error/);
    await expect(page.locator(".set-acoustid-key")).toHaveValue(KEY);
  });

  test("Clear removes the key", async ({ page }) => {
    await page.request.post("/api/settings", { data: { acoustid_api_key: KEY } });
    await openSettings(page);
    await expect(page.locator(".acoustid-key-msg")).toHaveText("A key ending in Ed is saved.");
    await page.locator(".set-acoustid-clear").click();
    await expect(page.locator(".acoustid-key-msg")).toHaveText("No key saved.");
    await expect(page.locator(".set-acoustid-clear")).toBeDisabled();
    expect((await settings(page)).acoustid_api_key_set).toBe(false);
  });
});
