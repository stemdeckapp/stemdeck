// The Settings dialog's navigation: sections down the left as a vertical
// tablist, arrow keys between them, search across every section, Escape and
// focus return. The matching itself is pinned without a browser in
// tests/js/settings-nav.test.mjs; this checks it is wired to the dialog.

import { test, expect } from "@playwright/test";
import { seedLibrary } from "./helpers.mjs";

async function openSettings(page) {
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").focus();
  await page.locator("#settingsBtn").click();
  await expect(page.locator(".library-editor")).toBeVisible();
}

const activeTab = (page) => page.locator(".settings-tab.active");

test("the sections are a vertical tablist with one tab in the tab order", async ({ page }) => {
  await openSettings(page);
  const list = page.locator(".settings-tabs");
  await expect(list).toHaveAttribute("role", "tablist");
  await expect(list).toHaveAttribute("aria-orientation", "vertical");
  await expect(page.locator(".settings-tab")).toHaveCount(8);
  await expect(page.locator('.settings-tab[tabindex="0"]')).toHaveCount(1);
  await expect(activeTab(page)).toHaveAttribute("data-tab", "general");
  await expect(activeTab(page)).toHaveAttribute("aria-selected", "true");
  await expect(activeTab(page)).toBeFocused();
});

test("arrow keys, Home and End move between sections", async ({ page }) => {
  await openSettings(page);
  await page.keyboard.press("ArrowDown");
  await expect(activeTab(page)).toHaveAttribute("data-tab", "library");
  await expect(page.locator('.settings-pane[data-pane="library"]')).toBeVisible();
  await page.keyboard.press("End");
  await expect(activeTab(page)).toHaveAttribute("data-tab", "registry");
  await page.keyboard.press("ArrowDown");
  await expect(activeTab(page)).toHaveAttribute("data-tab", "general");
  await page.keyboard.press("ArrowUp");
  await expect(activeTab(page)).toHaveAttribute("data-tab", "registry");
  await page.keyboard.press("Home");
  await expect(activeTab(page)).toHaveAttribute("data-tab", "general");
});

test("Tab stays inside the open dialog", async ({ page }) => {
  await openSettings(page);
  for (let i = 0; i < 40; i++) {
    await page.keyboard.press("Tab");
    expect(await page.evaluate(() => !!document.activeElement?.closest(".library-editor"))).toBe(true);
  }
});

test("search finds a setting in another section and opens it", async ({ page }) => {
  await openSettings(page);
  const search = page.locator(".settings-search-input");
  await search.fill("sample rate");
  const result = page.locator(".settings-result", { hasText: "Sample rate" });
  await expect(result).toBeVisible();
  await expect(result.locator("mark").first()).toBeVisible();
  await result.click();
  await expect(activeTab(page)).toHaveAttribute("data-tab", "export");
  await expect(page.locator(".set-export-samplerate")).toBeFocused();
  await expect(search).toHaveValue("");
});

test("a search with no match says so, and Escape clears it first", async ({ page }) => {
  await openSettings(page);
  const search = page.locator(".settings-search-input");
  await search.fill("qqqzzz");
  await expect(page.locator(".settings-results-status")).toContainText("qqqzzz");
  await search.press("Escape");
  await expect(search).toHaveValue("");
  await expect(page.locator(".library-editor")).toBeVisible();
  await expect(page.locator('.settings-pane[data-pane="general"]')).toBeVisible();
});

test("Escape closes Settings and focus returns to the Settings button", async ({ page }) => {
  await openSettings(page);
  await page.keyboard.press("Escape");
  await expect(page.locator(".library-editor")).toHaveCount(0);
  await expect(page.locator("#settingsBtn")).toBeFocused();
});

test("Escape on the reset confirmation closes only the confirmation", async ({ page }) => {
  await openSettings(page);
  await page.locator('.settings-tab[data-tab="library"]').click();
  await page.locator(".settings-reset-btn").click();
  await expect(page.locator(".reset-confirm-card")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.locator(".reset-confirm-card")).toHaveCount(0);
  await expect(page.locator(".library-editor")).toBeVisible();
  await expect(page.locator(".settings-reset-btn")).toBeFocused();
});
