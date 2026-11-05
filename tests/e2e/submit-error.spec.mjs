// What the error box says when an import or a version delete is refused.
//
// An import refused by a proxy in front of the server (an oversize upload
// answered with an HTML 413 page) showed "Failed to start job: Unexpected
// token '<'", because the answer was parsed as JSON before its status was
// checked. A batch of files said "Queued 0 of 2 files" in English whatever the
// language, and a refused version delete showed the server's own words.
//
// POST /api/jobs and the version DELETE never leave the page.

import { test, expect } from "@playwright/test";
import { openStudio, JOB_ID, resetVersions } from "./helpers.mjs";

const URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ";
const PROXY_413 = { status: 413, contentType: "text/html", body: "<html><body><h1>413 Request Entity Too Large</h1></body></html>" };

async function refuseImports(page) {
  await page.route("**/api/jobs", (route) => (route.request().method() === "POST" ? route.fulfill(PROXY_413) : route.continue()));
}

async function inGerman(page) {
  await page.addInitScript(() => localStorage.setItem("stemdeck.language", JSON.stringify("de")));
}

async function dropFiles(page, names) {
  const handle = await page.evaluateHandle((list) => {
    const dt = new DataTransfer();
    for (const n of list) dt.items.add(new File([new Uint8Array(64)], n, { type: "audio/wav" }));
    return dt;
  }, names);
  await page.dispatchEvent("body", "dragover", { dataTransfer: handle });
  await page.dispatchEvent("body", "drop", { dataTransfer: handle });
}

test.describe("a refused submit", () => {
  test("an import answered with a proxy's HTML page says the status, not a parse error", async ({ page }) => {
    await openStudio(page);
    await refuseImports(page);
    await page.locator("#url").fill(URL);
    await page.locator("#submit").click();

    const box = page.locator("#error");
    await expect(box).toBeVisible();
    await expect(box).toContainText(/Failed to start job: \S/);
    await expect(box).not.toContainText("Unexpected token");
    await expect(box).not.toContainText("JSON");
  });

  test("an ok answer that is not JSON says no reason was given, not a script error", async ({ page }) => {
    await openStudio(page);
    await page.route("**/api/jobs", (route) =>
      route.request().method() === "POST"
        ? route.fulfill({ status: 200, contentType: "text/html", body: "<html><body>OK</body></html>" })
        : route.continue(),
    );
    await page.locator("#url").fill(URL);
    await page.locator("#submit").click();

    const box = page.locator("#error");
    await expect(box).toBeVisible();
    await expect(box).toContainText("Failed to start job. No reason was given; please try again.");
    await expect(box).not.toContainText("Cannot read");
  });

  test("a refused batch of files is reported in the chosen language", async ({ page }) => {
    await inGerman(page);
    await openStudio(page);
    await refuseImports(page);
    await dropFiles(page, ["one.wav", "two.wav"]);
    await page.locator("#submit").click();

    const box = page.locator("#error");
    await expect(box).toBeVisible();
    await expect(box).toContainText("0 von 2 Dateien");
    await expect(box).not.toContainText("Queued");
    await expect(box).not.toContainText("Unexpected token");
  });
});

test.describe("a refused version delete", () => {
  test.beforeEach(async ({ request }) => { await resetVersions(request); });
  test.afterEach(async ({ request }) => { await resetVersions(request); });

  test("from its library row shows a sentence in the chosen language, not the server's words", async ({ page, request }) => {
    await request.post(`/api/jobs/${JOB_ID}/versions`, { data: { name: "Guitar study", stems: ["drums", "vocals"], select: false } });
    await inGerman(page);
    await page.setViewportSize({ width: 1280, height: 820 });
    await openStudio(page);
    await page.route("**/api/jobs/*/versions/*", (route) =>
      route.request().method() === "DELETE"
        ? route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "Version store is locked" }) })
        : route.continue(),
    );
    const row = page.locator(`#catalogList .cat-version[data-id="${JOB_ID}"]`).nth(1);
    await expect(row).toBeVisible();
    await row.hover();
    const del = row.locator(".cat-version-del");
    await del.click();
    await del.click();

    const box = page.locator("#error");
    await expect(box).toBeVisible();
    await expect(box).toContainText("Das hat nicht geklappt. Nichts wurde geändert.");
    await expect(box).not.toContainText("Version store is locked");
  });
});
