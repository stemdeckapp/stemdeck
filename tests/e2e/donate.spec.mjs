// The Donate button and dialog (static/js/donate.js).
//
// Donate sits in the left rail, below We Recommend and above Settings. The
// dialog is laid out like a donate page: a hero, then the ways to give (two
// tiles, preset amounts, a custom amount and an arrow) on the left and sharing
// StemDeck (QR, copy, share, star, releases) on the right.
//
// The links live in donateLinks.js: while the chosen way's link is the empty
// placeholder the arrow is a disabled "Coming soon" and the dialog holds no
// link for it; once it holds an https:// address the arrow opens it through the
// app's external-link path (open_url on desktop, a new tab in a browser). An
// optional template with {amount} carries the chosen amount; without one the
// arrow opens the plain link.
//
// The filled state is tested by serving a filled donateLinks.js, the same
// module the app reads, so a maintainer filling the file changes nothing else.
// The addresses are on example.org, a reserved domain, and are stubbed: no test
// reaches Stripe, Liberapay or GitHub.

import { test, expect } from "@playwright/test";
import { seedLibrary, stubTauri, stubUpdateCheck } from "./helpers.mjs";

const STRIPE = "https://example.org/stripe-test";
const LIBERAPAY = "https://example.org/liberapay-test";
const REPO = "https://github.com/stemdeckapp/stemdeck";

async function fillLinks(page, links, templates = { stripe: "", liberapay: "" }) {
  await page.route("**/js/donateLinks.js", (route) =>
    route.fulfill({
      contentType: "application/javascript",
      body:
        `export const DONATE_LINKS = Object.freeze(${JSON.stringify(links)});\n` +
        `export const DONATE_LINK_TEMPLATES = Object.freeze(${JSON.stringify(templates)});`,
    }),
  );
}

