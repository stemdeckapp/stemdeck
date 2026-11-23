// Online search and link import are opt-in (#1089).
//
// Off by default, the page does not offer them: the box says it takes a file,
// typing in it shows one line pointing to Settings, Experimental instead of
// results, and a link pressed through anyway gets the server's refusal said
// in words. Settings, Experimental always shows the responsibility text, and
// the switch turns on only once the box under it is ticked. Turning it off
// asks nothing. The phone page shows the same line in place of its link field.
//
// This spec talks to the real backend for the setting and for the refused
// import, and puts the setting back to off after every test: the backend is
// shared by the whole suite. The server half (every gated endpoint, the
// default for an old settings file) is in tests/test_online_sources.py.

import { expect, test } from "@playwright/test";

import { seedLibrary, stubUpdateCheck } from "./helpers.mjs";

const OFF_LINE = "Online search and link import are off. Turn them on in Settings, Experimental.";
const LINK = "https://www.youtube.com/watch?v=dQw4w9WgXcQ";

const setting = async (request) => (await (await request.get("/api/settings")).json()).online_sources;

test.beforeEach(async ({ request }) => {
  await request.post("/api/settings", { data: { online_sources: false } });
});

test.afterEach(async ({ request }) => {
  await request.post("/api/settings", { data: { online_sources: false } });
});

async function openApp(page) {
  await seedLibrary(page);
  await stubUpdateCheck(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#url").waitFor({ state: "visible" });
}

async function openExperimental(page) {
  await page.locator("#settingsBtn").click();
  await expect(page.locator(".library-editor")).toBeVisible();
  await page.locator('.settings-tab[data-tab="experimental"]').click();
  await expect(page.locator('.settings-pane[data-pane="experimental"]')).toBeVisible();
}

test.describe("online sources off", () => {
  test("the box offers a file, and typing points to Settings without searching", async ({ page, request }) => {
    expect(await setting(request)).toBe(false);
    const searches = [];
    await page.route("**/api/search", (route) => {
      searches.push(route.request().url());
      return route.fulfill({ json: { items: [], max_duration_sec: 1200 } });
    });
    await openApp(page);

    const box = page.locator("#url");
    await expect(box).toHaveAttribute("placeholder", "Drop or upload an audio file…");
    await box.type("daft punk ", { delay: 20 });
    const panel = page.locator(".search-panel");
    await expect(panel).toBeVisible();
    await expect(panel.locator(".search-status")).toHaveText(OFF_LINE);
    await expect(panel.locator(".search-tabs")).toBeHidden();
    await expect(panel.locator(".search-row")).toHaveCount(0);

    // A pasted link gets the same line, not a silent box.
    await box.fill(LINK);
    await expect(panel.locator(".search-status")).toHaveText(OFF_LINE);
    expect(searches).toEqual([]);
  });

  test("a link pressed through anyway is refused by the server and said in words", async ({ page, request }) => {
    await openApp(page);
    const before = (await (await request.get("/api/jobs")).json()).length;
    await page.locator("#url").fill(LINK);
    const answer = page.waitForResponse((r) => r.url().endsWith("/api/jobs") && r.request().method() === "POST");
    await page.locator("#submit").click();
    expect((await answer).status()).toBe(403);
    await expect(page.locator("#error .error-msg")).toHaveText(OFF_LINE);
    // No Try again: the same request would be refused the same way.
    await expect(page.locator("#error .retry-btn")).toHaveCount(0);
    expect((await (await request.get("/api/jobs")).json()).length).toBe(before);
  });

  test("the phone page shows the line in place of its link field", async ({ page }) => {
    await page.goto("/?ui=mobile", { waitUntil: "domcontentloaded" });
    await page.locator('[data-action="tab"][data-tab="extract"]').first().click();
    await expect(page.locator(".paste-off")).toHaveText(
      "Importing from a link is off. It can be turned on in Settings, Experimental, on a computer.",
    );
    await expect(page.locator("#ext-url")).toHaveCount(0);
    // Upload is still offered.
    await expect(page.locator('[data-action="pick-file"]')).toBeVisible();
  });
});

test.describe("Settings, Experimental", () => {
  test("shows the text always, and turns on only after the box is ticked", async ({ page, request }) => {
    await openApp(page);
    await openExperimental(page);
    const pane = page.locator('.settings-pane[data-pane="experimental"]');
    const sw = pane.locator(".online-sources-input");
    const accept = pane.locator(".online-sources-accept-input");
    const msg = pane.locator(".online-sources-msg");

    await expect(pane.locator(".online-sources-disclaimer")).toHaveText(
      "You alone are responsible for what you search for and download, and for following the terms of each service and copyright law. StemDeck only provides a connection to the open source tool yt-dlp and has no responsibility for how it is used.",
    );
    // The cookies moved here from General.
    await expect(pane.locator(".set-cookies-file")).toHaveCount(1);
    await expect(page.locator('.settings-pane[data-pane="general"] .set-cookies-file')).toHaveCount(0);

    await expect(sw).not.toBeChecked();
    await expect(accept).not.toBeChecked();
    await expect(msg).toHaveText("Tick the box above to turn this on.");

    // Without the tick the switch does not turn on, says why, and sends the
    // user to the box. Nothing is saved.
    await sw.click({ force: true });
    await expect(sw).not.toBeChecked();
    await expect(accept).toBeFocused();
    expect(await setting(request)).toBe(false);

    await accept.check();
    await expect(msg).toHaveText("");
    const saved = page.waitForResponse((r) => r.url().endsWith("/api/settings") && r.request().method() === "POST");
    await sw.click({ force: true });
    await saved;
    await expect(sw).toBeChecked();
    await expect(accept).toBeChecked();
    await expect(accept).toBeDisabled();
    expect(await setting(request)).toBe(true);

    // The box at the top follows at once, without a reload.
    await expect(page.locator("#url")).toHaveAttribute("placeholder", "Search or drop an audio file…");

    // Off asks nothing, and the next time on needs a new tick.
    const off = page.waitForResponse((r) => r.url().endsWith("/api/settings") && r.request().method() === "POST");
    await sw.click({ force: true });
    await off;
    await expect(sw).not.toBeChecked();
    await expect(accept).not.toBeChecked();
    await expect(accept).toBeEnabled();
    expect(await setting(request)).toBe(false);
    await expect(page.locator("#url")).toHaveAttribute("placeholder", "Drop or upload an audio file…");
  });

  test("on after a reload, search is offered again", async ({ page, request }) => {
    await request.post("/api/settings", { data: { online_sources: true } });
    await page.route("**/api/search", (route) =>
      route.fulfill({ json: { max_duration_sec: 1200, items: [{ title: "One More Time", url: LINK, duration: 320, uploader: "Daft Punk" }] } }),
    );
    await openApp(page);
    await expect(page.locator("#url")).toHaveAttribute("placeholder", "Search or drop an audio file…");
    await page.locator("#url").type("daft punk ", { delay: 20 });
    await expect(page.locator(".search-row").first()).toBeVisible();
    await expect(page.locator(".search-tabs")).toBeVisible();

    await openExperimental(page);
    const pane = page.locator('.settings-pane[data-pane="experimental"]');
    await expect(pane.locator(".online-sources-input")).toBeChecked();
    await expect(pane.locator(".online-sources-accept-input")).toBeChecked();
  });
});
