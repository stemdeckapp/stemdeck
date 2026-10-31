// A Lead + Backing split that did not work (#832). It used to leave one
// Vocals stem and say nothing, so the person who asked for Lead + Backing
// could not tell it from a song without backing vocals.
//
// The fixture has no model to split with, so every split here is stubbed at
// POST /api/jobs/{id}/vocal-split with the answer the server gives when the
// split fails (app/api/jobs.py start_vocal_split). The backend side, the real
// worker failing in each way it can, is tests/test_vocal_split_outcome.py.
import { test, expect } from "@playwright/test";
import { openStudio, stubVocalSplitHealth, JOB_ID, TRACK_TITLE, chooseMode } from "./helpers.mjs";

const SPLIT_URL = `**/api/jobs/${JOB_ID}/vocal-split`;

/** Answer the split with a failure for `reason`, and count the requests. */
async function failSplit(page, reasons) {
  const posts = [];
  await page.route(SPLIT_URL, (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    const reason = reasons[Math.min(posts.length, reasons.length - 1)];
    posts.push(reason);
    return route.fulfill({
      status: 500,
      json: { detail: "vocal split failed", vocal_split_reason: reason, vocal_split_message: "x" },
    });
  });
  return posts;
}

const tr = (page, key, vars) =>
  page.evaluate(async ([k, v]) => (await import("/js/i18n.js")).t(k, v), [key, vars]);

const lanes = (page) =>
  page.evaluate(async () => Object.keys((await import("/js/state.js")).trackIndex).filter((n) => !/^t[kr]_/.test(n)).sort().join(","));

/** What an import with Lead + Backing chosen does when its job finishes:
 * job.js runVocalSplitIfWanted, the call both completion paths make. */
async function finishImportWithSplit(page) {
  return page.evaluate(async (id) => {
    const job = await import("/js/job.js");
    const state = await (await fetch(`/api/jobs/${id}`)).json();
    job.jobVocalSplitModes.set(id, "split");
    const after = await job.runVocalSplitIfWanted(state);
    return { status: after.status, vocalSplit: after.vocal_split, reason: after.vocal_split_reason, stems: after.stems.map((s) => s.name) };
  }, JOB_ID);
}

test.describe("a Lead + Backing split that did not work", () => {
  test("says so, why, and keeps the Vocals stem", async ({ page }) => {
    const posts = await failSplit(page, ["dependency_missing"]);
    await openStudio(page);

    const after = await finishImportWithSplit(page);

    expect(posts).toEqual(["dependency_missing"]);
    // The song is done and keeps one Vocals stem.
    expect(after).toMatchObject({ status: "done", vocalSplit: "error", reason: "dependency_missing" });
    expect(after.stems).toContain("vocals");
    const box = page.locator("#error");
    await expect(box).toBeVisible();
    await expect(box).toContainText(await tr(page, "split.failed", { title: TRACK_TITLE }));
    await expect(box).toContainText(await tr(page, "split.reason.dependency_missing"));
    await expect(box.locator(".split-retry-btn")).toHaveText(await tr(page, "job.tryAgain"));
    // The notification centre keeps it after the box is closed.
    const failures = await page.evaluate(async () => (await import("/js/notifications.js")).getFailures());
    expect(failures.filter((f) => f.kind === "split" && f.context?.jobId === JOB_ID)).toHaveLength(1);
    await expect(page.locator(".daw-notif-card-title").first()).toHaveText(await tr(page, "notifKind.splitFailed"));
    expect(await lanes(page)).toContain("vocals");
  });

  test("Try again runs the split again on the same song", async ({ page }) => {
    const posts = await failSplit(page, ["model_unavailable", "out_of_memory"]);
    const creates = [];
    page.on("request", (req) => {
      if (req.method() === "POST" && /\/api\/jobs(\/[a-f0-9]{12}\/resplit)?$/.test(req.url())) creates.push(req.url());
    });
    await openStudio(page);
    await finishImportWithSplit(page);
    await expect(page.locator("#error")).toContainText(await tr(page, "split.reason.model_unavailable"));

    await page.locator("#error .split-retry-btn").click();

    // The same split endpoint, the same song: nothing is imported again.
    await expect.poll(() => posts.length).toBe(2);
    expect(creates).toEqual([]);
    await expect(page.locator("#error")).toContainText(await tr(page, "split.reason.out_of_memory"));
    // One card per song, refreshed rather than stacked.
    const failures = await page.evaluate(async () => (await import("/js/notifications.js")).getFailures());
    expect(failures.filter((f) => f.kind === "split")).toHaveLength(1);
  });

  test("a cancel is not reported as a failure", async ({ page }) => {
    await failSplit(page, ["cancelled"]);
    await openStudio(page);
    const after = await finishImportWithSplit(page);
    expect(after.reason).toBe("cancelled");
    await expect(page.locator("#error")).toBeHidden();
  });

  test("the Stems popover says why the last split did not work", async ({ page }) => {
    // The song's own state carries the failure, as after a restart. Opening
    // a song reads it fresh (catalog.js loadTrackIntoStudio).
    await page.route(`**/api/jobs/${JOB_ID}`, async (route) => {
      if (route.request().method() !== "GET") return route.fallback();
      const response = await route.fetch();
      const body = await response.json();
      await route.fulfill({ response, json: { ...body, vocal_split: "error", vocal_split_reason: "timeout" } });
    });
    await openStudio(page);
    await page.locator("#laneStemsBtn").click();
    await expect(page.locator("#laneStemsPop .stems-pop-split-note")).toHaveText(
      await tr(page, "stems.splitFailedWhy", { reason: await tr(page, "split.reason.timeout") }),
    );

    // Splitting from there again says why it failed this time.
    await failSplit(page, ["worker_failed"]);
    await page.locator("#laneStemsPop .stems-pop-split").click();
    await expect(page.locator("#laneStemsPop .stems-pop-status")).toHaveText(
      await tr(page, "stems.splitFailedWhy", { reason: await tr(page, "split.reason.worker_failed") }),
    );
  });
});

