// What the Extract stems button is allowed to be pointed at (#635).
//
// The composer is an input the button submits, not a caption for whatever
// track is open. Opening a track writes its source back into it, and for an
// uploaded file that source is the synthetic "local:my song.mp3". Stripping
// the prefix and showing the bare filename armed the button with a string that
// can never resolve: pressing it POSTed "my song.mp3" as though it were a
// link, the server refused it, and the reporter got an error for pressing a
// button that looked like it should re-split the song in front of them.
//
// Being non-empty is what made it reachable at all -- an empty box is stopped
// by the browser's own required check before any of our code runs.
//
// The other half of the rule -- a real link still lands in the box, ready to
// re-import -- is tests/js/source-arming.test.mjs. Asserting it here meant
// rewriting syncWithServer's own payload to fight the fixture's local source,
// which tests the stub more than it tests the app.
import { expect, test } from "@playwright/test";
import { openStudio, JOB_ID, TRACK_TITLE, resetVersions } from "./helpers.mjs";

const YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ";

test.describe("composer arming", () => {
  test("an uploaded track puts no filename in the box, and aims the button at itself", async ({
    page,
  }) => {
    // seed.py's fixture is sourceUrl "local:e2e-fixture", which is exactly
    // the case that broke.
    await openStudio(page);

    const url = page.locator("#url");
    await expect(url).toHaveValue("");
    // The button acts on the track instead, so the box must NOT be required:
    // the browser refuses a required-and-empty submit before any handler runs,
    // which would make re-splitting an upload impossible.
    await expect(url).not.toHaveAttribute("required", "");
    await expect(page.locator("#submit")).toHaveAttribute("data-resplit-job", JOB_ID);
    // And it says so: an empty box with no other signal left no way to tell
    // what pressing the button would act on.
    await expect(page.locator("#trackPill")).toBeVisible();
    await expect(page.locator("#trackPillName")).toHaveText(TRACK_TITLE);
  });

  test("an unfinished upload has nothing to act on at all", async ({ page }) => {
    // Neither a URL to import nor stems to re-separate from. This is the one
    // case where the empty box should stop the submit itself.
    await openStudio(page);
    await page.evaluate(() => {
      document.getElementById("submit").dataset.resplitJob = "";
      document.getElementById("url").setAttribute("required", "");
    });

    await page.locator("#submit").click();
    // Still on the page, nothing queued: the browser refused it.
    await expect(page.locator("#url")).toHaveValue("");
  });

  test("pressing the button on the open upload makes a version, never a re-split", async ({ page, request }) => {
    // The end-to-end wiring, and the one that catches a guard placed wrongly.
    // `required` on an empty box is refused by the browser before any handler
    // runs, so setting it here made the button answer "please fill out this
    // field" instead of acting on the track -- invisible to every assertion
    // that only looks at attributes.
    //
    // Since #804 the song is never separated over again: the chips become a
    // version of it, sharing its stems, and no second job is made.
    await openStudio(page);

    const posts = [];
    page.on("request", (req) => {
      if (req.method() === "POST") posts.push(new URL(req.url()).pathname);
    });
    await page.locator("#stemAllBtn").click(); // all off
    await page.locator('.stem-choice[data-stem="drums"]').click();
    await page.locator("#submit").click();

    await expect.poll(() => posts.filter((u) => u === `/api/jobs/${JOB_ID}/versions`).length, { timeout: 10000 }).toBe(1);
    expect(posts.filter((u) => u.endsWith("/resplit") || u === "/api/jobs")).toEqual([]);
    // The track it came from is still the one in the library, with its versions.
    await expect(page.locator(`.cat-item[data-id="${JOB_ID}"]`).first()).toBeVisible();
    await expect(page.locator(`#catalogList .cat-version[data-id="${JOB_ID}"]`)).toHaveCount(2);

    // Put the shared fixture back to one version for the specs after this one.
    await resetVersions(request);
  });

  test("a typed URL still wins over the open upload", async ({ page }) => {
    // The composer is not disabled while such a track is open: putting a link
    // in it is a new import, not a re-split of what happens to be loaded.
    await openStudio(page);

    const hits = [];
    await page.route("**/api/jobs**", async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      hits.push(route.request().url());
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ job_id: "cafebabe0002" }),
      });
    });

    await page.locator("#url").fill(YOUTUBE_URL);
    await page.locator("#submit").click();

    await expect.poll(() => hits.length, { timeout: 10000 }).toBe(1);
    expect(hits[0]).not.toContain("resplit");
  });

  test("the button keeps its name after a submit", async ({ page }) => {
    // setSubmitProcessing restored a label the markup never used, so the first
    // submit of a session renamed the button to "Process" for good -- on a
    // successful import as much as on a failed one.
    await openStudio(page);
    const label = page.locator("#submit span");
    const before = await label.textContent();

    await page.route("**/api/jobs", (route) =>
      route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "nope" }) }),
    );
    await page.locator("#url").fill(YOUTUBE_URL);
    await page.locator("#submit").click();

    await expect(label).toHaveText(before.trim(), { timeout: 10000 });
  });
});
