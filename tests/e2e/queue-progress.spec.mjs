// An import's progress and stage on its library row, and the Import Queue view:
// order, "extract next", removing a waiting import (#772), a paused queue and its
// Start button, and the view closing itself when the queue empties.
//
// The queue is answered here (the e2e backend never separates, so a job that
// stays running cannot be produced for real), and the queue stream is held
// off so its empty frames cannot overwrite the answer. The trash can on a queue
// row is covered by trash-queue.spec.mjs.

import { test, expect } from "@playwright/test";
import { JOB_ID, seedLibrary, stubExportEndpoints, stubFavorites, stubUpdateCheck } from "./helpers.mjs";

const A = "a0a0a0a0a001";
const B = "a0a0a0a0a002";
const C = "a0a0a0a0a003";

/** A queue the test owns. `set()` replaces it and pushes it to the page. */
async function stubQueue(page) {
  const sent = { reorder: [], stop: [], start: 0 };
  let snap = { running: null, queued: [], paused: false };
  const body = () => ({
    ...snap,
    max_pending_uploads: 5,
    max_pending_urls: 50,
    capacity_left_uploads: 5,
    capacity_left_urls: 50,
  });
  await page.route("**/api/queue", (route) => route.fulfill({ json: body() }));
  await page.route("**/api/queue/events", (route) => route.abort());
  await page.route("**/api/queue/reorder", (route) => {
    const { job_id: id, after } = JSON.parse(route.request().postData() || "{}");
    sent.reorder.push({ id, after });
    const rest = snap.queued.filter((j) => j.job_id !== id);
    const moved = snap.queued.find((j) => j.job_id === id);
    const at = after === null ? 0 : rest.findIndex((j) => j.job_id === after) + 1;
    rest.splice(at, 0, moved);
    snap = { ...snap, queued: rest };
    return route.fulfill({ json: body() });
  });
  await page.route("**/api/queue/start", (route) => {
    sent.start += 1;
    snap = { ...snap, paused: false };
    return route.fulfill({ json: body() });
  });
  await page.route(/\/api\/jobs\/[0-9a-f]{12}\/stop$/, (route) => {
    const id = new URL(route.request().url()).pathname.split("/")[3];
    sent.stop.push(id);
    snap = { ...snap, queued: snap.queued.filter((j) => j.job_id !== id) };
    return route.fulfill({ json: { ok: true } });
  });
  const push = () => page.evaluate(async () => (await import("/js/queue.js")).refreshQueue());
  return {
    sent,
    set: async (next) => { snap = { running: null, queued: [], paused: false, ...next }; await push(); },
  };
}

const job = (job_id, title, extra = {}) => ({ job_id, title, status: "queued", progress: 0, ...extra });

