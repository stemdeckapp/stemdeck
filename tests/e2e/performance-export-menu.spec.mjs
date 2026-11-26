// Performance's Export Performance menu (static/js/streamer/streamer.js
// buildExportMenu, static/js/exportMenu.js): one button that opens a menu with
// two formats, MP4 and DaVinci Resolve, the Resolve options, and the action
// row. The take is a stand-in ("fake-take", listed on the job by a route) and
// the export endpoints are stubbed, so these pin the page: what it asks the
// server for, what it says, and that the Studio's own export format is never
// read from these tabs. The package itself is measured in
// tests/test_takes_resolve.py.

import { test, expect } from "@playwright/test";
import { JOB_ID, openStudio, chooseMode } from "./helpers.mjs";

const sm = (page, sel) => page.locator(`#streamer ${sel}`);
const TAKE = "fake-take";

/** The studio with a last take that the job lists, and Performance open. */
async function setUp(page, { width = 1800, height = 1020 } = {}) {
  await page.setViewportSize({ width, height });
  await openStudio(page);
  await page.evaluate(([id, take]) => localStorage.setItem(`stemdeck:streamer:take:${id}`, take), [JOB_ID, TAKE]);
  await page.route(`**/api/jobs/${JOB_ID}`, async (r) => {
    if (r.request().method() !== "GET") return r.continue();
    const res = await r.fetch();
    const job = await res.json();
    return r.fulfill({ response: res, json: { ...job, takes: [...(job.takes || []), { id: TAKE }] } });
  });
  await chooseMode(page, "streamer");
  await expect(page.locator("#streamer")).toBeVisible();
  await expect(sm(page, ".sm-export-go")).toBeEnabled();
}

/** Stub one kind of export ("render" or "resolve"): the POST answers 202
 * and records its body, the status is done at once, the file is a few bytes. */
async function stubExport(page, kind) {
  const bodies = [];
  const file = kind === "resolve" ? "resolve.otioz" : "render.mp4";
  await page.route(`**/takes/${TAKE}/${kind}`, (r) => {
    if (r.request().method() === "POST") {
      bodies.push(r.request().postDataJSON());
      return r.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify({ status: "queued", progress: 0 }) });
    }
    return r.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ status: "done", progress: 1, url: `/api/jobs/${JOB_ID}/takes/${TAKE}/${file}`, large: false }),
    });
  });
  await page.route(`**/takes/${TAKE}/${file}*`, (r) => r.fulfill({ contentType: "application/zip", body: "PK" }));
  return bodies;
}

