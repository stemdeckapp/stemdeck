// The warning attached to "Make StemDeck available on your network".
//
// Handing someone a LAN address for their phone has a consequence the address
// itself does not show. Transpose is an AudioWorklet, and browsers hand that
// out only on a secure origin, so over plain http the key control on the phone
// is simply dead with nothing on screen to say why. Over https with StemDeck's
// own certificate the phone instead throws a full-page "your connection is not
// private" warning, which looks like the app is unsafe when it is not.
//
// Both are the moment to say something, and they need opposite text. These
// tests pin that the right one appears, because the failure mode is silent:
// the setting looks perfectly fine either way.

import { test, expect } from "@playwright/test";
import { seedLibrary } from "./helpers.mjs";

const warning = (page) => page.locator(".settings-net-warn");

async function openSettings(page) {
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  // The overlay opens on General; this setting lives under Network.
  await page.locator('.settings-tab[data-tab="network"]').click();
  await expect(page.locator(".net-access-input")).toBeVisible();
}

/** Answer /api/settings with a chosen set of LAN addresses. */
async function withAddresses(page, addresses) {
  await page.route("**/api/settings", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const resp = await route.fetch();
    const body = await resp.json();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ...body, allow_network: true, lan_addresses: addresses }),
    });
  });
}

test("a plain http address warns that the key control will not work", async ({ page }) => {
  await withAddresses(page, ["http://192.168.1.50:8000"]);
  await openSettings(page);
  await expect(warning(page)).toBeVisible();
  await expect(warning(page)).toContainText(/secure connection/i);
  await expect(warning(page)).toContainText(/https/i);
});

test("an https address explains the certificate prompt instead", async ({ page }) => {
  await withAddresses(page, ["https://192.168.1.50:8000"]);
  await openSettings(page);
  await expect(warning(page)).toBeVisible();
  // The phone's own words, so the user can match what they are seeing.
  await expect(warning(page)).toContainText(/not private/i);
  await expect(warning(page)).toContainText(/continue/i);
});

test("the two messages are not both shown", async ({ page }) => {
  await withAddresses(page, ["https://192.168.1.50:8000"]);
  await openSettings(page);
  await expect(warning(page)).not.toContainText(/secure connection \(https\)/i);
});

test("it is red, not another quiet grey note", async ({ page }) => {
  // The setting already carries two grey explanatory lines. A third would be
  // read as more of the same and skipped, which defeats the point of writing it.
  await withAddresses(page, ["http://192.168.1.50:8000"]);
  await openSettings(page);
  const colour = await warning(page).evaluate((el) => getComputedStyle(el).color);
  const [r, g, b] = colour.match(/\d+/g).map(Number);
  expect(r).toBeGreaterThan(g + 40);
  expect(r).toBeGreaterThan(b + 40);
});

test("nothing is said when there is no address to hand out", async ({ page }) => {
  await withAddresses(page, []);
  await openSettings(page);
  await expect(warning(page)).toBeHidden();
});
