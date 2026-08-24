// The notification centre and the "Report on GitHub" flow.
//
// The thing worth testing in a real browser (rather than in the unit test that
// covers the URL itself) is the desktop path: on Tauri the link never
// navigates -- a global handler intercepts it and hands the URL to the Rust
// open_url command. If that interception breaks, the report button does
// nothing at all in the shipped desktop app while still working perfectly in
// every browser a developer tests in. That is exactly how #335 hid.

import { test, expect } from "@playwright/test";
import { openStudio } from "./helpers.mjs";

/** Fail an export, which is the quickest real failure to provoke in the UI. */
async function failAnExport(page) {
  await page.locator("#t-export-btn").click();
  await page.locator("#t-export-mix").click();
  await page.evaluate(() => window.__e2e.choosePath());
  await page.evaluate(() => window.__e2e.failSave("disk full"));
  await expect(page.locator("#error")).not.toHaveClass(/hidden/);
}

const openBell = async (page) => {
  await page.locator("#notifBtn").click();
  await expect(page.locator(".daw-notif-panel")).toBeVisible();
};

test.describe("failure notifications", () => {
  test("a failure lands in the notification centre", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await expect(page.locator("#notifBadge")).toHaveClass(/hidden/);

    await failAnExport(page);
    await expect(page.locator("#notifBadge")).not.toHaveClass(/hidden/);

    await openBell(page);
    await expect(page.locator(".daw-notif-error")).toHaveCount(1);
    await expect(page.locator(".daw-notif-error")).toContainText("Export failed");
    await expect(page.locator("#notifEmpty")).toHaveClass(/hidden/);
  });

  test("one failure produces one card, not one per sighting", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await failAnExport(page);
    await page.locator(".retry-btn").click();
    await failAnExport(page);

    await openBell(page);
    // Same kind, same message, seconds apart: the second sighting refreshes the
    // first rather than stacking a duplicate the user dismisses twice.
    await expect(page.locator(".daw-notif-error")).toHaveCount(1);
  });

  test("the card opens a dialog whose report link is pre-filled", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await failAnExport(page);
    await openBell(page);
    await page.locator(".daw-notif-error").first().click();

    await expect(page.locator("#failureDialog")).not.toHaveClass(/hidden/);
    await expect(page.locator("#failureTitle")).toHaveText("Export failed");
    // The user can read every technical detail before sending anything.
    await expect(page.locator("#failureTech")).toContainText("StemDeck:");

    const href = await page.locator("#failureReport").getAttribute("href");
    const url = new URL(href);
    // blank_issues_enabled is false in the repo config: without a template the
    // link lands on a chooser instead of a pre-filled form.
    expect(url.searchParams.get("template")).toBe("bug_report.yml");
    expect(url.searchParams.get("title")).toContain("Export failed");
    expect(url.searchParams.get("version")).toMatch(/^v/);
    expect(url.searchParams.get("what")).toContain("Export failed");
  });

  test("desktop hands the report URL to the OS, never to the app window", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await failAnExport(page);
    await openBell(page);
    await page.locator(".daw-notif-error").first().click();
    await expect(page.locator("#failureDialog")).not.toHaveClass(/hidden/);

    await page.locator("#failureReport").click();

    const calls = await page.evaluate(() => window.__e2e.callsFor("open_url"));
    expect(calls).toHaveLength(1);
    expect(calls[0].args.url).toContain("template=bug_report.yml");
    // The Rust side rejects anything that is not http(s).
    expect(calls[0].args.url.startsWith("https://github.com/")).toBe(true);
    // A navigation would have replaced the studio with GitHub.
    expect(page.url()).not.toContain("github.com");
  });

  test("the update card shares the badge, so dismissing a failure need not clear it", async ({ page }) => {
    // One badge serves the whole centre. This is the interaction that broke the
    // two tests above in CI, where an update genuinely was available and the
    // badge stayed lit after the only failure card was dismissed -- correctly.
    // Pinned here so the shared-badge rule is a decision, not an accident.
    await openStudio(page, { tauri: true, updateAvailable: true });
    await expect(page.locator("#notifReleaseCard")).not.toHaveClass(/hidden/);
    await expect(page.locator("#notifBadge")).not.toHaveClass(/hidden/);
    // The newest release in the stub is an unpromoted pre-release. The card must
    // name the promoted one behind it: a release is offered only once it has
    // been promoted to the latest release, so nobody is updated to a build that
    // was never verified.
    await expect(page.locator("#notifReleaseDesc")).toHaveText("v9.9.9");

    await failAnExport(page);
    await openBell(page);
    await expect(page.locator(".daw-notif-error")).toHaveCount(1);

    await page.locator(".daw-notif-error .daw-notif-dismiss").first().click();
    await expect(page.locator(".daw-notif-error")).toHaveCount(0);
    // The update is still pending, so the badge stays and the empty state does
    // not come back.
    await expect(page.locator("#notifBadge")).not.toHaveClass(/hidden/);
    await expect(page.locator("#notifEmpty")).toHaveClass(/hidden/);
  });

  test("a failure survives a reload, and dismissal clears the badge", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await failAnExport(page);

    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.locator("#notifBadge")).not.toHaveClass(/hidden/);
    await openBell(page);
    // The banner is gone after a reload; the notification is the durable copy.
    await expect(page.locator(".daw-notif-error")).toHaveCount(1);

    await page.locator(".daw-notif-error .daw-notif-dismiss").first().click();
    await expect(page.locator(".daw-notif-error")).toHaveCount(0);
    await expect(page.locator("#notifBadge")).toHaveClass(/hidden/);
    await expect(page.locator("#notifEmpty")).not.toHaveClass(/hidden/);
  });
});
