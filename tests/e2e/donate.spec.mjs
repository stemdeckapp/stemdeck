// The Donate button and dialog (static/js/donate.js).
//
// Donate sits in the left rail, below We Recommend and above Settings. The
// dialog is laid out like a donate page: a hero, then the ways to give on the
// left and sharing StemDeck (QR, copy, share, star, releases) on the right.
//
// The links live in donateLinks.js. A way whose link is not set (the empty
// placeholder, or anything that is not an https:// address) has no control at
// all: its tile is hidden, and with no way set the Donate button is hidden
// too. A set link opens through the app's external-link path (open_url on
// desktop, a new tab in a browser).
//
// A way whose page takes no amount (no {amount} template) has a tile that is
// itself the link: pressing it opens the page, where the donor chooses. The
// preset amounts, the custom amount and the arrow show only while the chosen
// way has a template, which carries the amount picked here (#1037).
//
// Each state is tested by serving the real donateLinks.js with only the
// addresses replaced, so a maintainer filling the file changes nothing else.
// The addresses are on example.org, a reserved domain, and are stubbed: no test
// reaches Stripe, Liberapay or GitHub.

import { test, expect } from "@playwright/test";
import { seedLibrary, serveDonateLinks, stubTauri, stubUpdateCheck } from "./helpers.mjs";

const STRIPE = "https://example.org/stripe-test";
const LIBERAPAY = "https://example.org/liberapay-test";
const REPO = "https://github.com/stemdeckapp/stemdeck";
const BOTH = { stripe: STRIPE, liberapay: LIBERAPAY };
const NONE = { stripe: "", liberapay: "" };
// Both ways take the amount from their address.
const AMOUNTS = { stripe: `${STRIPE}?amount={amount}`, liberapay: `${LIBERAPAY}?amount={amount}` };
// Only Stripe takes it: Liberapay opens its own page, as it ships.
const STRIPE_AMOUNT = { stripe: `${STRIPE}?amount={amount}`, liberapay: "" };
const LANGUAGES = ["en", "pl", "ja", "zh-Hans", "de", "fr", "es", "pt", "pt-PT", "id", "ko"];

