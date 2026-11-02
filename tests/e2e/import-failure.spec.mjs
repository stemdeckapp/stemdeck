// A failed import says its cause in a sentence and can be tried again from the
// message itself (#175). The box used to show the server's English words, the
// cause as a bare key ("source-blocked - ..."), and a Try again that only put
// the cursor in the empty link box, so a failed restore had no retry. Resync
// gave no result line at all.
//
// Every import here is stubbed: nothing reaches YouTube, and the e2e backend
// never separates. A restore that succeeds is answered with the seeded fixture
// job, whose stems are real files.

import { test, expect } from "@playwright/test";
import { JOB_ID, SIBLING_JOB_ID, SIBLING_TITLE, fixtureTrack, seedCatalogState, stubImportQueue, stubUpdateCheck } from "./helpers.mjs";
import { TRANSLATIONS } from "../../static/js/i18n.js";

const en = TRANSLATIONS.en;
const DASH = String.fromCharCode(0x2014);
const LINK = "https://www.youtube.com/watch?v=dQw4w9WgXcQ";
const LINK_B = "https://www.youtube.com/watch?v=aaaaaaaaaaa";
const FAILED = "f1a1f1a1f1a1";
const RETRIED = "f2a2f2a2f2a2";
const UPLOAD = "abcabcabcabc";
const SERVER_WORDS = "Audio processing failed. Please try another video.";
const count = (key, n) => en[key].replace("{count}", String(n));

function failedState(jobId, sourceUrl, errorDetail) {
  return {
    job_id: jobId,
    status: "error",
    stage: "Error: Processing failed",
    title: "Gone Song",
    error: SERVER_WORDS,
    error_detail: errorDetail,
    source_url: sourceUrl,
    selected_stems: ["vocals", "drums"],
    stems: [],
  };
}

const queuedState = (jobId) => ({ job_id: jobId, status: "queued", stage: "Queued", progress: 0, stems: [] });

/** Answer a job's stream and its state read with `state`. */
async function stubJob(page, jobId, state) {
  await page.route(`**/api/jobs/${jobId}/events`, (route) =>
    route.fulfill({ status: 200, contentType: "text/event-stream", body: `data: ${JSON.stringify(state)}\n\n` }));
  await page.route(`**/api/jobs/${jobId}`, (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    return route.fulfill({ json: state });
  });
}

/** Answer POST /api/jobs with `answer(body, n)` ({ status, json }); resolves to the bodies sent. */
async function stubSubmit(page, answer) {
  const posts = [];
  await page.route("**/api/jobs", (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    const body = JSON.parse(route.request().postData() || "{}");
    posts.push(body);
    const { status = 200, json } = answer(body, posts.length);
    return route.fulfill({ status, json });
  });
  return posts;
}

/** The library the server knows is empty, so nothing seeded here is touched by the sync. */
async function emptyServerLibrary(page) {
  await page.route(/\/api\/jobs(\?.*)?$/, (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    return route.fulfill({ json: [] });
  });
}

async function openApp(page, tracks) {
  await seedCatalogState(page, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: Object.keys(tracks), color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks,
  });
  await stubUpdateCheck(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(".cat-item").first().waitFor({ timeout: 20000 });
}

const restore = (page, url) =>
  page.evaluate(
    async (link) => (await import("/js/job.js")).importFromUrl(link, { title: "Gone Song", stems: ["vocals", "drums"] }),
    url,
  );

test.describe("a failed link import", () => {
  test("says its cause in a sentence, with the server's words below it", async ({ page }) => {
    await stubSubmit(page, () => ({ json: { job_id: FAILED } }));
    await stubJob(page, FAILED, failedState(FAILED, LINK, `source-blocked ${DASH} Sign in to confirm you're not a bot`));
    await openApp(page, { [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, SIBLING_TITLE) });

    await restore(page, LINK);
    const box = page.locator("#error");
    await expect(box.locator(".error-msg")).toContainText(en["job.cause.sourceBlocked"]);
    await expect(box.locator(".error-detail")).toHaveText("Sign in to confirm you're not a bot");
    await expect(box).not.toContainText("source-blocked");
    await expect(box).not.toContainText(SERVER_WORDS);
  });

  test("Try again imports the job's own link again, with its stems", async ({ page }) => {
    const posts = await stubSubmit(page, (_body, n) => ({ json: { job_id: n === 1 ? FAILED : RETRIED } }));
    await stubJob(page, FAILED, failedState(FAILED, LINK, `source-unavailable ${DASH} Video unavailable`));
    await stubJob(page, RETRIED, queuedState(RETRIED));
    await openApp(page, { [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, SIBLING_TITLE) });

    await restore(page, LINK);
    const retry = page.locator("#error .import-retry-btn");
    await expect(retry).toHaveText(en["job.tryAgain"]);
    await expect(page.locator("#url")).toHaveValue("");
    await retry.click();

    await expect.poll(() => posts.length).toBe(2);
    expect(posts[1].url).toBe(LINK);
    expect(posts[1].stems).toEqual(["vocals", "drums"]);
    await expect(page.locator("#error")).toBeHidden();
    await expect(page.locator(`.cat-item[data-id="${RETRIED}"]`).first()).toBeVisible();
  });

  test("refused by the server, Try again asks again for the same link", async ({ page }) => {
    const posts = await stubSubmit(page, (_body, n) =>
      n === 1 ? { status: 503, json: { detail: "Queue is full" } } : { json: { job_id: RETRIED } });
    await stubJob(page, RETRIED, queuedState(RETRIED));
    await openApp(page, { [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, SIBLING_TITLE) });

    await restore(page, LINK);
    await expect(page.locator("#error")).toContainText("Queue is full");
    await page.locator("#error .import-retry-btn").click();

    await expect.poll(() => posts.length).toBe(2);
    expect(posts[1].url).toBe(LINK);
    expect(posts[1].stems).toEqual(["vocals", "drums"]);
  });
});

