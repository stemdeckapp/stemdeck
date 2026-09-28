// Settings > Song details: the tab, and the Discogs token in it.
//
// Driven in a browser because the risk is the control, not the value: the
// token is the user's, so the field is masked, it is never filled back in
// (the server hands out only its last two characters), a refused token has
// to say where to make a working one, and Clear has to actually clear it.
// serve.sh turns the save-time check off, so nothing here reaches Discogs;
// where Discogs itself would refuse, the POST is answered here.

import { test, expect } from "@playwright/test";
import { seedLibrary } from "./helpers.mjs";

const TOKEN = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789XyQz";

async function openSettings(page) {
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  await page.locator('.settings-tab[data-tab="details"]').click();
  await expect(page.locator(".set-discogs-token")).toBeVisible();
}

const settings = async (page) => (await page.request.get("/api/settings")).json();

test.describe("Song details tab", () => {
  test("sits right after General and holds the three sections", async ({ page }) => {
    await seedLibrary(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.locator("#settingsBtn").click();
    const tabs = page.locator(".settings-tab");
    await expect(tabs.nth(0)).toHaveAttribute("data-tab", "general");
    await expect(tabs.nth(1)).toHaveAttribute("data-tab", "details");
    await expect(tabs.nth(1)).toHaveText("Song details");

    // Not on General any more.
    const general = page.locator('.settings-pane[data-pane="general"]');
    await expect(general.locator(".set-acoustid-key, .set-discogs-token, .set-transcribe-lyrics")).toHaveCount(0);

    await tabs.nth(1).click();
    const pane = page.locator('.settings-pane[data-pane="details"]');
    await expect(pane).toBeVisible();
    await expect(general).toBeHidden();
    await expect(pane.locator(".settings-row-title")).toHaveText([
      "Song identification",
      "Discogs",
      "Transcribe lyrics",
    ]);
  });
});

test.describe("Discogs token", () => {
  test.beforeEach(async ({ page }) => {
    await page.request.post("/api/settings", { data: { discogs_token: "" } });
  });

  // The backend is shared across the suite: leave no token behind.
  test.afterEach(async ({ page }) => {
    await page.request.post("/api/settings", { data: { discogs_token: "" } });
  });

  test("starts empty and masked, and says when names are sent", async ({ page }) => {
    await openSettings(page);
    const input = page.locator(".set-discogs-token");
    await expect(input).toHaveAttribute("type", "password");
    await expect(input).toHaveValue("");
    await expect(page.locator(".discogs-token-msg")).toHaveText("No token saved.");
    await expect(page.locator(".set-discogs-clear")).toBeDisabled();
    await expect(page.locator(".set-discogs-save")).toBeDisabled();
    await expect(page.locator(".discogs-row")).toContainText(
      "Band profiles, members and releases for bands Wikipedia does not cover, from Discogs. Optional.",
    );
    await expect(page.locator(".discogs-privacy")).toHaveText(
      "Band names are sent to Discogs only when a token is saved, and only for a band Wikipedia has no history, members or albums for, or a name you search for that Wikipedia does not have.",
    );
    const link = page.locator(".discogs-register");
    await expect(link).toHaveText("Get a free token at discogs.com");
    await expect(link).toHaveAttribute("href", "https://www.discogs.com/settings/developers");
    await expect(link).toHaveAttribute("target", "_blank");
  });

  test("saves a token, shows only its last two, and survives a reopen", async ({ page }) => {
    await openSettings(page);
    const input = page.locator(".set-discogs-token");
    await input.fill(TOKEN);
    await expect(page.locator(".set-discogs-save")).toBeEnabled();

    const [response] = await Promise.all([
      page.waitForResponse((r) => r.url().endsWith("/api/settings") && r.request().method() === "POST"),
      page.locator(".set-discogs-save").click(),
    ]);
    expect(await response.text()).not.toContain(TOKEN);
    await expect(page.locator(".discogs-token-msg")).toHaveText("A token ending in Qz is saved.");
    await expect(input).toHaveValue("");
    await expect(page.locator(".set-discogs-clear")).toBeEnabled();

    const stored = await settings(page);
    expect(stored.discogs_token_set).toBe(true);
    expect(JSON.stringify(stored)).not.toContain(TOKEN);
    // The AcoustID line is untouched by it.
    await expect(page.locator(".acoustid-key-msg")).toHaveText("No key saved.");

    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator("#settingsBtn").click();
    await page.locator('.settings-tab[data-tab="details"]').click();
    await expect(page.locator(".discogs-token-msg")).toHaveText("A token ending in Qz is saved.");
    await expect(page.locator(".set-discogs-token")).toHaveValue("");
  });

  test("a token that cannot be one is refused and nothing is saved", async ({ page }) => {
    await openSettings(page);
    await page.locator(".set-discogs-token").fill("not a token!");
    await page.locator(".set-discogs-save").click();
    await expect(page.locator(".discogs-token-msg")).toHaveText("That does not look like a Discogs token.");
    await expect(page.locator(".discogs-token-msg")).toHaveClass(/error/);
    // Left in the field to be corrected.
    await expect(page.locator(".set-discogs-token")).toHaveValue("not a token!");
    expect((await settings(page)).discogs_token_set).toBe(false);
  });

  test("a token Discogs refuses says where to make one", async ({ page }) => {
    await page.route("**/api/settings", (route) => (route.request().method() === "POST"
      ? route.fulfill({ status: 422, contentType: "application/json", body: JSON.stringify({ detail: "Discogs does not accept this token" }) })
      : route.fallback()));
    await openSettings(page);
    await page.locator(".set-discogs-token").fill(TOKEN);
    await page.locator(".set-discogs-save").click();
    await expect(page.locator(".discogs-token-msg")).toHaveText(
      "Discogs does not accept this token. Create a personal access token in your Discogs account settings, under Developers.",
    );
    await expect(page.locator(".discogs-token-msg")).toHaveClass(/error/);
    await expect(page.locator(".set-discogs-token")).toHaveValue(TOKEN);
  });

  test("a save that never reaches the server says so", async ({ page }) => {
    await page.route("**/api/settings", (route) => (route.request().method() === "POST"
      ? route.abort("failed")
      : route.fallback()));
    await openSettings(page);
    await page.locator(".set-discogs-token").fill(TOKEN);
    await page.locator(".set-discogs-save").click();
    await expect(page.locator(".discogs-token-msg")).toHaveText(
      "Could not save the token. Check the connection and try again.",
    );
    await expect(page.locator(".discogs-token-msg")).toHaveClass(/error/);
  });

  test("Clear removes the token", async ({ page }) => {
    await page.request.post("/api/settings", { data: { discogs_token: TOKEN } });
    await openSettings(page);
    await expect(page.locator(".discogs-token-msg")).toHaveText("A token ending in Qz is saved.");
    await page.locator(".set-discogs-clear").click();
    await expect(page.locator(".discogs-token-msg")).toHaveText("No token saved.");
    await expect(page.locator(".set-discogs-clear")).toBeDisabled();
    expect((await settings(page)).discogs_token_set).toBe(false);
  });
});