async function openApp(page, { tauri = false, language = null, links = BOTH, templates = null } = {}) {
  await serveDonateLinks(page, links, templates);
  if (language) await page.addInitScript((l) => localStorage.setItem("stemdeck.language", JSON.stringify(l)), language);
  await seedLibrary(page);
  if (tauri) await stubTauri(page);
  await stubUpdateCheck(page);
  // Any request to a payment platform or to GitHub fails the test that made it.
  const outside = [];
  page.on("request", (r) => {
    if (/^https:\/\/([^/]*\.)?(stripe\.com|liberapay\.com|example\.org)\/|^https:\/\/github\.com\//.test(r.url())) outside.push(r.url());
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.locator("#donateDialog")).toHaveAttribute("data-ready", "1");
  return { outside };
}

const dialog = (page) => page.locator("#donateDialog");
const tile = (page, way) => page.locator(`.donate-way[data-way="${way}"]`);
const preset = (page, amount) => page.locator(`.donate-preset[data-amount="${amount}"]`);
const go = (page) => page.locator("#donateGo");
const custom = (page) => page.locator("#donateCustom");
const hint = (page) => page.locator("#donateHint");
const presets = (page) => dialog(page).locator(".donate-presets");
const opened = (page) => page.evaluate(() => window.__e2e.callsFor("open_url").map((c) => c.args.url));
const tagOf = (loc) => loc.evaluate((el) => el.tagName);

/** The tile is a plain external link to `url`, not a choice. */
async function expectLinkTile(page, way, url, name) {
  const el = tile(page, way);
  await expect(el).toBeVisible();
  expect(await tagOf(el)).toBe("A");
  await expect(el).toHaveAttribute("href", url);
  await expect(el).toHaveAttribute("target", "_blank");
  await expect(el).toHaveAttribute("rel", "noopener noreferrer");
  await expect(el).not.toHaveAttribute("aria-pressed", /./);
  await expect(el).toHaveAccessibleName(name);
  await expect(el).toHaveAttribute("title", name);
  // The visible cue that it leaves the app, like the arrow has.
  await expect(el.locator(".donate-way-out")).toBeVisible();
}

/** No amount is offered: no presets, no custom box, no arrow, no hint. */
async function expectNoAmounts(page) {
  await expect(presets(page)).toBeHidden();
  await expect(custom(page)).toBeHidden();
  await expect(go(page)).toBeHidden();
  await expect(hint(page)).toBeHidden();
}

test("Donate sits in the rail between We Recommend and Settings", async ({ page }) => {
  await openApp(page);
  const btn = page.locator("#donateBtn");
  await expect(btn).toBeVisible();
  await expect(btn).toHaveText("Donate");
  await expect(btn).toHaveAttribute("title", "Support StemDeck with a donation");
  const order = await page.locator(".sidebar-rail > .rail-btn").evaluateAll((els) => els.map((e) => e.id).filter(Boolean));
  const at = order.indexOf("donateBtn");
  expect(order[at - 1]).toBe("friendsBtn");
  expect(order[at + 1]).toBe("settingsBtn");
});

test("the dialog opens, closes with Escape, the close button and the backdrop, and gives focus back", async ({ page }) => {
  await openApp(page);
  const btn = page.locator("#donateBtn");

  await btn.click();
  await expect(dialog(page)).toBeVisible();
  await expect(page.getByRole("dialog", { name: "Support StemDeck, free and open for everyone" })).toBeVisible();
  await expect(dialog(page)).toContainText("I make StemDeck on my own");
  await expect(dialog(page)).toContainText("every feature stays free, whether you donate or not");
  await expect(page.locator("#donateClose")).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog(page)).toBeHidden();
  await expect(btn).toBeFocused();

  await btn.click();
  await page.locator("#donateClose").click();
  await expect(dialog(page)).toBeHidden();
  await expect(btn).toBeFocused();

  await btn.click();
  await page.mouse.click(5, 5);
  await expect(dialog(page)).toBeHidden();
  await expect(btn).toBeFocused();
});

for (const [label, templates] of [["link tiles", null], ["amount tiles", AMOUNTS]]) {
  test(`Tab stays inside the dialog, with ${label}`, async ({ page }) => {
    await openApp(page, { templates });
    await page.locator("#donateBtn").click();
    for (let i = 0; i < 24; i++) {
      await page.keyboard.press(i % 5 === 4 ? "Shift+Tab" : "Tab");
      expect(await page.evaluate(() => document.getElementById("donateDialog").contains(document.activeElement))).toBe(true);
    }
  });
}

test("no link set: there is no Donate control anywhere, not even before the script runs", async ({ page }) => {
  // The page as served already holds the button hidden, so it never flashes.
  const html = await (await page.request.get("/")).text();
  expect(html).toMatch(/<button\b[^>]*\bid="donateBtn"[^>]*\shidden[\s>]/);

  const { outside } = await openApp(page, { links: NONE });
  await expect(page.locator("#donateBtn")).toBeHidden();
  await expect(dialog(page)).toBeHidden();
  // The dialog is closed, so its tiles are checked by their own hidden flag.
  await expect(tile(page, "stripe")).toHaveJSProperty("hidden", true);
  await expect(tile(page, "liberapay")).toHaveJSProperty("hidden", true);
  await expect(go(page)).not.toHaveAttribute("href", /./);
  expect(outside).toEqual([]);
});

for (const [way, other, url, name] of [
  ["stripe", "liberapay", STRIPE, "One-time donation via Stripe, opens in your web browser"],
  ["liberapay", "stripe", LIBERAPAY, "Recurring donation via Liberapay, opens in your web browser"],
]) {
  test(`only ${way} set, with no amount template: its one tile is a link to its page, and no amount is offered`, async ({ page }) => {
    const { outside } = await openApp(page, { links: { ...NONE, [way]: url } });
    await expect(page.locator("#donateBtn")).toBeVisible();
    await page.locator("#donateBtn").click();
    await expectLinkTile(page, way, url, name);
    await expect(tile(page, other)).toBeHidden();
    await expectNoAmounts(page);
    // The one tile takes the whole row.
    const box = await tile(page, way).boundingBox();
    const row = await dialog(page).locator(".donate-ways").boundingBox();
    expect(Math.abs(box.width - row.width)).toBeLessThan(1);
    expect(outside).toEqual([]);
  });
}

test("only Liberapay set: a press on its tile, or Tab then Enter, opens Liberapay through the desktop app's open_url", async ({ page }) => {
  const { outside } = await openApp(page, { tauri: true, links: { ...NONE, liberapay: LIBERAPAY } });
  await page.locator("#donateBtn").click();
  await tile(page, "liberapay").click();
  expect(await opened(page)).toEqual([LIBERAPAY]);

  // From the close button, the tile is the next stop, and Enter opens it.
  await page.locator("#donateClose").focus();
  await page.keyboard.press("Tab");
  await expect(tile(page, "liberapay")).toBeFocused();
  await page.keyboard.press("Enter");
  expect(await opened(page)).toEqual([LIBERAPAY, LIBERAPAY]);
  // The webview never navigated: open_url took both presses.
  expect(outside).toEqual([]);
  await expect(dialog(page)).toBeVisible();
});

test("in a browser, the Liberapay tile opens its page in a new tab", async ({ page, context }) => {
  await context.route("https://example.org/**", (route) => route.fulfill({ contentType: "text/html", body: "<p>stub</p>" }));
  await openApp(page, { links: { ...NONE, liberapay: LIBERAPAY } });
  await page.locator("#donateBtn").click();
  const [tab] = await Promise.all([context.waitForEvent("page"), tile(page, "liberapay").click()]);
  await tab.waitForLoadState("domcontentloaded");
  expect(tab.url()).toBe(LIBERAPAY);
  expect(page.url()).not.toContain("example.org");
});

test("only Stripe set, with an {amount} template: today's flow, the tile is chosen and the amount reaches the page", async ({ page }) => {
  await openApp(page, { tauri: true, links: { ...NONE, stripe: STRIPE }, templates: STRIPE_AMOUNT });
  await page.locator("#donateBtn").click();
  expect(await tagOf(tile(page, "stripe"))).toBe("BUTTON");
  await expect(tile(page, "stripe")).toHaveAttribute("aria-pressed", "true");
  await expect(tile(page, "stripe").locator(".donate-way-out")).toHaveCount(0);
  await expect(presets(page)).toBeVisible();
  await expect(custom(page)).toBeVisible();
  await expect(go(page)).toBeVisible();
  await expect(go(page)).toHaveAttribute("href", STRIPE);
  await preset(page, 30).click();
  await expect(go(page)).toHaveAttribute("href", `${STRIPE}?amount=30`);
  await go(page).click();
  expect(await opened(page)).toEqual([`${STRIPE}?amount=30`]);
});

test("Stripe takes an amount and Liberapay does not: Stripe is chosen with the amounts, the Liberapay tile opens its own page", async ({ page }) => {
  const { outside } = await openApp(page, { tauri: true, templates: STRIPE_AMOUNT });
  await page.locator("#donateBtn").click();
  expect(await tagOf(tile(page, "stripe"))).toBe("BUTTON");
  await expect(tile(page, "stripe")).toHaveAttribute("aria-pressed", "true");
  await expectLinkTile(page, "liberapay", LIBERAPAY, "Recurring donation via Liberapay, opens in your web browser");
  // The two tiles share the row; the amounts sit under them, for Stripe.
  const s = await tile(page, "stripe").boundingBox();
  const l = await tile(page, "liberapay").boundingBox();
  expect(Math.abs(s.y - l.y)).toBeLessThan(1);
  expect(Math.abs(s.width - l.width)).toBeLessThan(1.5);
  const row = await presets(page).boundingBox();
  expect(row.y).toBeGreaterThanOrEqual(s.y + s.height);

  await preset(page, 15).click();
  await expect(go(page)).toHaveAttribute("href", `${STRIPE}?amount=15`);
  await expect(go(page)).toHaveAttribute("aria-label", "Continue to Stripe in your web browser");
  // Liberapay opens directly; the choice for Stripe stays as it was.
  await tile(page, "liberapay").click();
  await expect(tile(page, "stripe")).toHaveAttribute("aria-pressed", "true");
  await expect(preset(page, 15)).toHaveAttribute("aria-pressed", "true");
  await expect(go(page)).toHaveAttribute("href", `${STRIPE}?amount=15`);
  await go(page).click();
  await custom(page).fill("1");
  await custom(page).press("Enter");
  await expect(go(page)).toHaveAttribute("href", `${STRIPE}?amount=2`);
  await go(page).click();

  expect(await opened(page)).toEqual([LIBERAPAY, `${STRIPE}?amount=15`, `${STRIPE}?amount=2`]);
  expect(outside).toEqual([]);
});

test("both ways take an amount: both are choices, and each has its own link", async ({ page }) => {
  await openApp(page, { templates: AMOUNTS });
  await page.locator("#donateBtn").click();
  await expect(tile(page, "stripe")).toBeVisible();
  await expect(tile(page, "liberapay")).toBeVisible();
  expect(await tagOf(tile(page, "liberapay"))).toBe("BUTTON");
  await expect(go(page)).toHaveAttribute("href", STRIPE);
  await tile(page, "liberapay").click();
  await expect(go(page)).toHaveAttribute("href", LIBERAPAY);
});

test("neither way takes an amount: both tiles are links, side by side, and no amount is offered", async ({ page }) => {
  await openApp(page);
  await page.locator("#donateBtn").click();
  await expectLinkTile(page, "stripe", STRIPE, "One-time donation via Stripe, opens in your web browser");
  await expectLinkTile(page, "liberapay", LIBERAPAY, "Recurring donation via Liberapay, opens in your web browser");
  await expectNoAmounts(page);
});

test("choosing a tile and an amount: one of each is pressed at a time", async ({ page }) => {
  await openApp(page, { templates: AMOUNTS });
  await page.locator("#donateBtn").click();
  await expect(tile(page, "stripe")).toHaveAttribute("aria-pressed", "true");
  await expect(tile(page, "liberapay")).toHaveAttribute("aria-pressed", "false");
  await tile(page, "liberapay").click();
  await expect(tile(page, "liberapay")).toHaveAttribute("aria-pressed", "true");
  await expect(tile(page, "stripe")).toHaveAttribute("aria-pressed", "false");

  await expect(dialog(page).locator('.donate-preset[aria-pressed="true"]')).toHaveCount(0);
  await preset(page, 15).click();
  await expect(preset(page, 15)).toHaveAttribute("aria-pressed", "true");
  await preset(page, 30).click();
  await expect(preset(page, 30)).toHaveAttribute("aria-pressed", "true");
  await expect(dialog(page).locator('.donate-preset[aria-pressed="true"]')).toHaveCount(1);
  await expect(preset(page, 30)).toContainText("$30");

  // Typing a custom amount lets go of the preset.
  await custom(page).fill("12");
  await expect(dialog(page).locator('.donate-preset[aria-pressed="true"]')).toHaveCount(0);
  // And a preset clears the custom field.
  await preset(page, 5).click();
  await expect(custom(page)).toHaveValue("");
});

test("the custom amount takes whole dollars from $2 and says why it changed", async ({ page }) => {
  await openApp(page, { templates: AMOUNTS });
  await page.locator("#donateBtn").click();
  await expect(custom(page)).toHaveAttribute("placeholder", "Custom amount (from $2)");
  await expect(hint(page)).toBeHidden();

  await custom(page).pressSequentially("1x");
  await expect(custom(page)).toHaveValue("1");
  await expect(hint(page)).toHaveText("Numbers only, in whole US dollars.");

  await custom(page).fill("1");
  await expect(custom(page)).toHaveValue("1");
  await custom(page).press("Enter");
  await expect(custom(page)).toHaveValue("2");
  await expect(hint(page)).toHaveText("The smallest amount is $2.");

  await custom(page).fill("250000");
  await custom(page).blur();
  await expect(custom(page)).toHaveValue("10000");
  await expect(hint(page)).toHaveText("The largest amount here is $10,000.");

  await custom(page).fill("25");
  await expect(hint(page)).toBeHidden();
});

test("the arrow opens the chosen way, with its amount, through the desktop app's open_url", async ({ page }) => {
  const { outside } = await openApp(page, { tauri: true, templates: AMOUNTS });
  await page.locator("#donateBtn").click();
  await expect(go(page)).toBeVisible();
  await expect(go(page)).toHaveAttribute("target", "_blank");
  await expect(go(page)).toHaveAttribute("rel", /noopener/);
  await expect(go(page)).toHaveAttribute("aria-label", "Continue to Stripe in your web browser");

  await preset(page, 15).click();
  await expect(go(page)).toHaveAttribute("href", `${STRIPE}?amount=15`);
  await go(page).click();
  await tile(page, "liberapay").click();
  await expect(go(page)).toHaveAttribute("aria-label", "Continue to Liberapay in your web browser");
  await custom(page).fill("40");
  await go(page).click();

  expect(await opened(page)).toEqual([`${STRIPE}?amount=15`, `${LIBERAPAY}?amount=40`]);
  // The webview never navigated: open_url took the click.
  expect(outside).toEqual([]);
  await expect(dialog(page)).toBeVisible();
});

test("in a browser, the arrow opens the page in a new tab", async ({ page, context }) => {
  await context.route("https://example.org/**", (route) => route.fulfill({ contentType: "text/html", body: "<p>stub</p>" }));
  await openApp(page, { templates: AMOUNTS });
  await page.locator("#donateBtn").click();
  const [tab] = await Promise.all([context.waitForEvent("page"), go(page).click()]);
  await tab.waitForLoadState("domcontentloaded");
  expect(tab.url()).toBe(STRIPE);
});

test("a link that is not https counts as not set", async ({ page }) => {
  await openApp(page, { links: { stripe: "http://example.org/plain", liberapay: "liberapay.com/x" } });
  await expect(page.locator("#donateBtn")).toBeHidden();
});

test("share: the QR code comes from this server, copy puts the address on the clipboard, star and releases open GitHub", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await openApp(page, { tauri: true });
  const qrRequests = [];
  page.on("request", (r) => {
    if (r.url().includes("/api/qr")) qrRequests.push(r.url());
  });
  await page.locator("#donateBtn").click();
  const qr = page.locator("#donateQr");
  await expect(qr).toHaveAttribute("src", `/api/qr?url=${encodeURIComponent(REPO)}`);
  await expect.poll(() => qr.evaluate((img) => img.complete && img.naturalWidth)).toBeGreaterThan(0);
  expect(qrRequests).toHaveLength(1);
  await expect(qr).toHaveAttribute("alt", "QR code for the StemDeck page on GitHub");
  await expect(dialog(page).locator(".donate-addr")).toHaveText("github.com/stemdeckapp/stemdeck");

  const copy = page.locator("#donateCopy");
  await copy.click();
  await expect(copy).toHaveText("Copied");
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(REPO);
  await expect(copy).toHaveText("Copy", { timeout: 4000 });

  // Share shows only where the system has a share menu.
  const canShare = await page.evaluate(() => typeof navigator.share === "function");
  await expect(page.locator("#donateShare")).toBeVisible({ visible: canShare });

  await page.locator("#donateStar").click();
  await page.locator("#donateReleases").click();
  expect(await opened(page)).toEqual([REPO, `${REPO}/releases`]);
});

