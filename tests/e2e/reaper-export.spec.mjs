// REAPER: "Export as REAPER Project" (the stems zip plus a .rpp), "Open in
// REAPER" (desktop only, and only when a REAPER is found or chosen), and the
// External editors setting that says which REAPER that is.
//
// The REAPER lookup and launch live in the desktop shell (reaper.rs), which a
// browser does not have, so its commands are answered here. Their Rust half
// (detection, the chosen path, unpacking, the launch arguments) is covered by
// the shell's own tests, and the .rpp itself by tests/test_reaper_project.py.

import { test, expect } from "@playwright/test";
import { openStudio, exportUi, seedLibrary, stubTauri, JOB_ID } from "./helpers.mjs";

const FOUND = { path: "C:\\Program Files\\REAPER (x64)\\reaper.exe", source: "detected", chosen_missing: null };
const NONE = { path: null, source: "none", chosen_missing: null };
const CHOSEN = { path: "D:\\Audio\\REAPER\\reaper.exe", source: "chosen", chosen_missing: null };

/** Answer the REAPER commands on top of stubTauri. `status` may be changed
 * from the test through window.__reaper.status. */
async function stubReaper(page, status, { picked = CHOSEN } = {}) {
  await page.addInitScript(
    ([initial, pick, none]) => {
      window.__reaper = { status: initial };
      const base = window.__TAURI__.core.invoke;
      window.__TAURI__.core.invoke = (cmd, args) => {
        if (cmd === "reaper_status") return Promise.resolve(window.__reaper.status);
        if (cmd === "pick_reaper_app") {
          if (pick === "fail") return Promise.reject("that is not a program StemDeck can open");
          window.__reaper.status = pick;
          return base(cmd, args).then(() => pick);
        }
        if (cmd === "reset_reaper_app") {
          window.__reaper.status = none;
          return base(cmd, args).then(() => none);
        }
        return base(cmd, args);
      };
    },
    [status, picked, NONE],
  );
}

async function openSettings(page) {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  await expect(page.locator(".library-editor")).toBeVisible();
  await page.locator('.settings-tab[data-tab="library"]').click();
}

test.describe("export menu, browser", () => {
  test("Export as REAPER Project asks for the zip with the project in it", async ({ page, request }) => {
    await openStudio(page);
    const ui = exportUi(page);
    await ui.open();
    await expect(page.locator("#t-export-reaper")).toBeVisible();
    // Nothing to launch REAPER with in a browser.
    await expect(page.locator("#t-export-open-reaper")).toBeHidden();
    const download = page.waitForEvent("download");
    await page.locator("#t-export-reaper").click();
    const dl = await download;
    const url = new URL(dl.url());
    expect(url.pathname).toBe(`/api/jobs/${JOB_ID}/stems/all.zip`);
    expect(url.searchParams.get("reaper")).toBe("1");
    expect(dl.suggestedFilename()).toMatch(/_reaper\.zip$/);
    // The real server puts a project in it.
    const res = await request.get(url.pathname + url.search);
    expect(res.status()).toBe(200);
    expect((await res.body()).includes(Buffer.from(".rpp"))).toBe(true);
  });

  test("the REAPER rows go with the other audio-only rows under MP4", async ({ page }) => {
    await openStudio(page);
    const ui = exportUi(page);
    await ui.open();
    await page.evaluate(() => document.querySelector("#t-export-panel").classList.add("fmt-mp4"));
    await expect(page.locator("#t-export-reaper")).toBeHidden();
  });
});

