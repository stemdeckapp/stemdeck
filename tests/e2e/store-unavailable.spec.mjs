// A desktop store that cannot be opened stops the page (#1023).
//
// user-data.json can be there but unreadable for a while: a virus scanner or
// a sync client holding it. The page used to take a stand-in (an empty
// library) for every read that failed and could later save it over the real
// file. Now the page opens the store once with store_ready before any read;
// when that fails it reads nothing, says so with Try again, and Try again
// loads the page afresh, which opens the store again.

import { test, expect } from "@playwright/test";
import { stubTauri } from "./helpers.mjs";

test("a store that cannot be opened stops the page, and Try again loads it", async ({ page }) => {
  await stubTauri(page);
  // The first store_ready fails as the shell answers for a locked file; the
  // one after Try again works. sessionStorage survives the reload.
  await page.addInitScript(() => {
    const invoke = window.__TAURI__.core.invoke;
    window.__TAURI__.core.invoke = (cmd, args) => {
      if (cmd === "store_ready" && sessionStorage.getItem("e2e-store-ready") !== "ok") {
        sessionStorage.setItem("e2e-store-ready", "ok");
        window.__e2e.calls.push({ cmd: "store_ready", failed: true });
        return Promise.reject("user-data-unavailable: user-data.json could not be read (locked)");
      }
      return invoke(cmd, args);
    };
  });

  await page.goto("/");

  const box = page.locator("#error");
  await expect(box).toBeVisible();
  await expect(box).toContainText("cannot open your library list");
  const retry = box.locator(".store-retry-btn");
  await expect(retry).toBeVisible();

  // Nothing was read, so nothing can be a stand-in, and nothing was written.
  const counts = () =>
    page.evaluate(() => ({
      ready: window.__e2e.callsFor("store_ready").length,
      get: window.__e2e.callsFor("store_get").length,
      set: window.__e2e.callsFor("store_set").length + window.__e2e.callsFor("store_set_many").length,
    }));
  expect(await counts()).toEqual({ ready: 1, get: 0, set: 0 });

  await retry.click();

  // Loaded afresh: the store opens and the page reads it.
  await expect.poll(async () => (await counts()).get).toBeGreaterThan(0);
  await expect(box).toBeHidden();
  expect((await counts()).ready).toBe(1);
});