test("share uses the system share menu when there is one", async ({ page }) => {
  await page.addInitScript(() => {
    window.__shared = [];
    navigator.share = async (data) => {
      window.__shared.push(data);
    };
  });
  await openApp(page);
  await page.locator("#donateBtn").click();
  await expect(page.locator("#donateShare")).toBeVisible();
  await page.locator("#donateShare").click();
  await expect.poll(() => page.evaluate(() => window.__shared)).toEqual([{ title: "StemDeck", url: REPO }]);
});

test("the Guide explains the button and every control in the dialog, with amount tiles", async ({ page }) => {
  await openApp(page, { templates: AMOUNTS });
  await page.locator("#helpModeBtn").click();
  await expect(page.locator("body")).toHaveClass(/help-mode/);
  const tip = page.locator("#helpTip");

  await page.locator("#donateBtn").hover();
  await expect(tip).toContainText("supporting StemDeck with a donation");
  // Donate opens its dialog in the Guide, like a menu, so its parts explain themselves.
  await page.locator("#donateBtn").click();
  await expect(dialog(page)).toBeVisible();

  const cases = [
    [tile(page, "stripe"), "One-time donation", "through Stripe"],
    [tile(page, "liberapay"), "Recurring donation", "Chooses a recurring donation through Liberapay"],
    [preset(page, 10), "$10", "Picks this amount in US dollars"],
    [custom(page), "Custom amount in US dollars, from $2", "Type your own amount"],
    [page.locator("#donateQr"), "QR code for the StemDeck page on GitHub", "Scan this with a phone camera"],
    [page.locator("#donateCopy"), "Copy", "Copies the address"],
    [page.locator("#donateStar"), "Star", "give it a star"],
    [page.locator("#donateReleases"), "Releases", "follow new versions"],
    [page.locator("#donateClose"), null, "Closes the donate window"],
  ];
  for (const [el, title, text] of cases) {
    await el.hover();
    if (title) await expect(tip.locator("b")).toHaveText(title);
    await expect(tip).toContainText(text);
  }
  // The arrow, for the chosen way.
  await go(page).hover();
  await expect(tip.locator("b")).toHaveText("Continue to Stripe in your web browser");
  await expect(tip).toContainText("Opens the chosen way to donate");

  // A click in the Guide explains, it does not choose or open anything.
  await go(page).click();
  expect(page.context().pages()).toHaveLength(1);
});