test.describe("Lead + Backing on an install that cannot make it", () => {
  const split = (page) => page.locator('#vocalModeToggle .vocal-mode-btn[data-mode="split"]');

  test("is shown off, with the reason, before anyone picks it", async ({ page }) => {
    await stubVocalSplitHealth(page, { available: false, reason: "dependency_missing" });
    const posts = await failSplit(page, ["dependency_missing"]);
    await openStudio(page);

    await expect(split(page)).toHaveAttribute("aria-disabled", "true");
    await expect(split(page)).toHaveAttribute("title", await tr(page, "split.unavailable"));
    // aria-disabled, not disabled: a person can still press it and hear why.
    await split(page).click({ force: true });
    // Nothing chosen: Combined stays the mode, and the reason is said.
    await expect(split(page)).not.toHaveAttribute("aria-pressed", "true");
    await expect(page.locator("#error")).toContainText(await tr(page, "split.unavailable"));
    const mode = await page.evaluate(async () => (await import("/js/vocalSplitState.js")).effectiveVocalSplitMode());
    expect(mode).toBe("all");

    // The Stems popover's split and Karaoke's Separate say the same.
    await page.locator("#laneStemsBtn").click();
    await expect(page.locator("#laneStemsPop .stems-pop-split")).toBeDisabled();
    await expect(page.locator("#laneStemsPop .stems-pop-split-note")).toHaveText(await tr(page, "split.unavailable"));
    await page.keyboard.press("Escape");
    await chooseMode(page, "karaoke");
    const separate = page.locator('#karaoke .kk-voice-btn[data-form="split"]');
    await expect(separate).toBeDisabled();
    await expect(separate).toHaveAttribute("title", await tr(page, "split.unavailable"));
    expect(posts).toEqual([]);
  });

  test("is offered as usual when it can be made", async ({ page }) => {
    await stubVocalSplitHealth(page, { available: true, reason: null });
    await openStudio(page);

    await expect(split(page)).toHaveAttribute("aria-disabled", "false");
    await expect(split(page)).not.toHaveAttribute("title", /./);
    await split(page).click();
    await expect(split(page)).toHaveAttribute("aria-pressed", "true");
  });

  test("a split that finds a dependency missing switches it off at once", async ({ page }) => {
    await stubVocalSplitHealth(page, { available: true, reason: null });
    await failSplit(page, ["dependency_missing"]);
    await openStudio(page);
    await expect(split(page)).toHaveAttribute("aria-disabled", "false");

    await finishImportWithSplit(page);

    await expect(split(page)).toHaveAttribute("aria-disabled", "true");
  });
});
