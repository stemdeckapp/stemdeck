// "Make StemDeck available on your network" in the desktop app (#859).
//
// The desktop backend listens on loopback only while the setting is off, so a
// change only applies once the shell has restarted it on the new address. The
// page saves the choice so the restarted backend reads it, asks the shell to
// restart, then reloads on the URL the shell answers with. Turning it off is
// saved before anything else, so other devices are refused at once even when
// the restart waits. A running separation is never stopped without asking.
//
// The shell is stubbed (stubTauri): no test here drives the real Rust restart.
// The saving POST and the queue are answered here, so the shared test backend
// keeps its own state. In server mode the switch is disabled, so none of this
// is reachable there.

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
 *  beyond loopback when `listening`. A POST is answered here with
 *  `postStatus` and never reaches the backend. */
async function desktopSettings(page, { listening, saved = listening, postStatus = 200 }) {
  await page.route("**/api/settings", async (route) => {
    const request = route.request();
    if (request.method() === "POST") {
      const { allow_network } = request.postDataJSON();
      await route.fulfill({ status: postStatus, contentType: "application/json", body: JSON.stringify({ allow_network }) });
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

/** Answer /api/queue with a separation running, none, or a failure. */
async function queue(page, state) {
  await page.route("**/api/queue", async (route) => {
    if (state === "error") {
      await route.fulfill({ status: 500, contentType: "text/plain", body: "boom" });
      return;
    }
    const body = await (await route.fetch()).json();
    const job = { job_id: "e2e0deadbeef", status: "separating", title: "E2E Fixture Track" };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ...body, running: state === "running" ? job : null }),
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

const sw = (page) => page.locator(".net-access-input");
const toggle = (page) => page.locator(".settings-switch:has(.net-access-input)").click();
/** The saves and restarts, in order: everything else the page asks the shell is noise here. */
const calls = (page) =>
  page.evaluate(() =>
    window.__e2e.calls.map((c) => c.cmd).filter((c) => c === "POST /api/settings" || c === "restart_backend"),
  );
const posts = (page) =>
  page.evaluate(() => window.__e2e.calls.filter((c) => c.cmd === "POST /api/settings").map((c) => c.args));
const restartPending = (page) => expect.poll(() => page.evaluate(() => window.__e2e.restartPending())).toBe(true);
const dialogText = (page) => page.locator(".net-restart-body");

test.beforeEach(async ({ page }) => {
  await stubTauri(page);
  await controlRestart(page);
});

test("turning it on saves first, then restarts, then reloads where the shell says", async ({ page }) => {
  await desktopSettings(page, { listening: false });
  await queue(page, "idle");
  await openNetworkSettings(page);
  await toggle(page);

  await restartPending(page);
  expect(await calls(page)).toEqual(["POST /api/settings", "restart_backend"]);
  expect(await posts(page)).toEqual([{ allow_network: true }]);
  // The page has no say in the bind: the shell reads it from settings.json.
  const restart = await page.evaluate(() => window.__e2e.calls.find((c) => c.cmd === "restart_backend"));
  expect(restart.args).toBeUndefined();
  await expect(dialogText(page)).toContainText(/Restarting StemDeck/);
  // Not on until it has applied, and not changeable again meanwhile.
  await expect(sw(page)).not.toBeChecked();
  await expect(sw(page)).toBeDisabled();

  const target = `${new URL(page.url()).origin}/?restarted=1`;
  const reloaded = page.waitForURL(target);
  await page.evaluate((url) => window.__e2e.finishRestart(url), target);
  await reloaded;
});

test("turning it off refuses other devices before anything else", async ({ page }) => {
  await desktopSettings(page, { listening: true });
  await queue(page, "running");
  await openNetworkSettings(page);
  await expect(sw(page)).toBeChecked();
  await toggle(page);

  // Saved before the question, so Not now still leaves it off.
  await expect(dialogText(page)).toContainText(/refused from now on/i);
  expect(await posts(page)).toEqual([{ allow_network: false }]);
  await page.locator(".net-restart-cancel").click();
  await expect(dialogText(page)).toHaveCount(0);
  expect(await calls(page)).not.toContain("restart_backend");
  await expect(sw(page)).not.toBeChecked();
  await expect(sw(page)).toBeEnabled();
});

test("turning it on with a separation running asks, and Not now saves nothing", async ({ page }) => {
  await desktopSettings(page, { listening: false });
  await queue(page, "running");
  await openNetworkSettings(page);
  await toggle(page);

  await expect(dialogText(page)).toContainText(/separation is running/i);
  await expect(page.locator(".net-restart-go")).toBeFocused();
  await page.locator(".net-restart-cancel").click();
  await expect(dialogText(page)).toHaveCount(0);
  expect(await calls(page)).toEqual([]);
  await expect(sw(page)).not.toBeChecked();
  await expect(sw(page)).toBeEnabled();
});

test("with a separation running, Restart now saves and restarts", async ({ page }) => {
  await desktopSettings(page, { listening: false });
  await queue(page, "running");
  await openNetworkSettings(page);
  await toggle(page);

  await page.locator(".net-restart-go").click();
  await restartPending(page);
  expect(await calls(page)).toEqual(["POST /api/settings", "restart_backend"]);
});

test("a queue that cannot be read counts as a running separation", async ({ page }) => {
  await desktopSettings(page, { listening: false });
  await queue(page, "error");
  await openNetworkSettings(page);
  await toggle(page);

  await expect(dialogText(page)).toContainText(/separation is running/i);
  expect(await calls(page)).toEqual([]);
});

test("Escape closes the question and leaves Settings open", async ({ page }) => {
  await desktopSettings(page, { listening: false });
  await queue(page, "running");
  await openNetworkSettings(page);
  await toggle(page);
  await expect(dialogText(page)).toContainText(/separation is running/i);

  await page.keyboard.press("Escape");
  await expect(dialogText(page)).toHaveCount(0);
  await expect(sw(page)).toBeVisible();
  await expect(sw(page)).toBeFocused();
  expect(await calls(page)).toEqual([]);
});

test("a failed restart says the choice is saved; Try again only restarts", async ({ page }) => {
  await desktopSettings(page, { listening: false });
  await queue(page, "idle");
  await openNetworkSettings(page);
  await toggle(page);
  await restartPending(page);

  await page.evaluate(() => window.__e2e.failRestart("the audio backend did not shut down in time"));
  const msg = page.locator(".reset-confirm-msg");
  await expect(msg).toContainText("did not shut down in time");
  await expect(msg).toContainText(/setting is saved/i);
  await expect(page.locator(".net-restart-retry")).toBeFocused();

  // The backend may be down by now, so the retry must not need it.
  await page.locator(".net-restart-retry").click();
  await restartPending(page);
  expect(await calls(page)).toEqual(["POST /api/settings", "restart_backend", "restart_backend"]);

  // And the dialog can always be closed.
  await page.evaluate(() => window.__e2e.failRestart("still no"));
  await page.locator(".net-restart-cancel").click();
  await expect(dialogText(page)).toHaveCount(0);
  await expect(sw(page)).toBeEnabled();
});

test("a failed save restarts nothing, and Try again saves again", async ({ page }) => {
  await desktopSettings(page, { listening: false, postStatus: 500 });
  await queue(page, "idle");
  await openNetworkSettings(page);
  await toggle(page);

  await expect(page.locator(".reset-confirm-msg")).toContainText(/could not save/i);
  expect(await calls(page)).toEqual(["POST /api/settings"]);
  await page.locator(".net-restart-retry").click();
  await expect.poll(() => calls(page)).toEqual(["POST /api/settings", "POST /api/settings"]);
  expect(await calls(page)).not.toContain("restart_backend");
});

test("the switch shows what is listening, not only what was saved", async ({ page }) => {
  // Saved on, but the backend still listens on loopback: the switch is off,
  // so turning it on is what restarts onto the network.
  await desktopSettings(page, { listening: false, saved: true });
  await openNetworkSettings(page);
  await expect(sw(page)).not.toBeChecked();
});