async function openApp(page, { tauri = false, language = null } = {}) {
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
const soon = (page) => page.locator("#donateSoon");
const custom = (page) => page.locator("#donateCustom");
const hint = (page) => page.locator("#donateHint");

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

test("Tab stays inside the dialog", async ({ page }) => {
  await fillLinks(page, { stripe: STRIPE, liberapay: LIBERAPAY });
  await openApp(page);
  await page.locator("#donateBtn").click();
  for (let i = 0; i < 24; i++) {
    await page.keyboard.press(i % 5 === 4 ? "Shift+Tab" : "Tab");
    expect(await page.evaluate(() => document.getElementById("donateDialog").contains(document.activeElement))).toBe(true);
  }
});

test("placeholder links: both ways show a disabled Coming soon and the dialog holds no payment link", async ({ page }) => {
  const { outside } = await openApp(page);
  await page.locator("#donateBtn").click();
  for (const way of ["stripe", "liberapay"]) {
    await tile(page, way).click();
    await expect(tile(page, way)).toHaveAttribute("aria-pressed", "true");
    await expect(soon(page)).toBeVisible();
    await expect(soon(page)).toBeDisabled();
    await expect(soon(page)).toHaveText("Coming soon");
    await expect(go(page)).toBeHidden();
    await expect(go(page)).not.toHaveAttribute("href", /./);
  }
  // Only the two GitHub links on the share side carry an address.
  const hrefs = await dialog(page).locator("a[href]").evaluateAll((els) => els.map((a) => a.href));
  expect(hrefs.sort()).toEqual([REPO, `${REPO}/releases`].sort());
  expect(outside).toEqual([]);
});

test("choosing a tile and an amount: one of each is pressed at a time", async ({ page }) => {
  await openApp(page);
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
  await expect(preset(page, 30)).toContainText("lunch for two");

  // Typing a custom amount lets go of the preset.
  await custom(page).fill("12");
  await expect(dialog(page).locator('.donate-preset[aria-pressed="true"]')).toHaveCount(0);
  // And a preset clears the custom field.
  await preset(page, 5).click();
  await expect(custom(page)).toHaveValue("");
});

test("the custom amount takes whole dollars from $2 and says why it changed", async ({ page }) => {
  await openApp(page);
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

test("a filled link: the arrow opens it through the desktop app's open_url, with no template the amount stays in the dialog", async ({ page }) => {
  await fillLinks(page, { stripe: STRIPE, liberapay: LIBERAPAY });
  const { outside } = await openApp(page, { tauri: true });
  await page.locator("#donateBtn").click();
  await expect(soon(page)).toBeHidden();
  await expect(go(page)).toBeVisible();
  await expect(go(page)).toHaveAttribute("target", "_blank");
  await expect(go(page)).toHaveAttribute("rel", /noopener/);
  await expect(go(page)).toHaveAttribute("aria-label", "Continue to Stripe in your web browser");

  await preset(page, 15).click();
  await expect(go(page)).toHaveAttribute("href", STRIPE);
  await go(page).click();
  await tile(page, "liberapay").click();
  await expect(go(page)).toHaveAttribute("aria-label", "Continue to Liberapay in your web browser");
  await custom(page).fill("40");
  await go(page).click();

  const opened = await page.evaluate(() => window.__e2e.callsFor("open_url").map((c) => c.args.url));
  expect(opened).toEqual([STRIPE, LIBERAPAY]);
  // The webview never navigated: open_url took the click.
  expect(outside).toEqual([]);
  await expect(dialog(page)).toBeVisible();
});

test("a filled link with an {amount} template carries the chosen amount", async ({ page }) => {
  await fillLinks(page, { stripe: STRIPE, liberapay: LIBERAPAY }, { stripe: `${STRIPE}?amount={amount}`, liberapay: "" });
  await openApp(page, { tauri: true });
  await page.locator("#donateBtn").click();
  // No amount yet: the plain link.
  await expect(go(page)).toHaveAttribute("href", STRIPE);
  await preset(page, 30).click();
  await expect(go(page)).toHaveAttribute("href", `${STRIPE}?amount=30`);
  await go(page).click();
  await custom(page).fill("1");
  await custom(page).press("Enter");
  await expect(go(page)).toHaveAttribute("href", `${STRIPE}?amount=2`);
  await go(page).click();
  // Liberapay has no template: its plain link, whatever the amount.
  await tile(page, "liberapay").click();
  await expect(go(page)).toHaveAttribute("href", LIBERAPAY);
  const opened = await page.evaluate(() => window.__e2e.callsFor("open_url").map((c) => c.args.url));
  expect(opened).toEqual([`${STRIPE}?amount=30`, `${STRIPE}?amount=2`]);
});

test("in a browser, the arrow opens the page in a new tab, and each way follows its own link", async ({ page, context }) => {
  await context.route("https://example.org/**", (route) => route.fulfill({ contentType: "text/html", body: "<p>stub</p>" }));
  await fillLinks(page, { stripe: STRIPE, liberapay: "" });
  await openApp(page);
  await page.locator("#donateBtn").click();
  const [tab] = await Promise.all([context.waitForEvent("page"), go(page).click()]);
  await tab.waitForLoadState("domcontentloaded");
  expect(tab.url()).toBe(STRIPE);
  await tile(page, "liberapay").click();
  await expect(soon(page)).toBeVisible();
  await expect(go(page)).toBeHidden();
  await expect(go(page)).not.toHaveAttribute("href", /./);
});

test("a link that is not https stays Coming soon", async ({ page }) => {
  await fillLinks(page, { stripe: "http://example.org/plain", liberapay: "liberapay.com/x" });
  await openApp(page);
  await page.locator("#donateBtn").click();
  for (const way of ["stripe", "liberapay"]) {
    await tile(page, way).click();
    await expect(soon(page)).toBeVisible();
    await expect(go(page)).toBeHidden();
  }
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
  const opened = await page.evaluate(() => window.__e2e.callsFor("open_url").map((c) => c.args.url));
  expect(opened).toEqual([REPO, `${REPO}/releases`]);
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

test("the Guide explains the button and every control in the dialog", async ({ page }) => {
  await fillLinks(page, { stripe: STRIPE, liberapay: "" });
  await openApp(page);
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
    [tile(page, "liberapay"), "Recurring donation", "through Liberapay"],
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
  // The live arrow (Stripe is filled here).
  await go(page).hover();
  await expect(tip.locator("b")).toHaveText("Continue to Stripe in your web browser");
  await expect(tip).toContainText("Opens the chosen way to donate");

  // A click in the Guide explains, it does not choose or open anything.
  await go(page).click();
  expect(page.context().pages()).toHaveLength(1);

});

test("the Guide explains Coming soon and names the chosen way", async ({ page }) => {
  await openApp(page);
  await page.locator("#helpModeBtn").click();
  await page.locator("#donateBtn").click();
  await expect(dialog(page)).toBeVisible();
  // The disabled button takes no pointer, so the pointer lands on its wrapper.
  await soon(page).hover({ force: true });
  await expect(page.locator("#helpTip b")).toHaveText("Coming soon");
  await expect(page.locator("#helpTip")).toContainText("Donating through Stripe is not set up yet");
});

test("the dialog follows the interface language", async ({ page }) => {
  await openApp(page, { language: "de" });
  await expect(page.locator("#donateBtn")).toHaveText("Spenden");
  await page.locator("#donateBtn").click();
  await expect(page.locator("#donateTitle")).toHaveText("Unterstütze StemDeck, frei und offen für alle");
  await expect(tile(page, "stripe")).toContainText("Einmalige Spende");
  await expect(soon(page)).toHaveText("Bald verfügbar");
  await expect(custom(page)).toHaveAttribute("placeholder", "Eigener Betrag (ab $2)");
  await custom(page).fill("1");
  await custom(page).press("Enter");
  await expect(hint(page)).toHaveText("Der kleinste Betrag ist $2.");
});

test("at phone width the cards stack and nothing scrolls sideways but the amounts", async ({ page }) => {
  await page.setViewportSize({ width: 400, height: 860 });
  await openApp(page);
  await page.locator("#donateBtn").click();
  const give = await page.locator(".donate-give").boundingBox();
  const share = await page.locator(".donate-share").boundingBox();
  expect(share.y).toBeGreaterThanOrEqual(give.y + give.height - 1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  const card = await page.locator(".donate-card").boundingBox();
  expect(card.x).toBeGreaterThanOrEqual(0);
  expect(card.x + card.width).toBeLessThanOrEqual(400);
  // The amounts row scrolls inside itself.
  expect(await page.locator(".donate-presets").evaluate((r) => r.scrollWidth > r.clientWidth)).toBe(true);
});