test("the Guide says where a link tile goes, and a press in the Guide opens nothing", async ({ page }) => {
  await openApp(page, { tauri: true });
  await page.locator("#helpModeBtn").click();
  await page.locator("#donateBtn").click();
  await expect(dialog(page)).toBeVisible();
  const tip = page.locator("#helpTip");

  await tile(page, "stripe").hover();
  await expect(tip.locator("b")).toHaveText("One-time donation");
  await expect(tip).toContainText("Opens Stripe in your web browser for a one-time donation by card");
  await tile(page, "liberapay").hover();
  await expect(tip.locator("b")).toHaveText("Recurring donation");
  await expect(tip).toContainText("Opens Liberapay in your web browser, where you choose the amount and how often to give");
  await expect(tip).toContainText("can be stopped at any time");

  await tile(page, "liberapay").click();
  await expect(tip).toContainText("Opens Liberapay");
  expect(await opened(page)).toEqual([]);
  expect(page.context().pages()).toHaveLength(1);
});

test("the dialog follows the interface language", async ({ page }) => {
  await openApp(page, { language: "de", templates: STRIPE_AMOUNT });
  await expect(page.locator("#donateBtn")).toHaveText("Spenden");
  await page.locator("#donateBtn").click();
  await expect(page.locator("#donateTitle")).toHaveText("Unterstütze StemDeck, frei und offen für alle");
  await expect(tile(page, "stripe")).toContainText("Einmalige Spende");
  await expect(tile(page, "liberapay")).toHaveAccessibleName("Regelmäßige Spende über Liberapay, öffnet sich im Webbrowser");
  await expect(go(page)).toHaveAttribute("aria-label", "Weiter zu Stripe im Webbrowser");
  await expect(custom(page)).toHaveAttribute("placeholder", "Eigener Betrag (ab $2)");
  await custom(page).fill("1");
  await custom(page).press("Enter");
  await expect(hint(page)).toHaveText("Der kleinste Betrag ist $2.");
});

