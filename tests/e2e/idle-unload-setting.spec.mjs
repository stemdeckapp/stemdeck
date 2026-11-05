// "Free memory when idle" in Settings > General (#975): how many minutes
// without a new track before the separation models are unloaded, 0 for never.
//
// Driven in a real browser because the risk is the field, not the value: 0 is a
// real choice here (never unload), so a field that treats it as empty would
// show the wrong number or post a default the user never picked. The unload
// itself is tested in tests/test_idle_unload.py; no browser can observe it.

import { test, expect } from "@playwright/test";
import { openStudio, seedLibrary } from "./helpers.mjs";

const field = (page) => page.locator(".set-idle-unload");

async function openSettings(page) {
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  await expect(field(page)).toBeVisible();
}

const msg = (page) => page.locator(".idle-unload-msg");

/** A paste as the browser delivers one, with the text on the event. */
async function paste(page, text) {
  await field(page).focus();
  await field(page).evaluate((el, t) => {
    const dt = new DataTransfer();
    dt.setData("text/plain", t);
    el.dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
  }, text);
}

const saved = async (page) =>
  (await (await page.request.get("/api/settings")).json()).idle_unload_minutes;

test.describe("free memory when idle", () => {
  // The backend is shared across the suite: every test leaves the default.
  test.beforeEach(async ({ page }) => {
    await page.request.post("/api/settings", { data: { idle_unload_minutes: 10 } });
  });
  test.afterEach(async ({ page }) => {
    await page.request.post("/api/settings", { data: { idle_unload_minutes: 10 } });
  });

  test("shows ten minutes, and a new value reaches the server and survives a reopen", async ({ page }) => {
    await openSettings(page);
    await expect(field(page)).toHaveValue("10");
    await expect(page.locator(".settings-row:has(.set-idle-unload) .settings-unit")).toHaveText("min");

    await field(page).fill("25");
    await field(page).blur();
    await expect.poll(() => saved(page)).toBe(25);

    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator("#settingsBtn").click();
    await expect(field(page)).toHaveValue("25");
  });

  test("0 is kept as never, not taken for an empty field", async ({ page }) => {
    await openSettings(page);
    await field(page).fill("0");
    await field(page).blur();
    await expect.poll(() => saved(page)).toBe(0);

    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator("#settingsBtn").click();
    await expect(field(page)).toHaveValue("0");
  });

  test("the server owns the ceiling, the field shows what it kept, and says so", async ({ page }) => {
    await openSettings(page);
    await expect(page.locator(".set-idle-unload-desc")).toContainText("at most 1440");
    await field(page).fill("9999");
    await field(page).blur();
    await expect(field(page)).toHaveValue("1440");
    await expect(msg(page)).toHaveText("The most is 1440 minutes, so 1440 was saved.");
    expect(await saved(page)).toBe(1440);
    // Typing again clears the message.
    await field(page).fill("12");
    await expect(msg(page)).toBeHidden();
  });

  test("Enter commits the value", async ({ page }) => {
    await openSettings(page);
    await field(page).fill("40");
    await field(page).press("Enter");
    await expect.poll(() => saved(page)).toBe(40);
    await expect(field(page)).toHaveValue("40");
    await expect(field(page)).toBeFocused();
  });

  test("a failed save puts back the value in use and says so", async ({ page }) => {
    await openSettings(page);
    await page.route("**/api/settings", (route) =>
      route.request().method() === "POST" ? route.fulfill({ status: 500, body: "{}" }) : route.continue(),
    );
    await field(page).fill("40");
    await field(page).blur();
    await expect(field(page)).toHaveValue("10");
    await expect(msg(page)).toHaveText("Could not save this setting. The field shows the value still in use.");
    await expect(msg(page)).toHaveClass(/error/);
    await page.unroute("**/api/settings");
    expect(await saved(page)).toBe(10);
  });

  // With nothing saved, the server answers its default (10, or the value of
  // STEMDECK_IDLE_UNLOAD_MINUTES; tests/test_idle_unload.py covers that side).
  // The shared e2e backend cannot be put back to "nothing saved", so the
  // answer is stubbed: what is tested is that the field shows the server's
  // default rather than one of its own.
  test("shows the server's default, not one of its own", async ({ page }) => {
    await page.route("**/api/settings", async (route) => {
      if (route.request().method() !== "GET") return route.continue();
      const res = await route.fetch();
      const body = await res.json();
      await route.fulfill({ response: res, json: { ...body, idle_unload_minutes: 7 } });
    });
    await openSettings(page);
    await expect(field(page)).toHaveValue("7");
  });

  test("a pasted 1,440 is 1440, not the 144 maxlength used to leave", async ({ page }) => {
    await openSettings(page);
    await paste(page, "1,440");
    await expect(field(page)).toHaveValue("1440");
    await expect.poll(() => saved(page)).toBe(1440);
  });

  test("a paste over the ceiling is clamped and says so", async ({ page }) => {
    await openSettings(page);
    await paste(page, "20000");
    await expect(field(page)).toHaveValue("1440");
    await expect(msg(page)).toHaveText("The most is 1440 minutes, so 1440 was saved.");
    expect(await saved(page)).toBe(1440);
  });

  test("a pasted -5 is refused, not saved as 5", async ({ page }) => {
    await openSettings(page);
    await paste(page, "-5");
    await expect(msg(page)).toHaveText("Paste a whole number of minutes, 0 or more.");
    await expect(field(page)).toHaveValue("10");
    expect(await saved(page)).toBe(10);
  });

  test("refuses non-digits as they are typed", async ({ page }) => {
    await openSettings(page);
    await field(page).fill("");
    await field(page).pressSequentially("1a5");
    await expect(field(page)).toHaveValue("15");
  });

  test("an emptied field goes back to the saved value and sends nothing", async ({ page }) => {
    await page.request.post("/api/settings", { data: { idle_unload_minutes: 30 } });
    await openSettings(page);
    await expect(field(page)).toHaveValue("30");
    const posts = [];
    page.on("request", (r) => {
      if (r.method() === "POST" && r.url().endsWith("/api/settings")) posts.push(r.postData());
    });
    await field(page).fill("");
    await field(page).blur();
    await expect(field(page)).toHaveValue("30");
    expect(posts).toEqual([]);
    expect(await saved(page)).toBe(30);
  });

  test("the Guide explains it", async ({ page }) => {
    await openStudio(page);
    await page.locator("#helpModeBtn").click();
    await expect(page.locator("body")).toHaveClass(/help-mode/);
    await page.locator("#settingsBtn").hover();
    const tip = page.locator("#helpTip");
    await expect(tip).toContainText("Free memory when idle");
    await expect(tip).toContainText("Frees the memory the separation models hold");
  });

  test("is translated", async ({ page }) => {
    await seedLibrary(page);
    await page.addInitScript(() => localStorage.setItem("stemdeck.language", JSON.stringify("de")));
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.locator("#settingsBtn").click();
    await expect(page.locator(".settings-row:has(.set-idle-unload) .settings-row-title")).toHaveText(
      "Speicher im Leerlauf freigeben",
    );
  });

  test("the Guide explains it in another language", async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem("stemdeck.language", JSON.stringify("de")));
    await openStudio(page);
    await page.locator("#helpModeBtn").click();
    await expect(page.locator("body")).toHaveClass(/help-mode/);
    await page.locator("#settingsBtn").hover();
    const tip = page.locator("#helpTip");
    await expect(tip).toContainText("Speicher im Leerlauf freigeben");
    await expect(tip).toContainText("Gibt den Speicher der Trennmodelle");
  });

  test("the description shows the ceiling in another language", async ({ page }) => {
    await seedLibrary(page);
    await page.addInitScript(() => localStorage.setItem("stemdeck.language", JSON.stringify("de")));
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.locator("#settingsBtn").click();
    await expect(page.locator(".set-idle-unload-desc")).toContainText("höchstens 1440");
  });
});
