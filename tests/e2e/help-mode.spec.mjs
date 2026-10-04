// Help mode: Guide in the rail, below Trash, makes the studio explain itself. Every
// control it covers shows an outline, pointing at one shows a box with its
// name and what it does, and a click while help mode is on explains instead of
// acting.

import { test, expect } from "@playwright/test";

import { openStudio, seedLibrary, stubUpdateCheck } from "./helpers.mjs";

const tip = (page) => page.locator("#helpTip");

async function helpOn(page) {
  await page.locator("#helpModeBtn").click();
  await expect(page.locator("body")).toHaveClass(/help-mode/);
  await expect(page.locator("#helpModeBtn")).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".help-pill")).toBeVisible();
}

test.describe("help mode", () => {
  test("Guide turns it on, and Esc or Guide turns it off", async ({ page }) => {
    await openStudio(page);
    await helpOn(page);
    await page.keyboard.press("Escape");
    await expect(page.locator("body")).not.toHaveClass(/help-mode/);
    await helpOn(page);
    await page.locator("#helpModeBtn").click();
    await expect(page.locator("body")).not.toHaveClass(/help-mode/);
    await expect(page.locator(".help-target")).toHaveCount(0);
    await expect(page.locator(".help-pill")).toBeHidden();
  });

  test("turning Guide on makes every explainable control glow once, then fade", async ({ page }) => {
    await openStudio(page);
    await page.locator("#helpModeBtn").click();
    const rings = page.locator(".help-intro-ring");
    expect(await rings.count()).toBeGreaterThan(20);
    await expect(page.locator(".help-intro")).toBeVisible();
    // Gone by itself, so the screen is calm again.
    await expect(rings).toHaveCount(0, { timeout: 4000 });
    await expect(page.locator(".help-intro")).toBeHidden();

    // Leaving Guide mid-glow clears it at once.
    await page.keyboard.press("Escape");
    await page.locator("#helpModeBtn").click();
    await page.keyboard.press("Escape");
    await expect(rings).toHaveCount(0);
  });

  test("pointing at a control explains it, under its own label", async ({ page }) => {
    await openStudio(page);
    await helpOn(page);
    await page.locator("#t-speed-075").hover();
    await expect(tip(page)).toBeVisible();
    await expect(tip(page).locator("b")).toHaveText("0.75x");
    await expect(tip(page)).toContainText("three-quarter speed");
    await expect(page.locator("#t-speed-075")).toHaveClass(/help-current/);
  });

  test("a lane's control names its instrument", async ({ page }) => {
    await openStudio(page);
    await helpOn(page);
    const drumsMute = page.locator(".mx-row, .lane-header").filter({ hasText: "Drums" }).locator(".mx-btn.mute").first();
    await drumsMute.hover();
    await expect(tip(page)).toContainText("Silences Drums");
    // Headed by what the button does, not by its one letter.
    await expect(tip(page).locator("b")).toHaveText("Mute Drums");

    const drumsKey = page.locator(".lane-key").filter({ has: page.locator('[aria-label*="Drums"]') }).locator(".lane-key-value").first();
    await drumsKey.hover({ force: true });
    await expect(tip(page).locator("b")).toHaveText("Key of Drums");
  });

  test("each song fact and presence card explains itself", async ({ page }) => {
    await openStudio(page);
    await helpOn(page);
    await page.locator('.daw-meta-card[data-meta="lufs"]').hover();
    await expect(tip(page)).toContainText("streaming services");
    await page.locator('.daw-meta-card[data-meta="stability"]').hover();
    await expect(tip(page)).toContainText("How steady the tempo is");
    await page.locator('.stem-presence-panel .stem-card[data-stem="drums"]').hover();
    await expect(tip(page)).toContainText("How loud the Drums part is");
  });

  test("with no song open, controls the studio switches off still explain themselves", async ({ page }) => {
    await seedLibrary(page);
    await stubUpdateCheck(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.locator(".cat-item").first().waitFor();
    await helpOn(page);
    for (const sel of ["#t-export-btn", "#np-details-btn", '.daw-meta-card[data-meta="key"]', '.stem-presence-panel .stem-card[data-stem="vocals"]']) {
      const box = await page.locator(sel).first().boundingBox();
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await expect(tip(page), sel).toBeVisible();
      await expect(page.locator(sel).first(), sel).toHaveClass(/help-current/);
    }
  });

  test("a click explains instead of acting", async ({ page }) => {
    await openStudio(page);
    await helpOn(page);
    const fav = page.locator("#fav-btn");
    await expect(fav).toHaveAttribute("aria-pressed", "false");
    await fav.click();
    await expect(tip(page)).toContainText("Favorites");
    await expect(fav).toHaveAttribute("aria-pressed", "false");

    // Off again, the same click works.
    await page.keyboard.press("Escape");
    await fav.click();
    await expect(fav).toHaveAttribute("aria-pressed", "true");
  });

  test("Settings lists every tab and its options", async ({ page }) => {
    await openStudio(page);
    await helpOn(page);
    await page.locator("#settingsBtn").hover();
    for (const text of ["General", "Song details", "Network", "Export", "Logs", "Registry", "Port", "Compute device", "Make StemDeck available on your network"]) {
      await expect(tip(page)).toContainText(text);
    }
  });

  test("every outlined control has a title and a real explanation", async ({ page }) => {
    await openStudio(page);
    await helpOn(page);
    const targets = page.locator(".help-target");
    const count = await targets.count();
    expect(count).toBeGreaterThan(40);
    const bad = [];
    for (let i = 0; i < count; i++) {
      const el = targets.nth(i);
      if (!(await el.isVisible())) continue;
      // Hover the centre of the box, which for the small lane steppers is the
      // only point not shared with a neighbour.
      await el.hover({ force: true });
      const title = (await tip(page).locator("b").textContent())?.trim();
      const text = (await tip(page).textContent())?.trim();
      if (!title || !text || /help\.|\{name\}/.test(text) || text === title) bad.push({ i, title, text });
    }
    expect(bad).toEqual([]);
  });
});
