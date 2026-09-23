// The logo opens the site, and on the desktop it opens it outside the app.
//
// This is the case a browser test would pass without checking: in a browser,
// target="_blank" is the whole implementation and there is nothing to get
// wrong. Inside the Tauri webview there is no second tab to open into, so a
// link left to its own devices navigates the app window away from StemDeck and
// leaves the user in a web page with no way back. main.js intercepts
// a[target="_blank"] and hands the URL to the shell instead, and the logo has
// to be reached by that path rather than by a click handler of its own.
import { test, expect } from "@playwright/test";
import { openStudio } from "./helpers.mjs";

test.describe("the logo links to the site", () => {
  test("it is an external anchor, not a decoration", async ({ page }) => {
    await openStudio(page, { tauri: true });

    const link = page.locator(".daw-brand a");
    await expect(link).toHaveAttribute("href", "https://stemdeck.app");
    await expect(link).toHaveAttribute("target", "_blank");
    // Without noopener the opened page gets a handle on this one.
    await expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  test("on the desktop the click goes to the shell, not the app window", async ({ page }) => {
    await openStudio(page, { tauri: true });

    const result = await page.evaluate(async () => {
      const seen = [];
      const original = window.__TAURI__.core.invoke;
      window.__TAURI__.core.invoke = (cmd, args) => {
        seen.push({ cmd, url: args?.url });
        return original(cmd, args);
      };
      document.querySelector(".daw-brand a").click();
      await new Promise((r) => setTimeout(r, 200));
      window.__TAURI__.core.invoke = original;
      return { seen, path: location.pathname };
    });

    expect(result.seen).toContainEqual({ cmd: "open_url", url: "https://stemdeck.app/" });
    // And the app is still the thing on screen.
    expect(result.path).toBe("/");
  });

  test("its tooltip is translated like every other label", async ({ page }) => {
    // An icon with no text is only labelled by its title and aria-label, so a
    // hardcoded English string here would be the one part of the bar that does
    // not follow the language setting.
    await page.addInitScript(() =>
      localStorage.setItem("stemdeck.language", JSON.stringify("de")),
    );
    await openStudio(page, { tauri: true });

    const link = page.locator(".daw-brand a");
    await expect(link).toHaveAttribute("title", "stemdeck.app öffnen");
    await expect(link).toHaveAttribute("aria-label", "stemdeck.app öffnen");
  });

  test("the link does not change the shape of the logo cell", async ({ page }) => {
    // The anchor is a flex item inside a cell that centres it. Left inline it
    // would carry a line box and push the logo off the rail's centre line.
    await openStudio(page, { tauri: true });

    const box = await page.evaluate(() => {
      const cell = document.querySelector(".daw-brand").getBoundingClientRect();
      const img = document.querySelector(".daw-brand img").getBoundingClientRect();
      return {
        above: Math.round(img.top - cell.top),
        below: Math.round(cell.bottom - img.bottom),
        height: Math.round(img.height),
      };
    });
    expect(box.height).toBe(52);
    expect(Math.abs(box.above - box.below)).toBeLessThanOrEqual(1);
  });
});