test("at phone width the cards stack and nothing scrolls sideways", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 860 });
  await openApp(page, { templates: AMOUNTS });
  await page.locator("#donateBtn").click();
  const give = await page.locator(".donate-give").boundingBox();
  const share = await page.locator(".donate-share").boundingBox();
  expect(share.y).toBeGreaterThanOrEqual(give.y + give.height - 1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  const card = await page.locator(".donate-card").boundingBox();
  expect(card.x).toBeGreaterThanOrEqual(0);
  expect(card.x + card.width).toBeLessThanOrEqual(360);
  // The four amounts share the row evenly, on one line, with no scrolling.
  expect(await page.locator(".donate-presets").evaluate((r) => r.scrollWidth <= r.clientWidth)).toBe(true);
  const boxes = await page.locator(".donate-preset").evaluateAll((els) => els.map((e) => e.getBoundingClientRect()));
  expect(boxes).toHaveLength(4);
  for (const b of boxes) expect(Math.abs(b.y - boxes[0].y)).toBeLessThan(1);
  for (const b of boxes) expect(Math.abs(b.width - boxes[0].width)).toBeLessThan(1.5);
});

// Every language, at 950 px and at phone width: no tile text spills out of its
// tile or runs under the cue that it opens in the browser.
for (const [label, templates] of [["link tiles", null], ["one amount tile and one link tile", STRIPE_AMOUNT]]) {
  test(`${label} fit in every language, at 950 px and at phone width`, async ({ page }) => {
    // Eleven reloads in one test, each well under the usual 45 s.
    test.setTimeout(120_000);
    await openApp(page, { templates });
    const problems = [];
    for (const lang of LANGUAGES) {
      await page.evaluate((l) => localStorage.setItem("stemdeck.language", JSON.stringify(l)), lang);
      await page.reload({ waitUntil: "domcontentloaded" });
      await expect(dialog(page)).toHaveAttribute("data-ready", "1");
      await page.locator("#donateBtn").click();
      for (const width of [950, 360]) {
        await page.setViewportSize({ width, height: 900 });
        await expect(tile(page, "liberapay")).toBeVisible();
        const found = await page.evaluate(() => {
          const out = [];
          if (document.documentElement.scrollWidth > innerWidth) out.push("page scrolls sideways");
          for (const el of document.querySelectorAll(".donate-way:not([hidden])")) {
            const box = el.getBoundingClientRect();
            if (el.scrollWidth > el.clientWidth + 1) out.push(`${el.dataset.way} spills out`);
            const cue = el.querySelector(".donate-way-out")?.getBoundingClientRect();
            for (const part of el.querySelectorAll(".donate-way-name, .donate-way-via")) {
              const r = part.getBoundingClientRect();
              if (r.right > box.right + 0.5) out.push(`${el.dataset.way} ${part.className} past the edge`);
              if (cue && r.right > cue.left && r.left < cue.right && r.bottom > cue.top && r.top < cue.bottom) {
                out.push(`${el.dataset.way} ${part.className} under the cue`);
              }
            }
          }
          return out;
        });
        for (const p of found) problems.push(`${lang} ${width}px: ${p}`);
      }
      await page.setViewportSize({ width: 1280, height: 900 });
    }
    expect(problems).toEqual([]);
  });
}