test.describe("a failed upload", () => {
  test("says its cause and to add the file again, and offers no Try again", async ({ page }) => {
    const queue = await stubImportQueue(page, UPLOAD, "My Song");
    await stubJob(page, UPLOAD, { ...failedState(UPLOAD, "local:My Song", `out-of-memory ${DASH} CUDA out of memory`), title: "My Song" });
    await openApp(page, {
      [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, SIBLING_TITLE),
      [UPLOAD]: { ...fixtureTrack(UPLOAD, "My Song"), status: "processing", sourceUrl: "local:My Song" },
    });
    await queue.run();
    await queue.settle();

    const sentence = en["job.uploadFailed"].replace("{cause}", en["job.cause.outOfMemory"]);
    await page.locator("#notifBtn").click();
    const card = page.locator(".daw-notif-error").first();
    await expect(card).toContainText(sentence);
    await expect(card).not.toContainText("out-of-memory");
    const row = page.locator(`.cat-item[data-id="${UPLOAD}"]`).first();
    await expect(row).toBeVisible();
    await expect(row.locator(".cat-extract")).toHaveCount(0);
  });
});

test.describe("Resync", () => {
  test("says how many tracks came back, which failed and why, and which are local files", async ({ page }) => {
    await emptyServerLibrary(page);
    // The first link comes back as the seeded fixture job; the second fails.
    await stubSubmit(page, (body) => ({ json: { job_id: body.url === LINK ? JOB_ID : FAILED } }));
    await stubJob(page, FAILED, { ...failedState(FAILED, LINK_B, `source-unavailable ${DASH} Video unavailable`), title: "Blocked Song" });
    const gone = (id, title, sourceUrl) => ({ ...fixtureTrack(id, title), status: "unavailable", sourceUrl });
    await openApp(page, {
      aaaaaaaaaaa1: gone("aaaaaaaaaaa1", "Comes Back", LINK),
      aaaaaaaaaaa2: gone("aaaaaaaaaaa2", "Blocked Song", LINK_B),
      aaaaaaaaaaa3: gone("aaaaaaaaaaa3", "Old Upload", "local:Old Upload"),
    });

    await page.locator("#settingsBtn").click();
    await page.locator('.settings-tab[data-tab="library"]').click();
    await page.locator(".library-editor-sync").click();

    const box = page.locator("#error");
    const summary = [
      count("settings.outOfSync.restored.one", 1),
      count("settings.outOfSync.failed.one", 1),
      count("settings.outOfSync.localOnly.one", 1),
    ].join(" ");
    await expect(box.locator(".error-msg")).toContainText(summary, { timeout: 30000 });
    await expect(box.locator(".error-detail")).toHaveText(
      en["settings.outOfSync.failedTrack"].replace("{title}", "Blocked Song").replace("{reason}", en["job.cause.sourceUnavailable"]),
    );
    await expect(box.locator(".import-retry-btn")).toHaveCount(0);
  });

  test("with only local files says they must be added again", async ({ page }) => {
    await emptyServerLibrary(page);
    await openApp(page, {
      aaaaaaaaaaa3: { ...fixtureTrack("aaaaaaaaaaa3", "Old Upload"), status: "unavailable", sourceUrl: "local:Old Upload" },
    });

    await page.locator("#settingsBtn").click();
    await page.locator('.settings-tab[data-tab="library"]').click();
    const status = page.locator(".library-editor-status");
    await expect(status).toHaveText(count("settings.outOfSync.summary.one", 1));
    await page.locator(".library-editor-sync").click();
    await expect(status).toHaveText(count("settings.outOfSync.localOnly.one", 1));
  });
});
