// "Make StemDeck available on your network" in the desktop app (#859).
//
// The desktop backend listens on loopback only while the setting is off, so a
// change only applies once the shell has restarted it on the new address. The
// page saves the choice first, so the restarted backend reads it, then asks
// the shell to restart, then reloads on the URL the shell answers with. These
// pin that order, what a failed restart leaves on screen, and that a running
// separation is never stopped without asking.
//
// The shell is stubbed (stubTauri): no test here drives the real Rust restart.
// The saving POST is answered here too, so the shared test backend keeps its
// own setting.

import { test, expect } from "@playwright/test";
import { seedLibrary, stubTauri } from "./helpers.mjs";

/** Hold restart_backend open so the test decides how it ends, and record the
 *  saving POST into the same call log so the order can be read back. */
async function controlRestart(page) {
  await page.addInitScript(() => {
    const invoke = window.__TAURI__.core.invoke;
    let settle = null;
    window.__TAURI__.core.invoke = (cmd, args) => {
      if (cmd !== "restart_backend") return invoke(cmd, args);
      window.__e2e.calls.push({ cmd, args });
      return new Promise((resolve, reject) => { settle = { resolve, reject }; });
    };
    window.__e2e.restartPending = () => Boolean(settle);
    window.__e2e.finishRestart = (url) => { settle.resolve({ url }); settle = null; };
    window.__e2e.failRestart = (message) => { settle.reject(message); settle = null; };
    const fetchOriginal = window.fetch.bind(window);
    window.fetch = (input, init) => {
      if (String(input).endsWith("/api/settings") && init?.method === "POST") {
        window.__e2e.calls.push({ cmd: "POST /api/settings", args: JSON.parse(init.body) });
      }
      return fetchOriginal(input, init);
    };
  });
}

/** Answer /api/settings as a desktop backend that saved `saved` and listens
 *  beyond loopback when `listening`. A POST is answered here and never reaches
 *  the backend. */
async function desktopSettings(page, { listening, saved = listening }) {
  await page.route("**/api/settings", async (route) => {
    const request = route.request();
    if (request.method() === "POST") {
      const { allow_network } = request.postDataJSON();
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ allow_network }) });
      return;
    }
    const body = await (await route.fetch()).json();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ...body, allow_network: saved, network_listening: listening }),
    });
  });
}

/** Answer /api/queue with a separation running, or none. */
async function queueRunning(page, running) {
  await page.route("**/api/queue", async (route) => {
    const resp = await route.fetch();
    const body = await resp.json();
    const job = { job_id: "e2e0deadbeef", status: "separating", title: "E2E Fixture Track" };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ...body, running: running ? job : null }),
    });
  });
}

async function openNetworkSettings(page) {
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  await page.locator('.settings-tab[data-tab="network"]').click();
  await expect(page.locator(".net-access-input")).toBeVisible();
}

const toggle = (page) => page.locator(".settings-switch:has(.net-access-input)").click();
const calls = (page) => page.evaluate(() => window.__e2e.calls.map((c) => c.cmd));

test.describe("desktop", () => {
  test.beforeEach(async ({ page }) => {
    await stubTauri(page);
    await controlRestart(page);
  });

  test("turning it on saves first, then restarts, then reloads", async ({ page }) => {
    await desktopSettings(page, { listening: false });
    await queueRunning(page, false);
    await openNetworkSettings(page);
    await toggle(page);

    await expect.poll(() => page.evaluate(() => window.__e2e.restartPending())).toBe(true);
    const log = await page.evaluate(() => window.__e2e.calls);
    const order = log.map((c) => c.cmd).filter((c) => c !== "store_get" && c !== "store_set");
    expect(order.slice(-2)).toEqual(["POST /api/settings", "restart_backend"]);
    expect(log.find((c) => c.cmd === "POST /api/settings").args).toEqual({ allow_network: true });
    // The page has no say in the bind: the shell reads it from settings.json.
    expect(log.find((c) => c.cmd === "restart_backend").args).toBeUndefined();
    await expect(page.locator(".net-restart-body")).toContainText(/Restarting StemDeck/);
    // Not on until it has applied.
    await expect(page.locator(".net-access-input")).not.toBeChecked();

    const origin = new URL(page.url()).origin;
    const reloaded = page.waitForEvent("framenavigated");
    await page.evaluate((url) => window.__e2e.finishRestart(url), origin);
    await reloaded;
    expect(new URL(page.url()).origin).toBe(origin);
  });

  test("a failed restart says why and offers Try again", async ({ page }) => {
    await desktopSettings(page, { listening: false });
    await queueRunning(page, false);
    await openNetworkSettings(page);
    await toggle(page);
    await expect.poll(() => page.evaluate(() => window.__e2e.restartPending())).toBe(true);

    await page.evaluate(() => window.__e2e.failRestart("the audio backend did not shut down in time"));
    await expect(page.locator(".reset-confirm-msg")).toContainText("did not shut down in time");
    const retry = page.locator(".net-restart-retry");
    await expect(retry).toBeVisible();

    await retry.click();
    await expect.poll(() => page.evaluate(() => window.__e2e.restartPending())).toBe(true);
    const restarts = (await calls(page)).filter((c) => c === "restart_backend");
    expect(restarts).toHaveLength(2);
  });

  test("with a separation running it asks, and Cancel changes nothing", async ({ page }) => {
    await desktopSettings(page, { listening: false });
    await queueRunning(page, true);
    await openNetworkSettings(page);
    await toggle(page);

    await expect(page.locator(".net-restart-body")).toContainText(/separation is running/i);
    await page.locator(".net-restart-cancel").click();
    await expect(page.locator(".net-restart-body")).toHaveCount(0);

    const log = await calls(page);
    expect(log).not.toContain("POST /api/settings");
    expect(log).not.toContain("restart_backend");
    await expect(page.locator(".net-access-input")).not.toBeChecked();
  });

  test("with a separation running, Restart now goes ahead", async ({ page }) => {
    await desktopSettings(page, { listening: false });
    await queueRunning(page, true);
    await openNetworkSettings(page);
    await toggle(page);

    await page.locator(".net-restart-go").click();
    await expect.poll(() => page.evaluate(() => window.__e2e.restartPending())).toBe(true);
    expect(await calls(page)).toContain("POST /api/settings");
  });

  test("the switch shows what is listening, not only what was saved", async ({ page }) => {
    // Saved on, but the backend still listens on loopback: the switch is off,
    // so turning it on is what restarts onto the network.
    await desktopSettings(page, { listening: false, saved: true });
    await openNetworkSettings(page);
    await expect(page.locator(".net-access-input")).not.toBeChecked();
  });
});

test("in server mode the switch stays on and read-only, with nothing to restart", async ({ page }) => {
  // A server reports no network_listening (null): its bind is not the
  // shell's to report, so the switch must not read it as off.
  await page.route("**/api/settings", async (route) => {
    const body = await (await route.fetch()).json();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ...body, allow_network: true, network_listening: null }),
    });
  });
  await openNetworkSettings(page);
  const input = page.locator(".net-access-input");
  await expect(input).toBeChecked();
  await expect(input).toBeDisabled();
  await expect(page.locator(".net-restart-body")).toHaveCount(0);
});
