// Settings, Privacy: crash reports are off by default, and desktop only.
//
// The desktop branch runs under stubTauri, whose crash_reports_status answers
// null: the state a fresh install has, with nothing on record and nothing
// sent. The Rust side (consent gating, scrubbing, withdrawal) is not driven
// here; desktop/src-tauri/src/telemetry.rs has its own tests for it.

import { test, expect } from "@playwright/test";
import { seedLibrary, stubTauri } from "./helpers.mjs";

async function openPrivacy(page) {
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  await expect(page.locator(".library-editor")).toBeVisible();
  await page.locator('.settings-tab[data-tab="privacy"]').click();
  await expect(page.locator('.settings-pane[data-pane="privacy"]')).toBeVisible();
}

test("desktop: the switch is off by default and nothing has been sent", async ({ page }) => {
  await stubTauri(page);
  await openPrivacy(page);
  const sw = page.locator(".crash-reports-input");
  await expect(sw).toBeVisible();
  await expect(sw).not.toBeChecked();
  await expect(page.locator(".crash-reports-last")).toHaveValue("Nothing has been sent yet.");
  await expect(page.locator(".crash-reports-server-note")).toBeHidden();
  // The page asked the shell, rather than deciding the state itself.
  const asked = await page.evaluate(() => window.__e2e.callsFor("crash_reports_status").length);
  expect(asked).toBeGreaterThan(0);
  // Nothing was forwarded while off.
  const forwarded = await page.evaluate(() => window.__e2e.callsFor("report_web_error").length);
  expect(forwarded).toBe(0);
});

test("desktop: turning the switch on asks the shell with the consent text version", async ({ page }) => {
  await stubTauri(page);
  await openPrivacy(page);
  await page.locator(".crash-reports-input").click({ force: true });
  await expect.poll(() => page.evaluate(() => window.__e2e.callsFor("set_crash_reports").length)).toBe(1);
  const args = await page.evaluate(() => window.__e2e.callsFor("set_crash_reports")[0].args);
  expect(args).toEqual({ on: true, version: 1 });
  // The stub answers null, which is not "on": the switch shows the shell's
  // answer, not the click.
  await expect(page.locator(".crash-reports-input")).not.toBeChecked();
});

test("desktop: an error in the page is not forwarded while reports are off", async ({ page }) => {
  await stubTauri(page);
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.evaluate(() => {
    setTimeout(() => { throw new Error("e2e crash report probe"); }, 0);
    Promise.reject(new Error("e2e rejection probe"));
  });
  await page.waitForTimeout(300);
  const forwarded = await page.evaluate(() => window.__e2e.callsFor("report_web_error").length);
  expect(forwarded).toBe(0);
});

test("desktop: the example report shows what the shell made, with reports off", async ({ page }) => {
  await stubTauri(page);
  // stubTauri answers null for commands it does not know; this one answers
  // with a fixed example, as the shell's crash_report_example would.
  const example = '{\n  "event_id": "00000000000000000000000000000000",\n  "exception": { "values": [ { "value": "Could not open <path> <email> (from <url>)" } ] }\n}';
  await page.addInitScript((json) => {
    const inner = window.__TAURI__.core.invoke;
    window.__TAURI__.core.invoke = (cmd, args) => (cmd === "crash_report_example" ? (window.__e2e.calls.push({ cmd, args }), Promise.resolve(json)) : inner(cmd, args));
  }, example);
  await openPrivacy(page);
  const box = page.locator(".crash-reports-example");
  await expect(box).toBeVisible();
  await expect(box).toHaveValue(example);
  await expect(box).toHaveAttribute("readonly", "");
  await expect(box).toHaveAttribute("aria-label", "Example report (read only)");
  await expect(page.locator(".crash-reports-input")).not.toBeChecked();
  const asked = await page.evaluate(() => window.__e2e.callsFor("crash_report_example").length);
  expect(asked).toBe(1);
  // Showing the example turned nothing on and sent nothing.
  expect(await page.evaluate(() => window.__e2e.callsFor("set_crash_reports").length)).toBe(0);
  expect(await page.evaluate(() => window.__e2e.callsFor("report_web_error").length)).toBe(0);
});

test("desktop: a build with no report address shows a disabled switch and says so", async ({ page }) => {
  await stubTauri(page);
  // As a build from source answers: no STEMDECK_SENTRY_DSN was compiled in.
  await page.addInitScript(() => {
    const inner = window.__TAURI__.core.invoke;
    window.__TAURI__.core.invoke = (cmd, args) => {
      if (cmd !== "crash_reports_status") return inner(cmd, args);
      window.__e2e.calls.push({ cmd, args });
      return Promise.resolve({ available: false, on: false, version: 1, givenAt: null, lastReport: null });
    };
  });
  await openPrivacy(page);
  await expect(page.locator(".crash-reports-unavailable")).toHaveText(
    "Crash reports are not available in this build. Nothing is ever sent from it.",
  );
  await expect(page.locator(".crash-reports-input")).toBeDisabled();
  await expect(page.locator(".crash-reports-input")).not.toBeChecked();
  // The example still shows; only sending is missing.
  await expect(page.locator(".crash-reports-example")).toBeVisible();
});

test("server: the pane is only the sentence that says a server sends nothing", async ({ page }) => {
  await openPrivacy(page);
  await expect(page.locator(".crash-reports-server-note")).toHaveText(
    "This StemDeck runs as a server, and a server never sends crash reports. They exist only in the desktop app, and only once they are turned on there.",
  );
  const pane = page.locator('.settings-pane[data-pane="privacy"]');
  await expect(pane.locator(".settings-group-title")).toBeHidden();
  await expect(pane.locator(".crash-reports-input")).toBeHidden();
  await expect(pane.locator(".crash-reports-example")).toBeHidden();
  await expect(pane.locator(".crash-reports-list").first()).toBeHidden();
  await expect(pane.locator(".crash-reports-last")).toBeHidden();
});
