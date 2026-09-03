// Every Settings tab scrolls inside the dialog.
//
// The dialog is a fixed height so that switching tabs never resizes it, and
// each pane is flex:1 to fill it. For a long time only the General pane had
// overflow-y, which was a bet that no other tab would outgrow 540px. Network
// did, once it gained the secure-origin warning and one QR card per network
// interface, and a pane with no overflow does not clip: it runs on underneath,
// leaving the Done button sitting on top of the Port setting.
//
// So these tests assert the containment rather than any particular height. A
// pane is allowed to be as tall as it likes; what it may not do is escape the
// dialog or collide with the footer.

import { test, expect } from "@playwright/test";
import { seedLibrary } from "./helpers.mjs";

const TABS = ["general", "network", "export", "logs", "registry"];

async function openSettings(page) {
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  await expect(page.locator(".library-editor")).toBeVisible();
}

/** Make the Network tab as tall as it gets: warning plus several QR cards. */
async function withManyAddresses(page) {
  await page.route("**/api/settings", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const resp = await route.fetch();
    const body = await resp.json();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ...body,
        allow_network: true,
        lan_addresses: [
          "http://192.168.1.50:8000",
          "http://10.0.0.7:8000",
          "http://172.28.224.1:8000",
        ],
      }),
    });
  });
}

async function showTab(page, name) {
  await page.locator(`.settings-tab[data-tab="${name}"]`).click();
  return page.locator(`.settings-pane[data-pane="${name}"]`);
}

// The invariant, stated the only way that actually distinguishes the two
// cases. Measuring boxes cannot: a pane that overflows keeps its own
// constrained box and merely paints its children outside it, while a pane that
// scrolls correctly has children below the fold whose rects also sit past the
// bottom. Both look identical to getBoundingClientRect. What separates them is
// whether the pane clips.
for (const name of TABS) {
  test(`the ${name} tab clips and scrolls rather than painting outside`, async ({ page }) => {
    await withManyAddresses(page);
    await openSettings(page);
    const pane = await showTab(page, name);
    const overflow = await pane.evaluate((el) => getComputedStyle(el).overflowY);
    expect(overflow, `${name} pane would overlap the footer once it grows`).not.toBe("visible");
  });
}

test("the dialog is one size on every tab", async ({ page }) => {
  // The reason panes are flex:1 in the first place. If a tab could stretch it,
  // the fix for the overlap would just be a resizing dialog instead.
  await withManyAddresses(page);
  await openSettings(page);
  const heights = [];
  for (const name of TABS) {
    await showTab(page, name);
    heights.push((await page.locator(".library-editor").boundingBox()).height);
  }
  expect(Math.max(...heights) - Math.min(...heights)).toBeLessThanOrEqual(2);
});

test("the network tab scrolls rather than overflowing", async ({ page }) => {
  await withManyAddresses(page);
  await openSettings(page);
  const pane = await showTab(page, "network");
  const { scrollable, canScroll } = await pane.evaluate((el) => ({
    scrollable: getComputedStyle(el).overflowY,
    canScroll: el.scrollHeight > el.clientHeight,
  }));
  expect(scrollable).toBe("auto");
  // The fixture is deliberately tall enough that this is a real scroll, not a
  // property that happens to be set on content which never needed it.
  expect(canScroll).toBe(true);
  await pane.evaluate((el) => el.scrollTo(0, el.scrollHeight));
  expect(await pane.evaluate((el) => el.scrollTop)).toBeGreaterThan(0);
});

test("the Done button stays reachable at the bottom of a long tab", async ({ page }) => {
  await withManyAddresses(page);
  await openSettings(page);
  await showTab(page, "network");
  const done = page.locator(".settings-done");
  await expect(done).toBeVisible();
  // Not merely present: actually clickable where it is drawn.
  await done.click();
  await expect(page.locator(".library-editor")).toHaveCount(0);
});