test.describe("Export Performance menu", () => {
  test("opens on its button with two formats, and the format is remembered", async ({ page }) => {
    await setUp(page);
    const go = sm(page, ".sm-export-go");
    await expect(go).toHaveText("Export Performance");
    await expect(go).toHaveAttribute("aria-haspopup", "menu");
    await expect(sm(page, ".sm-export-menu")).toBeHidden();
    await go.click();
    await expect(go).toHaveAttribute("aria-expanded", "true");
    await expect(sm(page, ".sm-export-fmt")).toHaveText(["MP4", "DaVinci Resolve"]);
    await sm(page, '.sm-export-fmt[data-format="mp4"]').click();
    await expect(sm(page, ".sm-export-run-title")).toHaveText("Export MP4");
    await expect(sm(page, ".sm-export-run-desc")).toHaveText("The take with the song under it, as one video file.");
    // The options are DaVinci Resolve's only.
    await expect(sm(page, ".sm-export-opts")).toBeHidden();
    await sm(page, '.sm-export-fmt[data-format="resolve"]').click();
    await expect(sm(page, '.sm-export-fmt[data-format="resolve"]')).toHaveAttribute("aria-checked", "true");
    await expect(sm(page, ".sm-export-run-title")).toHaveText("Export for DaVinci Resolve");
    await expect(sm(page, ".sm-export-run-desc")).toContainText("DaVinci Resolve 18.5 or newer");
    await expect(sm(page, '.sm-export-opt[data-opt="stems"]')).toHaveAttribute("aria-checked", "false");
    await expect(sm(page, '.sm-export-opt[data-opt="click"]')).toBeVisible();
    // The Studio's own format is never read from these tabs.
    expect(await page.evaluate(() => document.querySelector("#t-export-panel .export-fmt.active")?.id)).toBe("t-fmt-wav");
    // Remembered after a reload.
    expect(await page.evaluate(() => localStorage.getItem("stemdeck.streamer.exportFormat"))).toBe("resolve");
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    // Performance may come back open with the song; open it only if not.
    if (!(await page.locator("#streamer").isVisible())) await chooseMode(page, "streamer");
    await expect(page.locator("#streamer")).toBeVisible();
    await expect(sm(page, ".sm-export-go")).toBeEnabled();
    await sm(page, ".sm-export-go").click();
    await expect(sm(page, '.sm-export-fmt[data-format="resolve"]')).toHaveClass(/is-on/);
  });

  test("the keyboard: Down opens it and goes in, Up and Down move, Escape closes", async ({ page }) => {
    await setUp(page);
    const go = sm(page, ".sm-export-go");
    await go.focus();
    await page.keyboard.press("ArrowDown");
    await expect(sm(page, ".sm-export-menu")).toBeVisible();
    await sm(page, '.sm-export-fmt[data-format="mp4"]').click();
    await go.focus();
    await page.keyboard.press("ArrowDown");
    await expect(sm(page, ".sm-export-run")).toBeFocused();
    // Left and Right move between the formats.
    await sm(page, '.sm-export-fmt[data-format="mp4"]').focus();
    await page.keyboard.press("ArrowRight");
    await expect(sm(page, '.sm-export-fmt[data-format="resolve"]')).toBeFocused();
    await expect(sm(page, '.sm-export-fmt[data-format="resolve"]')).toHaveAttribute("aria-checked", "true");
    await page.keyboard.press("Escape");
    await expect(sm(page, ".sm-export-menu")).toBeHidden();
    await expect(go).toBeFocused();
    await expect(go).toHaveAttribute("aria-expanded", "false");
  });

  test("DaVinci Resolve: the package request, and the .otioz saved", async ({ page }) => {
    await setUp(page);
    const bodies = await stubExport(page, "resolve");
    await sm(page, ".sm-export-go").click();
    await sm(page, '.sm-export-fmt[data-format="resolve"]').click();
    await sm(page, '.sm-export-opt[data-opt="stems"]').click();
    const download = page.waitForEvent("download");
    await sm(page, ".sm-export-run").click();
    const file = await download;
    expect(file.suggestedFilename()).toMatch(/\(Performance\)\.otioz$/);
    await expect(sm(page, ".sm-export-text")).toHaveText("Saved.");
    expect(bodies).toHaveLength(1);
    const body = bodies[0];
    expect(body.backing).toBe("stems");
    expect(body.master).toBeUndefined();
    expect(body.name).toMatch(/\(Performance\)$/);
    expect(body.click).toBe(false);
    // The menu shut while it ran, and the button is back.
    await expect(sm(page, ".sm-export-menu")).toBeHidden();
    await expect(sm(page, ".sm-export-go")).toBeVisible();
  });

  test("MP4 still exports one video", async ({ page }) => {
    await setUp(page);
    const bodies = await stubExport(page, "render");
    await sm(page, ".sm-export-go").click();
    await sm(page, '.sm-export-fmt[data-format="mp4"]').click();
    const download = page.waitForEvent("download");
    await sm(page, ".sm-export-run").click();
    expect((await download).suggestedFilename()).toMatch(/\(Performance\)\.mp4$/);
    expect(bodies).toHaveLength(1);
    expect(bodies[0].backing).toBeUndefined();
  });

  test("a take without a picture is kept and says so (no Try again)", async ({ page }) => {
    await setUp(page);
    await page.route(`**/takes/${TAKE}/render`, (r) =>
      r.fulfill({ status: 422, contentType: "application/json", body: JSON.stringify({ detail: "take has no camera" }) }));
    await sm(page, ".sm-export-go").click();
    await sm(page, '.sm-export-fmt[data-format="mp4"]').click();
    await sm(page, ".sm-export-run").click();
    await expect(sm(page, ".sm-export-text")).toContainText("This take has no picture");
    // Not read as a deleted take: it is still the last take.
    expect(await page.evaluate((id) => localStorage.getItem(`stemdeck:streamer:take:${id}`), JOB_ID)).toBe(TAKE);
    await expect(sm(page, ".sm-export-go")).toBeEnabled();
    await sm(page, ".sm-export-go").click();
    await expect(sm(page, ".sm-export-run-title")).toHaveText("Export MP4");
  });

  test("in the Guide the button still opens its menu, and every part explains itself", async ({ page }) => {
    await setUp(page);
    await page.locator("#helpModeBtn").click();
    await expect(page.locator("body")).toHaveClass(/help-mode/);
    await sm(page, ".sm-export-go").click();
    await expect(sm(page, ".sm-export-menu")).toBeVisible();
    for (const [sel, text] of [
      [".sm-export-go", "export choices"],
      [".sm-export-fmts", "DaVinci Resolve 18.5"],
      [".sm-export-run", "start and end handles"],
    ]) {
      await sm(page, sel).hover();
      await expect(page.locator("#helpTip"), sel).toContainText(text);
    }
  });

  test("narrow: the menu fits its panel, with targets 38 px or taller", async ({ page }) => {
    await setUp(page, { width: 1100, height: 900 });
    await sm(page, ".sm-output-btn").click();
    await sm(page, ".sm-export-go").click();
    await sm(page, '.sm-export-fmt[data-format="resolve"]').click();
    const sizes = await page.evaluate(() => {
      const menu = document.querySelector("#sm-export-menu");
      const side = document.querySelector("#streamer .sm-side");
      const boxes = [...menu.querySelectorAll(".sm-export-fmt, .sm-export-opt, .sm-export-run")].map((e) => e.getBoundingClientRect());
      const m = menu.getBoundingClientRect();
      const s = side.getBoundingClientRect();
      return {
        overflow: menu.scrollWidth - menu.clientWidth,
        inside: m.left >= s.left - 0.5 && m.right <= s.right + 0.5,
        least: Math.min(...boxes.map((b) => b.height)),
      };
    });
    expect(sizes.overflow).toBeLessThanOrEqual(0);
    expect(sizes.inside).toBe(true);
    expect(sizes.least).toBeGreaterThanOrEqual(38);
  });
});
