// A build is offered to users only when GitHub itself calls it the latest
// release.
//
// GitHub's publish dialog has three states, not two: set as the latest
// release, set as a pre-release, or neither. The updater used to read the list
// endpoint and take the first release that was neither a draft nor a
// pre-release, which cannot tell the third state from the first: a release
// published with nothing ticked comes back as `draft: false, prerelease:
// false` and was pushed to every install (#666).
//
// So these are about which releases stay silent, which is the half that has no
// visible symptom until it is too late.
import { test, expect } from "@playwright/test";
import { seedLibrary, stubExportEndpoints } from "./helpers.mjs";

// The app skips the check on dev builds, so it has to look like a release.
const stubVersion = (page, version) =>
  page.route("**/api/health**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        name: "StemDeck",
        status: "ok",
        version,
        ffmpeg_configured: true,
        demucs_model: "htdemucs_6s",
        demucs_device: "cpu",
      }),
    }),
  );

const stubGithub = (page, { status = 200, body = null }) =>
  page.route("https://api.github.com/**", (route) =>
    route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(body ?? { message: "Not Found" }),
    }),
  );

const release = (over = {}) => ({
  tag_name: "v9.9.9",
  draft: false,
  prerelease: false,
  body: "notes",
  html_url: "https://example.invalid",
  assets: [],
  ...over,
});

async function open(page) {
  await seedLibrary(page);
  await stubExportEndpoints(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
}

const cardShown = (page) =>
  page.evaluate(
    () => !document.getElementById("notifReleaseCard")?.classList.contains("hidden"),
  );

test.describe("update check", () => {
  test("a release GitHub calls latest is offered", async ({ page }) => {
    await stubVersion(page, "0.5.0");
    await stubGithub(page, { body: release() });
    await open(page);

    await expect.poll(() => cardShown(page)).toBe(true);
  });

  test("nothing is offered while every release is a pre-release", async ({ page }) => {
    // /releases/latest 404s when there is no promoted release, which is exactly
    // the state a project is in between publishing and promoting. Before this,
    // the list endpoint would hand back the newest stable release regardless.
    await stubVersion(page, "0.5.0");
    await stubGithub(page, { status: 404 });
    await open(page);

    await page.waitForTimeout(1200);
    expect(await cardShown(page)).toBe(false);
  });

  test("a pre-release is not offered even if the endpoint returns one", async ({ page }) => {
    // Belt and braces: the endpoint is documented not to return these, and the
    // app should stay quiet rather than inherit a change in that behaviour.
    await stubVersion(page, "0.5.0");
    await stubGithub(page, { body: release({ prerelease: true }) });
    await open(page);

    await page.waitForTimeout(1200);
    expect(await cardShown(page)).toBe(false);
  });

  test("a draft is not offered either", async ({ page }) => {
    await stubVersion(page, "0.5.0");
    await stubGithub(page, { body: release({ draft: true }) });
    await open(page);

    await page.waitForTimeout(1200);
    expect(await cardShown(page)).toBe(false);
  });

  test("the current version is not offered to itself", async ({ page }) => {
    await stubVersion(page, "9.9.9");
    await stubGithub(page, { body: release() });
    await open(page);

    await page.waitForTimeout(1200);
    expect(await cardShown(page)).toBe(false);
  });

  // The bug itself, which needs both endpoints stubbed to express.
  //
  // A release published with neither box ticked is absent from
  // /releases/latest and present in the list, indistinguishable there from a
  // promoted one. Reading the list offered it; asking for the latest release
  // does not see it at all.
  test("a release published as neither latest nor pre-release is not offered", async ({ page }) => {
    await stubVersion(page, "0.5.0");
    await page.route("https://api.github.com/**", (route) => {
      const url = route.request().url();
      if (url.includes("/releases/latest")) {
        return route.fulfill({ status: 404, contentType: "application/json",
          body: JSON.stringify({ message: "Not Found" }) });
      }
      // The list still carries it, and says nothing about it being unpromoted.
      return route.fulfill({ status: 200, contentType: "application/json",
        body: JSON.stringify([release({ tag_name: "v9.9.11", body: "published as none" })]) });
    });
    await open(page);

    await page.waitForTimeout(1200);
    expect(await cardShown(page)).toBe(false);
  });
});
