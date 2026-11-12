// The Donate button and dialog (static/js/donate.js).
//
// Donate sits in the left rail, below We Recommend and above Settings. The
// dialog is laid out like a donate page: a hero, then the ways to give on the
// left and sharing StemDeck (QR, share, star, follow links) on the right.
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

async function openApp(page, { tauri = false, language = null, links = BOTH, templates = null, qr = null, social = null } = {}) {
  await serveDonateLinks(page, links, templates, qr, social);
  if (language) await page.addInitScript((l) => localStorage.setItem("stemdeck.language", JSON.stringify(l)), language);
  await seedLibrary(page);
  if (tauri) await stubTauri(page);
  await stubUpdateCheck(page);
  // Any request to a payment platform or to GitHub fails the test that made it.
  const outside = [];
  page.on("request", (r) => {
    if (/^https:\/\/([^/]*\.)?(stripe\.com|liberapay\.com|example\.org|instagram\.com|x\.com)\/|^https:\/\/github\.com\//.test(r.url())) outside.push(r.url());
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

const WAY_LINKS = { stripe: STRIPE, liberapay: LIBERAPAY };
const WAY_NAMES = { stripe: "One-time donation via Stripe, opens in your web browser", liberapay: "Recurring donation via Liberapay, opens in your web browser" };

test("both link tiles show a QR code of their own link, and each is still one link", async ({ page }) => {
  const { outside } = await openApp(page, { links: BOTH });
  const requested = [];
  page.on("request", (r) => { if (r.url().includes("/api/qr")) requested.push(new URL(r.url()).searchParams.get("url")); });
  // Nothing is drawn before the dialog opens.
  for (const way of ["stripe", "liberapay"]) {
    await expect(tile(page, way).locator("img.donate-way-qr")).toHaveCount(1);
    expect(await tile(page, way).locator("img").getAttribute("src")).toBeNull();
  }
  expect(requested).toEqual([]);
  await page.locator("#donateBtn").click();
  for (const [way, link] of Object.entries(WAY_LINKS)) {
    const qr = tile(page, way).locator("img.donate-way-qr");
    await expect(qr).toHaveAttribute("src", `/api/qr?url=${encodeURIComponent(link)}`);
    await expect(qr).toHaveAttribute("alt", "");
    await expect.poll(() => qr.evaluate((i) => i.complete && i.naturalWidth > 0)).toBe(true);
    expect(requested).toContain(link);
    // One single link: the tile is the anchor, the code sits inside it.
    await expect(tile(page, way)).toHaveJSProperty("tagName", "A");
    await expect(tile(page, way)).toHaveAttribute("href", link);
    await expect(tile(page, way).locator("a")).toHaveCount(0);
    await expectLinkTile(page, way, link, WAY_NAMES[way]);
  }
  // A matched pair: the same height.
  const [s1, s2] = [await tile(page, "stripe").boundingBox(), await tile(page, "liberapay").boundingBox()];
  expect(Math.abs(s1.height - s2.height)).toBeLessThan(1);
  expect(outside).toEqual([]);
});

for (const withShare of [false, true]) {
test(`the share card is laid out like the donation tiles, ${withShare ? "with" : "without"} a system share menu: QR card, caption, address, then one row of buttons`, async ({ page }) => {
  if (withShare) await page.addInitScript(() => { navigator.share = async () => {}; });
  const { outside } = await openApp(page, { links: BOTH });
  for (const width of [1280, 927, 360]) {
    await page.setViewportSize({ width, height: 900 });
    await page.locator("#donateBtn").click();
    const qr = page.locator("#donateQr");
    await expect.poll(() => qr.evaluate((i) => i.complete && i.naturalWidth > 0)).toBe(true);
    const caption = page.locator(".donate-share-scan .donate-way-scan-caption");
    await expect(caption).toHaveText("Scan with your phone");
    const addr = page.locator(".donate-addr");
    await expect(addr).toHaveText("github.com/stemdeckapp/stemdeck");
    const card = await page.locator(".donate-share").boundingBox();
    const box = {
      qr: await qr.boundingBox(), caption: await caption.boundingBox(), addr: await addr.boundingBox(),
      btns: await page.locator(".donate-share-btns .donate-sq:not(.donate-social):visible").evaluateAll((els) => els.map((e) => e.getBoundingClientRect().toJSON())),
    };
    // Centred, in order, and inside the card.
    expect(Math.abs(box.qr.x + box.qr.width / 2 - (card.x + card.width / 2))).toBeLessThan(2);
    expect(box.caption.y).toBeGreaterThanOrEqual(box.qr.y + box.qr.height - 1);
    expect(box.addr.y).toBeGreaterThanOrEqual(box.caption.y + box.caption.height - 1);
    expect(box.btns).toHaveLength(withShare ? 2 : 1);
    await expect(page.locator("#donateShare")).toBeVisible({ visible: withShare });
    for (const b of box.btns) {
      expect(Math.abs(b.y - box.btns[0].y)).toBeLessThan(1);
      expect(Math.abs(b.width - box.btns[0].width)).toBeLessThan(1.5);
      expect(b.y).toBeGreaterThanOrEqual(box.addr.y + box.addr.height - 1);
      expect(b.right).toBeLessThanOrEqual(card.x + card.width + 0.5);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    // The same QR card as the donation tiles.
    const look = (sel) => page.locator(sel).first().evaluate((e) => { const c = getComputedStyle(e); return [c.borderRadius, c.boxShadow, c.backgroundColor, c.padding, c.width]; });
    if (width > 760) expect(await look("#donateQr")).toEqual(await look(".donate-way-qr"));
    // In the row layout the three QR cards and their captions line up exactly.
    if (width > 760) {
      const qrs = await page.locator("#donateDialog img:is(.donate-way-qr, .donate-qr)").evaluateAll((els) => els.map((e) => e.getBoundingClientRect().toJSON()));
      const caps = await page.locator("#donateDialog .donate-way-scan-caption").evaluateAll((els) => els.map((e) => e.getBoundingClientRect().toJSON()));
      expect(qrs).toHaveLength(3);
      expect(caps).toHaveLength(3);
      for (const q of qrs) {
        expect(Math.abs(q.top - qrs[0].top)).toBeLessThan(0.6);
        expect(Math.abs(q.bottom - qrs[0].bottom)).toBeLessThan(0.6);
        expect(Math.abs(q.width - qrs[0].width)).toBeLessThan(0.6);
      }
      for (const c of caps) expect(Math.abs(c.top - caps[0].top)).toBeLessThan(0.6);
    }
    await page.locator("#donateClose").click();
  }
  expect(outside).toEqual([]);
});
}

const INSTAGRAM = "https://www.instagram.com/stemdeck/";
const X_URL = "https://x.com/StemDeckApp";

test("the Share card has Instagram and X follow links that open in the browser, and nothing loads from them before a click", async ({ page }) => {
  const { outside } = await openApp(page, { tauri: true });
  await page.locator("#donateBtn").click();
  for (const [id, href, name] of [["#donateInstagram", INSTAGRAM, "StemDeck on Instagram"], ["#donateX", X_URL, "StemDeck on X"]]) {
    const a = page.locator(id);
    await expect(a).toBeVisible();
    await expect(a).toHaveAttribute("href", href);
    await expect(a).toHaveAttribute("target", "_blank");
    await expect(a).toHaveAttribute("rel", "noopener noreferrer");
    await expect(a).toHaveAccessibleName(name);
    await expect(a.locator("svg")).toHaveAttribute("aria-hidden", "true");
  }
  expect(outside).toEqual([]);
  await page.locator("#donateInstagram").click();
  await page.locator("#donateX").click();
  expect(await opened(page)).toEqual([INSTAGRAM, X_URL]);
});

test("a follow link that is not set has no button, the other stays", async ({ page }) => {
  await openApp(page, { social: { instagram: "", x: "http://example.org/not-https" } });
  await page.locator("#donateBtn").click();
  await expect(page.locator("#donateInstagram")).toBeHidden();
  await expect(page.locator("#donateX")).toBeHidden();
  await expect(page.locator("#donateInstagram")).not.toHaveAttribute("href", /./);
});

test("the Guide titles the icon-only follow links from their names and says they open in the browser", async ({ page }) => {
  await openApp(page, { tauri: true });
  await page.locator("#helpModeBtn").click();
  await page.locator("#donateBtn").click();
  const tip = page.locator("#helpTip");
  await page.locator("#donateInstagram").hover();
  await expect(tip.locator("b")).toHaveText("StemDeck on Instagram");
  await expect(tip).toContainText("in your web browser");
  await page.locator("#donateX").hover();
  await expect(tip.locator("b")).toHaveText("StemDeck on X");
  await page.locator("#donateX").click();
  expect(await opened(page)).toEqual([]);
});

test("the hero has the heart pattern evenly across its whole width and no drawing", async ({ page }) => {
  for (const width of [1280, 360]) {
    await page.setViewportSize({ width, height: 900 });
    await openApp(page, { links: BOTH });
    await page.locator("#donateBtn").click();
    const hero = page.locator(".donate-hero");
    await expect(page.locator(".donate-art, .donate-art-svg")).toHaveCount(0);
    const found = await hero.evaluate((el) => {
      const before = getComputedStyle(el, "::before");
      const after = getComputedStyle(el, "::after");
      const box = el.getBoundingClientRect();
      return {
        before: { content: before.content, width: parseFloat(before.width), mask: before.maskImage || before.webkitMaskImage, repeat: before.maskRepeat || before.webkitMaskRepeat },
        afterContent: after.content, afterBg: after.backgroundImage, width: box.width, boxHeight: box.height,
      };
    });
    // The pattern spans the box, repeats, and nothing fades it (no gradient layer on top).
    expect(Math.abs(found.before.width - (found.width - 2))).toBeLessThan(2);
    expect(found.before.mask).toContain("data:image/svg+xml");
    expect(found.before.repeat).toBe("repeat");
    expect(["none", "normal"]).toContain(found.afterContent);
    expect(found.afterBg).not.toContain("gradient");
    // Text-driven height: no min-height holds it open.
    if (width === 1280) expect(found.boxHeight).toBeLessThan(270);
    await page.goto("about:blank");
  }
});

for (const failing of ["stripe", "liberapay"]) {
  test(`a ${failing} QR code that fails to load hides only its own code and caption`, async ({ page }) => {
    await page.route("**/api/qr?*", (route) => (route.request().url().includes(encodeURIComponent(WAY_LINKS[failing])) ? route.abort() : route.continue()));
    await openApp(page, { links: BOTH });
    await page.locator("#donateBtn").click();
    const other = failing === "stripe" ? "liberapay" : "stripe";
    await expect(tile(page, failing).locator(".donate-way-scan")).toBeHidden();
    await expect(tile(page, failing)).toHaveAttribute("href", WAY_LINKS[failing]);
    await expect(tile(page, other).locator(".donate-way-scan")).toBeVisible();
    await expect(tile(page, other).locator("img.donate-way-qr")).toHaveAttribute("src", `/api/qr?url=${encodeURIComponent(WAY_LINKS[other])}`);
  });
}

test("a way whose DONATE_QR is false shows no QR code, and the other tile keeps its own", async ({ page }) => {
  const { outside } = await openApp(page, { links: BOTH, qr: { stripe: true, liberapay: false } });
  await page.locator("#donateBtn").click();
  await expect(tile(page, "liberapay").locator("img")).toHaveCount(0);
  await expect(tile(page, "liberapay").locator(".donate-way-scan")).toHaveCount(0);
  await expect(tile(page, "stripe").locator("img.donate-way-qr")).toHaveAttribute("src", /\/api\/qr\?url=/);
  await expectLinkTile(page, "liberapay", LIBERAPAY, WAY_NAMES.liberapay);
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

test("share: the QR code comes from this server, there is no Copy button, star opens GitHub", async ({ page }) => {
  await openApp(page, { tauri: true });
  const qrRequests = [];
  page.on("request", (r) => {
    // The Stripe tile draws its own code too; this one is about the repo's.
    if (r.url().includes("/api/qr") && r.url().includes(encodeURIComponent(REPO))) qrRequests.push(r.url());
  });
  await page.locator("#donateBtn").click();
  const qr = page.locator("#donateQr");
  await expect(qr).toHaveAttribute("src", `/api/qr?url=${encodeURIComponent(REPO)}`);
  await expect.poll(() => qr.evaluate((img) => img.complete && img.naturalWidth)).toBeGreaterThan(0);
  expect(qrRequests).toHaveLength(1);
  await expect(qr).toHaveAttribute("alt", "QR code for the StemDeck page on GitHub");
  await expect(dialog(page).locator(".donate-addr")).toHaveText("github.com/stemdeckapp/stemdeck");

  // The address stays plain selectable text for anyone who wants to copy it by hand.
  await expect(page.locator("#donateCopy")).toHaveCount(0);
  expect(await dialog(page).locator(".donate-addr").evaluate((e) => getComputedStyle(e).userSelect)).not.toBe("none");

  // Share shows only where the system has a share menu.
  const canShare = await page.evaluate(() => typeof navigator.share === "function");
  await expect(page.locator("#donateShare")).toBeVisible({ visible: canShare });

  await page.locator("#donateStar").click();
  expect(await opened(page)).toEqual([REPO]);
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
    [page.locator("#donateStar"), "Star", "give it a star"],
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
// tile or runs under the cue that it opens in the browser. The language is
// switched with the dialog open, as Settings does, so the link tile's name
// has to follow the switch too.
for (const [label, templates] of [["link tiles", null], ["one amount tile and one link tile", STRIPE_AMOUNT]]) {
  test(`${label} fit in every language, at 950 px and at phone width`, async ({ page }) => {
    await openApp(page, { templates });
    await page.locator("#donateBtn").click();
    const problems = [];
    for (const lang of LANGUAGES) {
      const want = await page.evaluate(async (l) => {
        const i18n = await import("/js/i18n.js");
        await i18n.setLanguage(l);
        return i18n.t("donate.wayLink", { name: i18n.t("donate.recurring"), via: i18n.t("donate.viaLiberapay") });
      }, lang);
      await expect(tile(page, "liberapay")).toHaveAccessibleName(want);
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
