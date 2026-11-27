// Opening Performance's Export menu leaves the rest of the Recording & Output
// panel alone (#1144): the panel keeps its width when the open menu makes it
// scroll, no control above the export moves or is covered, the Sound and
// picture sync section stays reachable, and in a window too short for the
// panel the menu is scrolled into sight.
//
// Playwright's headless Chromium hides scrollbars by default, which is
// exactly what hid this: a scrollbar that takes no width cannot narrow
// anything. These run with the platform's own scrollbars, as the app has.
//
// The take is a stand-in ("fake-take" on a Performance track, listed on the
// job by a route) with a camera and an estimated sync, so the sync section
// shows as it does under a real take.

import { test, expect } from "@playwright/test";
import { JOB_ID, openStudio, chooseMode } from "./helpers.mjs";

test.use({ launchOptions: { ignoreDefaultArgs: ["--hide-scrollbars"] } });

const sm = (page, sel) => page.locator(`#streamer ${sel}`);
const TAKE = "fake-take";
const TRACK = "k0fa4e001";

/** The studio with a last take on a Performance track, and Performance open. */
async function setUp(page, { width, height }) {
  await page.setViewportSize({ width, height });
  await page.addInitScript(([id, take, track]) => {
    localStorage.setItem(`stemdeck:streamer:take:${id}`, take);
    localStorage.setItem(`stemdeck:streamer:track:${id}`, track);
  }, [JOB_ID, TAKE, TRACK]);
  await page.route(`**/api/jobs/${JOB_ID}`, async (r) => {
    if (r.request().method() !== "GET") return r.continue();
    const res = await r.fetch();
    const job = await res.json();
    const take = {
      id: TAKE, track_id: TRACK, label: "Performance", has_camera: true, user_nudge_ms: 0,
      video_sync: { v: 1, source: "estimated", base: "estimated", offsetMs: 120, estimateMs: 120, nudgeMs: 0, reason: "no-camera-audio" },
    };
    const track = { id: TRACK, name: "Performance", order: 0, kind: "video", active_take_id: TAKE, input: null };
    return r.fulfill({
      response: res,
      json: { ...job, takes: [...(job.takes || []), take], tracks: [...(job.tracks || []), track] },
    });
  });
  await openStudio(page);
  await chooseMode(page, "streamer");
  await expect(page.locator("#streamer")).toBeVisible();
  // Under 1200 px the panel is a drawer, opened by Output.
  if (await sm(page, ".sm-output-btn").isVisible()) await sm(page, ".sm-output-btn").click();
  await expect(sm(page, ".sm-export-go")).toBeEnabled();
  await expect(sm(page, ".sm-sync")).toBeVisible();
}

const CONTROLS = {
  video: "#streamerVideoSrc",
  audio: "#streamerAudioSrc",
  resolution: "#streamerRes",
  fps: "#streamerFps",
  mixed: '[data-setting="recordMixedAudio"]',
  autoStop: '[data-setting="autoStopAtEnd"]',
};

/** The panel and every control above the export: width, place in the
 * panel's content, and whether something else is drawn over it. */
function measure(page) {
  return page.evaluate((controls) => {
    const side = document.querySelector("#streamer .sm-side");
    const view = side.getBoundingClientRect();
    const card = document.querySelector("#streamer .sm-out").getBoundingClientRect();
    const res = document.querySelector("#streamerRes");
    const out = {};
    for (const [name, sel] of Object.entries(controls)) {
      const el = document.querySelector(`#streamer ${sel}`);
      const r = el.getBoundingClientRect();
      const shown = r.bottom > view.top && r.top < view.bottom;
      const y = Math.min(Math.max(r.top + Math.min(r.height / 2, 8), view.top + 1), view.bottom - 1);
      const hit = shown ? document.elementFromPoint(r.left + Math.min(r.width / 2, 20), y) : null;
      out[name] = {
        top: Math.round(r.top - view.top + side.scrollTop),
        width: Math.round(r.width),
        height: Math.round(r.height),
        covered: shown && !(hit && (hit === el || el.contains(hit))),
      };
    }
    return {
      panelWidth: side.clientWidth,
      cardWidth: Math.round(card.width),
      resolutionCut: res.scrollWidth > res.clientWidth,
      controls: out,
    };
  }, CONTROLS);
}

/** Whether `sel` is inside the panel's visible part and drawn on top. */
function inSight(page, sel) {
  return page.evaluate((s) => {
    const side = document.querySelector("#streamer .sm-side");
    const el = document.querySelector(`#streamer ${s}`);
    const view = side.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    if (r.top < view.top - 1 || r.bottom > view.bottom + 1) return false;
    const hit = document.elementFromPoint(r.left + Math.min(r.width / 2, 20), r.top + Math.min(r.height / 2, 8));
    return Boolean(hit && (hit === el || el.contains(hit)));
  }, sel);
}

async function openMenu(page) {
  await sm(page, ".sm-export-go").click();
  await expect(sm(page, ".sm-export-menu")).toBeVisible();
  // DaVinci Resolve shows the most: its two options and the longer line.
  await sm(page, '.sm-export-fmt[data-format="resolve"]').click();
  await expect(sm(page, ".sm-export-opts")).toBeVisible();
}

test.describe("Performance Export menu and the panel around it", () => {
  for (const [name, size] of [
    ["the side column", { width: 1800, height: 900 }],
    ["the drawer", { width: 1100, height: 900 }],
  ]) {
    test(`${name}: opening it narrows, moves and covers nothing above it`, async ({ page }) => {
      await setUp(page, size);
      const before = await measure(page);
      for (const [n, c] of Object.entries(before.controls)) expect(c.covered, `${n} before`).toBe(false);
      expect(before.resolutionCut).toBe(false);

      await openMenu(page);
      const after = await measure(page);
      // The open menu makes the panel scroll here; its scrollbar has its own room.
      expect(await page.evaluate(() => {
        const s = document.querySelector("#streamer .sm-side");
        return s.scrollHeight > s.clientHeight;
      })).toBe(true);
      expect(after.panelWidth).toBe(before.panelWidth);
      expect(after.cardWidth).toBe(before.cardWidth);
      expect(after.resolutionCut).toBe(false);
      for (const n of Object.keys(CONTROLS)) {
        expect(after.controls[n], n).toEqual(before.controls[n]);
      }
      // Only the status line and the Export row are sticky; the menu and the
      // sync section are the card's own rows under them.
      await expect(sm(page, ".sm-export .sm-export-menu")).toHaveCount(0);
      await expect(sm(page, ".sm-export .sm-sync")).toHaveCount(0);
      // The sync section is still there, under the menu, and reachable.
      await sm(page, ".sm-sync").scrollIntoViewIfNeeded();
      expect(await inSight(page, ".sm-sync-title")).toBe(true);
    });
  }

  test("a window too short for the panel: the menu is scrolled into sight", async ({ page }) => {
    await setUp(page, { width: 1800, height: 620 });
    const before = await measure(page);
    await openMenu(page);
    expect(await inSight(page, ".sm-export-fmts")).toBe(true);
    const after = await measure(page);
    expect(after.panelWidth).toBe(before.panelWidth);
    expect(after.cardWidth).toBe(before.cardWidth);
    expect(after.resolutionCut).toBe(false);
    // Scrolled, not laid out again: every control keeps its place in the panel.
    for (const n of Object.keys(CONTROLS)) expect(after.controls[n].top, n).toBe(before.controls[n].top);
  });
});