test.describe("export menu, desktop (Tauri) mode", () => {
  test("Open in REAPER shows when REAPER is found, and opens the saved project", async ({ page }) => {
    await stubTauri(page);
    await stubReaper(page, FOUND);
    await openStudio(page); // the stubs above stand in for the shell
    const ui = exportUi(page);
    await ui.open();
    const row = page.locator("#t-export-open-reaper");
    await expect(row).toBeVisible();
    await row.click();
    await page.waitForFunction(() => window.__e2e.pickPending());
    const pick = await page.evaluate(() => window.__e2e.callsFor("pick_export_destination")[0].args);
    expect(pick.filename).toMatch(/_reaper\.zip$/);
    await page.evaluate(() => window.__e2e.choosePath());
    await page.waitForFunction(() => window.__e2e.savePending());
    const save = await page.evaluate(() => window.__e2e.callsFor("download_to_path")[0].args);
    expect(new URL(save.url).searchParams.get("reaper")).toBe("1");
    await page.evaluate(() => window.__e2e.finishSave());
    await page.waitForFunction(() => window.__e2e.callsFor("open_reaper_project").length === 1);
    const open = await page.evaluate(() => window.__e2e.callsFor("open_reaper_project")[0].args);
    expect(open).toEqual({ token: "token-1" });
  });

  test("Open in REAPER is hidden when no REAPER is found, and appears once one is", async ({ page }) => {
    await stubTauri(page);
    await stubReaper(page, NONE);
    await openStudio(page); // the stubs above stand in for the shell
    const ui = exportUi(page);
    await ui.open();
    await expect(page.locator("#t-export-reaper")).toBeVisible();
    await expect(page.locator("#t-export-open-reaper")).toBeHidden();
    // Chosen in Settings meanwhile: the next time the menu opens, it is there.
    await ui.button.click(); // closes the menu
    await expect(ui.panel).toBeHidden();
    await page.evaluate((s) => (window.__reaper.status = s), CHOSEN);
    await ui.open();
    await expect(page.locator("#t-export-open-reaper")).toBeVisible();
  });
});

test.describe("Settings > External editors", () => {
  test("is not offered in a browser", async ({ page }) => {
    await seedLibrary(page);
    await openSettings(page);
    await expect(page.locator(".settings-group-external-editors")).toHaveCount(0);
  });

  test("shows the REAPER it found, takes a chosen one, and goes back to finding it", async ({ page }) => {
    await stubTauri(page);
    await stubReaper(page, FOUND);
    await seedLibrary(page);
    await openSettings(page);
    const pathEl = page.locator(".reaper-app-path");
    const reset = page.locator(".reset-reaper-app");
    await expect(pathEl).toHaveText(`${FOUND.path} (found automatically)`);
    await expect(reset).toBeHidden();

    await page.locator(".set-reaper-app").click();
    await expect(pathEl).toHaveText(CHOSEN.path);
    await expect(pathEl).toHaveAttribute("title", CHOSEN.path);
    await expect(reset).toBeVisible();

    await reset.click();
    await expect(pathEl).toHaveText("Not found. Install REAPER, or choose where it is.");
    await expect(reset).toBeHidden();
  });

  test("a chosen REAPER that has gone is said plainly", async ({ page }) => {
    await stubTauri(page);
    await stubReaper(page, { ...FOUND, chosen_missing: "E:\\Old\\reaper.exe" });
    await seedLibrary(page);
    await openSettings(page);
    const msg = page.locator(".reaper-app-msg");
    await expect(msg).toHaveText("The REAPER you chose is no longer at E:\\Old\\reaper.exe.");
    await expect(msg).toHaveClass(/\berror\b/);
    await expect(page.locator(".reset-reaper-app")).toBeVisible();
  });

  test("a refused pick says why and keeps the REAPER", async ({ page }) => {
    await stubTauri(page);
    await stubReaper(page, FOUND, { picked: "fail" });
    await seedLibrary(page);
    await openSettings(page);
    await page.locator(".set-reaper-app").click();
    await expect(page.locator(".reaper-app-msg")).toHaveText("that is not a program StemDeck can open");
    await expect(page.locator(".reaper-app-path")).toHaveText(`${FOUND.path} (found automatically)`);
  });

  test("a shell too old to look for REAPER hides the group", async ({ page }) => {
    await stubTauri(page);
    await seedLibrary(page);
    await openSettings(page);
    await expect(page.locator(".settings-group-external-editors")).toHaveCount(0);
  });
});
