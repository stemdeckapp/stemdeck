// Sound and picture sync in Performance's export step
// (static/js/streamer/syncControl.js, the rules in static/js/video/avSync.js):
// after a take with a camera, the export step says how the picture was placed
// against the sound (measured, estimated with its reason, or adjusted by
// hand) and where it starts, and the nudge is saved on the take through
// POST .../sync, which moves the take's picture at once (camera_start_sec).
//
// A real take on Chromium's fake camera and microphone, as streamer-media
// does. Whether the server could measure that take depends on the fake
// devices, so the first test accepts either wording; the states the fake
// devices cannot make are answered by a stubbed POST .../sync.

import { test, expect } from "@playwright/test";
import { JOB_ID, openStudio, chooseMode } from "./helpers.mjs";
import { removeTracks } from "./recording-helpers.mjs";

test.use({ launchOptions: { args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--mute-audio", "--autoplay-policy=no-user-gesture-required"] } });

test.afterEach(async ({ request }) => removeTracks(request));

const ev = (page, fn, arg) => page.evaluate(fn, arg);
const sm = (page, sel) => page.locator(`#streamer ${sel}`);
const SYNC_ROUTE = "**/api/jobs/*/takes/*/sync";

/** The last take's entry as the page holds it (the Performance track's active take). */
const takeEntry = (page) => ev(page, async () => {
  const { getTracks } = await import("/js/capture/takeLanes.js");
  const tr = getTracks().find((x) => x.active?.has_camera);
  return tr ? JSON.parse(JSON.stringify(tr.active)) : null;
});

async function recordTake(page) {
  await page.setViewportSize({ width: 1800, height: 1020 });
  await openStudio(page);
  await ev(page, async ([id]) => {
    const m = await import("/js/streamer/streamerModel.js");
    const { storeSet } = await import("/js/utils.js");
    await storeSet(`stemdeck:streamer:scene:${id}`, m.serializeScene({ ...m.defaultScene("16:9"), trim: { in: 1, out: 4 } }));
  }, [JOB_ID]);
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
  await page.waitForFunction(() => !/\/\s*00:00\s*$/.test(document.querySelector("#t-time")?.textContent || "00:00 / 00:00"));
  await chooseMode(page, "streamer");
  await expect(page.locator("#streamer")).toBeVisible();
  const allow = sm(page, ".sm-cam-act");
  if (await allow.isVisible()) await allow.click({ timeout: 3000 }).catch(() => {});
  await sm(page, ".sm-rec").click();
  await expect.poll(() => ev(page, () => document.querySelector("#streamer .sm-rec")?.classList.contains("recording")), { timeout: 20000 }).toBe(false);
  await expect(sm(page, ".sm-export-go")).toBeEnabled({ timeout: 30000 });
  await expect(sm(page, ".sm-sync")).toBeVisible({ timeout: 30000 });
}

/** The entry the server would answer, with `sync` over its video_sync. */
function withSync(entry, sync) {
  const s = { ...entry.video_sync, ...sync };
  return { ...entry, video_sync: { ...s, pictureOffsetMs: s.offsetMs + s.nudgeMs - (entry.camera_offset_ms || 0) } };
}

test.describe("Sound and picture sync in the export step", () => {
  test("after a take it says how the picture was placed, and where it starts", async ({ page }) => {
    await recordTake(page);
    await expect(sm(page, ".sm-sync-title")).toHaveText("Sound and picture sync");
    const state = (await sm(page, ".sm-sync-state").textContent()).trim();
    expect(state).toMatch(/^(Measured automatically from the sound|Estimated, not measured)/);
    await expect(sm(page, ".sm-sync-offset")).toHaveText(/^The picture (starts \d+ ms (after|before) the sound|and the sound start together)\.$/);
    // An estimate warns that the sound may be out of step; a measurement does not.
    if (state.startsWith("Estimated")) await expect(sm(page, ".sm-sync-warn")).toContainText("move the slider");
    else await expect(sm(page, ".sm-sync-warn")).toBeHidden();
    // The label says what moves, and the hint which way plus goes.
    await expect(sm(page, ".sm-sync-label")).toHaveText("Move the picture earlier or later");
    await expect(sm(page, ".sm-sync-hint")).toContainText("Plus shows the picture later");
    await expect(sm(page, ".sm-sync-value")).toHaveText("0 ms");
    await expect(sm(page, ".sm-sync-reset")).toBeHidden();
    // What the page shows is what the server holds.
    const entry = await takeEntry(page);
    expect(entry?.video_sync?.source).toMatch(/^(measured|estimated)$/);
  });

  test("the nudge is saved on the take and moves its picture, and Reset takes it back", async ({ page, request }) => {
    await recordTake(page);
    const before = await takeEntry(page);
    const sent = [];
    page.on("request", (r) => { if (r.method() === "POST" && /\/sync$/.test(r.url())) sent.push(r.postDataJSON()); });
    for (let i = 0; i < 4; i++) await sm(page, ".sm-sync-later").click();
    // Four presses in a row are one save.
    await expect(sm(page, ".sm-sync-status")).toHaveText("Sync saved. The next export uses it.");
    expect(sent).toEqual([{ nudgeMs: 20 }]);
    await expect(sm(page, ".sm-sync-value")).toHaveText("+20 ms");
    await expect(sm(page, ".sm-sync-state")).toHaveText(/^Adjusted by hand/);
    await expect(sm(page, ".sm-sync-warn")).toBeHidden();
    // The picture is 20 ms later in the song, in the page and on the server.
    const after = await takeEntry(page);
    expect(after.camera_start_sec - before.camera_start_sec).toBeCloseTo(0.02, 3);
    const onServer = await (await request.get(`/api/jobs/${JOB_ID}/takes/${before.id}/sync`)).json();
    expect(onServer.video_sync.nudgeMs).toBe(20);
    expect(onServer.video_sync.source).toBe("manual");

    await sm(page, ".sm-sync-reset").click();
    await expect.poll(() => sent.length).toBe(2);
    expect(sent[1]).toEqual({ nudgeMs: 0 });
    await expect(sm(page, ".sm-sync-value")).toHaveText("0 ms");
    await expect(sm(page, ".sm-sync-reset")).toBeHidden();
    expect((await takeEntry(page)).camera_start_sec).toBeCloseTo(before.camera_start_sec, 3);
  });

  test("the slider by keyboard saves 5 ms a press, and minus moves the picture earlier", async ({ page }) => {
    await recordTake(page);
    const sent = [];
    page.on("request", (r) => { if (r.method() === "POST" && /\/sync$/.test(r.url())) sent.push(r.postDataJSON()); });
    await sm(page, ".sm-sync-range").focus();
    await page.keyboard.press("ArrowLeft");
    await expect.poll(() => sent.length).toBe(1);
    expect(sent[0]).toEqual({ nudgeMs: -5 });
    await expect(sm(page, ".sm-sync-value")).toHaveText("-5 ms");
    await expect(sm(page, ".sm-sync-range")).toHaveAttribute("aria-valuetext", "-5 ms");
  });

  test("a refusal is said in words, and the value goes back", async ({ page }) => {
    await recordTake(page);
    await page.route(SYNC_ROUTE, (route) => route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: "video render running" }) }));
    await sm(page, ".sm-sync-later").click();
    await expect(sm(page, ".sm-sync-status")).toContainText("a video of this take is being made");
    await expect(sm(page, ".sm-sync-value")).toHaveText("0 ms");
    expect((await takeEntry(page)).video_sync.nudgeMs).toBe(0);
  });

  test("an estimate says why and warns; Measure again shows only when the measurement did not finish", async ({ page }) => {
    await recordTake(page);
    const entry = await takeEntry(page);
    const answers = [
      withSync(entry, { source: "estimated", base: "estimated", reason: "measure-failed", nudgeMs: 0, confidence: null, offsetMs: 456 }),
      withSync(entry, { source: "measured", base: "measured", reason: "ok", nudgeMs: 0, confidence: 0.93, offsetMs: -70 }),
    ];
    const bodies = [];
    await page.route(SYNC_ROUTE, (route) => {
      bodies.push(route.request().postDataJSON());
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(answers.shift()) });
    });
    await sm(page, ".sm-sync-later").click();
    await expect(sm(page, ".sm-sync-state")).toHaveText("Estimated, not measured: the measurement did not finish.");
    await expect(sm(page, ".sm-sync-offset")).toHaveText("The picture starts 456 ms after the sound.");
    await expect(sm(page, ".sm-sync-warn")).toHaveText("The sound may be out of step with the picture. If it is, move the slider until they match.");
    await expect(sm(page, ".sm-sync-measure")).toBeVisible();
    await sm(page, ".sm-sync-measure").click();
    await expect(sm(page, ".sm-sync-state")).toHaveText("Measured automatically from the sound (93% match).");
    await expect(sm(page, ".sm-sync-offset")).toHaveText("The picture starts 70 ms before the sound.");
    await expect(sm(page, ".sm-sync-warn")).toBeHidden();
    await expect(sm(page, ".sm-sync-measure")).toBeHidden();
    expect(bodies[1]).toEqual({ remeasure: true });
  });

  test("the Guide explains each part of the sync control", async ({ page }) => {
    await recordTake(page);
    await page.locator("#helpModeBtn").click();
    await expect(page.locator("body")).toHaveClass(/help-mode/);
    const tip = page.locator("#helpTip");
    await sm(page, ".sm-sync-range").hover();
    await expect(tip).toContainText("Moves the picture earlier or later");
    await sm(page, ".sm-sync-later").hover();
    await expect(tip).toContainText("Plus shows the picture later");
    await sm(page, ".sm-sync-state").hover();
    await expect(tip).toContainText("lined up");
  });
});
