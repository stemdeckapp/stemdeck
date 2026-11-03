// The Donate button and dialog (static/js/donate.js).
//
// Donate sits in the left rail, below We Recommend and above Settings. The
// dialog is laid out like a donate page: a hero, then the ways to give (two
// tiles, preset amounts, a custom amount and an arrow) on the left and sharing
// StemDeck (QR, copy, share, star, releases) on the right.
//
// The links live in donateLinks.js. A way whose link is not set (the empty
// placeholder, or anything that is not an https:// address) has no control at
// all: its tile is hidden, and with no way set the Donate button is hidden
// too. A set link opens through the app's external-link path (open_url on
// desktop, a new tab in a browser). An optional template with {amount} carries
// the chosen amount; without one the arrow opens the plain link.
//
// Each state is tested by serving the real donateLinks.js with only the two
// addresses replaced, so a maintainer filling the file changes nothing else.
// The addresses are on example.org, a reserved domain, and are stubbed: no test
// reaches Stripe, Liberapay or GitHub.

import { test, expect } from "@playwright/test";
import { seedLibrary, stubTauri, stubUpdateCheck } from "./helpers.mjs";

const STRIPE = "https://example.org/stripe-test";
const LIBERAPAY = "https://example.org/liberapay-test";
const REPO = "https://github.com/stemdeckapp/stemdeck";
const BOTH = { stripe: STRIPE, liberapay: LIBERAPAY };
const NONE = { stripe: "", liberapay: "" };

/** Serve the real donateLinks.js with these links (and templates) in place of the ones it holds. */
async function fillLinks(page, links, templates = null) {
  await page.route("**/js/donateLinks.js", async (route) => {
    const res = await route.fetch();
    const source = await res.text();
    const swap = (text, name, value) => {
      const out = text.replace(new RegExp(`(export const ${name} = Object\\.freeze\\()\\{[^}]*\\}`), (_, head) => head + JSON.stringify(value));
      if (out === text) throw new Error(`donateLinks.js has no ${name} block to fill`);
      return out;
    };
    let body = swap(source, "DONATE_LINKS", links);
    if (templates) body = swap(body, "DONATE_LINK_TEMPLATES", templates);
    await route.fulfill({ response: res, body });
  });
}

async function openApp(page, { tauri = false, language = null, links = BOTH, templates = null } = {}) {
  await fillLinks(page, links, templates);
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
  await openApp(page);
  await page.locator("#donateBtn").click();
  for (let i = 0; i < 24; i++) {
    await page.keyboard.press(i % 5 === 4 ? "Shift+Tab" : "Tab");
    expect(await page.evaluate(() => document.getElementById("donateDialog").contains(document.activeElement))).toBe(true);
  }
});

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

for (const [way, other, url] of [
  ["stripe", "liberapay", STRIPE],
  ["liberapay", "stripe", LIBERAPAY],
]) {
  test(`only ${way} set: Donate shows with only that way, already chosen, and the arrow goes there`, async ({ page }) => {
    await openApp(page, { links: { ...NONE, [way]: url } });
    await expect(page.locator("#donateBtn")).toBeVisible();
    await page.locator("#donateBtn").click();
    await expect(tile(page, way)).toBeVisible();
    await expect(tile(page, way)).toHaveAttribute("aria-pressed", "true");
    await expect(tile(page, other)).toBeHidden();
    // The one tile takes the whole row.
    const box = await tile(page, way).boundingBox();
    const row = await dialog(page).locator(".donate-ways").boundingBox();
    expect(Math.abs(box.width - row.width)).toBeLessThan(1);
    await expect(go(page)).toBeVisible();
    await expect(go(page)).toHaveAttribute("href", url);
  });
}

test("both links set: both ways show and each has its own link", async ({ page }) => {
  await openApp(page);
  await page.locator("#donateBtn").click();
  await expect(tile(page, "stripe")).toBeVisible();
  await expect(tile(page, "liberapay")).toBeVisible();
  await expect(go(page)).toHaveAttribute("href", STRIPE);
  await tile(page, "liberapay").click();
  await expect(go(page)).toHaveAttribute("href", LIBERAPAY);
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
  const { outside } = await openApp(page, { tauri: true });
  await page.locator("#donateBtn").click();
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
  await openApp(page, { tauri: true, templates: { stripe: `${STRIPE}?amount={amount}`, liberapay: "" } });
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

test("in a browser, the arrow opens the page in a new tab", async ({ page, context }) => {
  await context.route("https://example.org/**", (route) => route.fulfill({ contentType: "text/html", body: "<p>stub</p>" }));
  await openApp(page);
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
  // The arrow, for the chosen way.
  await go(page).hover();
  await expect(tip.locator("b")).toHaveText("Continue to Stripe in your web browser");
  await expect(tip).toContainText("Opens the chosen way to donate");

  // A click in the Guide explains, it does not choose or open anything.
  await go(page).click();
  expect(page.context().pages()).toHaveLength(1);

});

test("the dialog follows the interface language", async ({ page }) => {
  await openApp(page, { language: "de" });
  await expect(page.locator("#donateBtn")).toHaveText("Spenden");
  await page.locator("#donateBtn").click();
  await expect(page.locator("#donateTitle")).toHaveText("Unterstütze StemDeck, frei und offen für alle");
  await expect(tile(page, "stripe")).toContainText("Einmalige Spende");
  await expect(go(page)).toHaveAttribute("aria-label", "Weiter zu Stripe im Webbrowser");
  await expect(custom(page)).toHaveAttribute("placeholder", "Eigener Betrag (ab $2)");
  await custom(page).fill("1");
  await custom(page).press("Enter");
  await expect(hint(page)).toHaveText("Der kleinste Betrag ist $2.");
});

test("at phone width the cards stack and nothing scrolls sideways", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 860 });
  await openApp(page);
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