async function open(page) {
  await seedLibrary(page);
  await stubFavorites(page);
  await stubExportEndpoints(page);
  await stubUpdateCheck(page);
  const queue = await stubQueue(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().waitFor({ timeout: 20000 });
  return queue;
}

const row = (page, id) => page.locator(`.cat-item[data-id="${id}"]`).first();

// ── progress and stage on the library row ───────────────────────────────────

test("a running import shows its stage and progress on its library row", async ({ page }) => {
  const queue = await open(page);
  const el = row(page, JOB_ID);

  await queue.set({ running: { job_id: JOB_ID, status: "separating", stage: "Separating 40%", progress: 0.4 } });
  await expect(el).toHaveClass(/queue-running/);
  await expect(el.locator(".cat-queue-label")).toHaveText("Separating 40%");
  await expect(el.locator(".cat-progress-fill")).toHaveAttribute("style", /width: 40%/);

  // A stage without its own percentage gets one, and "..." is dropped.
  await queue.set({ running: { job_id: JOB_ID, status: "analyzing", stage: "Analyzing...", progress: 0.72 } });
  await expect(el.locator(".cat-queue-label")).toHaveText("Analyzing 72%");
  await expect(el.locator(".cat-progress-fill")).toHaveAttribute("style", /width: 72%/);

  // Nothing done yet: no "0%".
  await queue.set({ running: { job_id: JOB_ID, status: "downloading", stage: "Downloading...", progress: 0 } });
  await expect(el.locator(".cat-queue-label")).toHaveText("Downloading");

  // Gone from the queue: the row is itself again.
  await queue.set({});
  await expect(el).not.toHaveClass(/in-queue/);
  await expect(el.locator(".cat-progress")).toHaveCount(0);
  await expect(el.locator(".cat-queue-label")).toHaveCount(0);
  await expect(el.locator(".cat-sub")).toHaveText("00:06");
});

test("a waiting import's row says where it is in line, without a bar", async ({ page }) => {
  const queue = await open(page);
  await queue.set({ running: job(A, "First", { status: "separating", progress: 0.1 }), queued: [job(JOB_ID, "Mine")] });
  const el = row(page, JOB_ID);
  await expect(el).toHaveClass(/queue-waiting/);
  await expect(el.locator(".cat-queue-label")).toHaveText("Queued - #2 in line");
  await expect(el.locator(".cat-progress")).toHaveCount(0);
});

// ── the Import Queue view ───────────────────────────────────────────────────

async function openQueueView(page) {
  await page.locator(".rail-queue").click();
  await expect(page.locator(".queue-section")).toBeVisible();
}

const shownOrder = (page) =>
  page.locator(".queue-section .queue-row").evaluateAll((els) => els.map((e) => e.dataset.id));

test("the rail button appears with a count only while something is queued", async ({ page }) => {
  const queue = await open(page);
  await expect(page.locator(".rail-queue")).toBeHidden();
  await queue.set({ running: job(A, "First", { status: "separating" }), queued: [job(B, "Second"), job(C, "Third")] });
  await expect(page.locator(".rail-queue")).toBeVisible();
  await expect(page.locator("#queueBadge")).toHaveText("3");
});

test("the view lists the running import first, then the line in order", async ({ page }) => {
  const queue = await open(page);
  await queue.set({
    running: job(A, "First", { status: "separating", stage: "Separating 10%", progress: 0.1 }),
    queued: [job(B, "Second"), job(C, "Third")],
  });
  await openQueueView(page);

  expect(await shownOrder(page)).toEqual([A, B, C]);
  const running = page.locator(`.queue-row[data-id="${A}"]`);
  await expect(running.locator(".cat-queue-label")).toHaveText("Separating 10%");
  await expect(running.locator(".queue-grip")).toHaveCount(0);
  await expect(running).not.toHaveAttribute("draggable", "true");
  await expect(page.locator(`.queue-row[data-id="${B}"] .cat-queue-label`)).toHaveText("Queued - #2 in line");
  await expect(page.locator(`.queue-row[data-id="${C}"] .cat-queue-label`)).toHaveText("Queued - #3 in line");
  // Second in line is already next: only the third offers "extract next".
  await expect(page.locator(`.queue-row[data-id="${B}"] .queue-top`)).toHaveCount(0);
  await expect(page.locator(`.queue-row[data-id="${C}"] .queue-top`)).toHaveCount(1);
});

test("Extract this one next moves it to the front of the line", async ({ page }) => {
  const queue = await open(page);
  await queue.set({ running: job(A, "First", { status: "separating" }), queued: [job(B, "Second"), job(C, "Third")] });
  await openQueueView(page);

  await page.locator(`.queue-row[data-id="${C}"] .queue-top`).click();

  await expect.poll(() => shownOrder(page)).toEqual([A, C, B]);
  expect(queue.sent.reorder).toEqual([{ id: C, after: null }]);
  await expect(page.locator(`.queue-row[data-id="${C}"] .cat-queue-label`)).toHaveText("Queued - #2 in line");
});

test("removing a waiting import takes its row out at once", async ({ page }) => {
  const queue = await open(page);
  await queue.set({ running: job(A, "First", { status: "separating" }), queued: [job(B, "Second"), job(C, "Third")] });
  await openQueueView(page);

  await page.locator(`.queue-row[data-id="${B}"] .queue-cancel`).click();

  await expect.poll(() => shownOrder(page)).toEqual([A, C]);
  expect(queue.sent.stop).toEqual([B]);
  await expect(page.locator("#queueBadge")).toHaveText("2");
});

test("a paused queue says so and Start sets it going", async ({ page }) => {
  const queue = await open(page);
  await queue.set({ paused: true, queued: [job(B, "Second"), job(C, "Third")] });
  await openQueueView(page);

  const banner = page.locator(".queue-paused-banner");
  await expect(banner).toContainText("Paused - 2 tracks from your last session.");
  await expect(page.locator(`.queue-row[data-id="${B}"] .cat-queue-label`)).toHaveText("Paused");

  await page.locator(".queue-start-btn").click();

  await expect(banner).toHaveCount(0);
  expect(queue.sent.start).toBe(1);
  await expect(page.locator(`.queue-row[data-id="${B}"] .cat-queue-label`)).toHaveText("Queued - #1 in line");
});

test("the view goes back to the library when the queue empties", async ({ page }) => {
  const queue = await open(page);
  await queue.set({ running: job(A, "First", { status: "separating" }) });
  await openQueueView(page);

  await queue.set({});

  await expect(page.locator(".queue-section")).toHaveCount(0);
  await expect(row(page, JOB_ID)).toBeVisible();
  await expect(page.locator(".rail-queue")).toBeHidden();
});
