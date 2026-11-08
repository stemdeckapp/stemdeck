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

  test("a middle-click and a Ctrl-click go to the shell too, and nothing navigates (#1041)", async ({ page }) => {
    await openStudio(page, { tauri: true });

    const result = await page.evaluate(async () => {
      const seen = [];
      const original = window.__TAURI__.core.invoke;
      window.__TAURI__.core.invoke = (cmd, args) => {
        seen.push({ cmd, url: args?.url });
        return original(cmd, args);
      };
      const a = document.querySelector(".daw-brand a");
      const events = [
        new MouseEvent("auxclick", { bubbles: true, cancelable: true, button: 1 }),
        new MouseEvent("auxclick", { bubbles: true, cancelable: true, button: 2 }),
        new MouseEvent("click", { bubbles: true, cancelable: true, button: 0, ctrlKey: true }),
        new MouseEvent("click", { bubbles: true, cancelable: true, button: 0, metaKey: true, shiftKey: true }),
      ];
      const prevented = [];
      for (const ev of events) {
        a.dispatchEvent(ev);
        prevented.push(ev.defaultPrevented);
      }
      await new Promise((r) => setTimeout(r, 200));
      window.__TAURI__.core.invoke = original;
      return { seen, prevented, path: location.pathname };
    });

    // Middle button, Ctrl and Cmd+Shift are taken; the right button (context menu) is not.
    expect(result.prevented).toEqual([true, false, true, true]);
    expect(result.seen.filter((c) => c.cmd === "open_url")).toEqual([
      { cmd: "open_url", url: "https://stemdeck.app/" },
      { cmd: "open_url", url: "https://stemdeck.app/" },
      { cmd: "open_url", url: "https://stemdeck.app/" },
    ]);
    expect(result.path).toBe("/");
  });

  test("with the Guide on, a middle-click on a link opens nothing and saves nothing (#1041)", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await page.locator("#helpModeBtn").click();

    const result = await page.evaluate(async () => {
      const press = (el) => {
        const ev = new MouseEvent("auxclick", { bubbles: true, cancelable: true, button: 1 });
        el.dispatchEvent(ev);
        return ev.defaultPrevented;
      };
      const prevented = [press(document.querySelector(".daw-brand a"))];
      const dl = document.querySelector('a.lane-dl:not([href="#"])');
      if (dl) prevented.push(press(dl));
      await new Promise((r) => setTimeout(r, 200));
      return {
        prevented,
        opened: window.__e2e.callsFor("open_url").length,
        saved: window.__e2e.callsFor("save_audio_file").length,
        path: location.pathname,
      };
    });

    expect(result.prevented.every(Boolean)).toBe(true);
    expect(result.opened).toBe(0);
    expect(result.saved).toBe(0);
    expect(result.path).toBe("/");
  });

  test("a middle-click on a stem download link saves it through the shell, no navigation (#1041)", async ({ page }) => {
    await openStudio(page, { tauri: true });

    const result = await page.evaluate(async () => {
      const dl = document.querySelector('a.lane-dl:not([href="#"])');
      const ev = new MouseEvent("auxclick", { bubbles: true, cancelable: true, button: 1 });
      dl.dispatchEvent(ev);
      await new Promise((r) => setTimeout(r, 200));
      return {
        prevented: ev.defaultPrevented,
        href: dl.href,
        name: dl.download,
        saved: window.__e2e.callsFor("save_audio_file").map((c) => c.args),
        opened: window.__e2e.callsFor("open_url").length,
        path: location.pathname,
      };
    });

    expect(result.prevented).toBe(true);
    expect(result.saved).toEqual([{ url: result.href, filename: result.name }]);
    expect(result.opened).toBe(0);
    expect(result.path).toBe("/");
  });

  test("in a plain browser the press is left to the browser", async ({ page }) => {
    await openStudio(page);

    const prevented = await page.evaluate(() => {
      const a = document.querySelector(".daw-brand a");
      const ev = new MouseEvent("auxclick", { bubbles: true, cancelable: true, button: 1 });
      a.dispatchEvent(ev);
      return ev.defaultPrevented;
    });
    expect(prevented).toBe(false);
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
