// Renaming a section is double-click, type, Enter. It had no browser test at
// all, which is how adding a click gesture to the same element could break it
// without anything going red.
import { test, expect } from "@playwright/test";
import { openStudio, JOB_ID } from "./helpers.mjs";

test.afterEach(async ({ page }) => {
  await page.request.patch(`/api/jobs/${JOB_ID}/sections`, { data: { sections: [] } });
});

async function seed(page, sections) {
  const res = await page.request.patch(`/api/jobs/${JOB_ID}/sections`, { data: { sections } });
  expect(res.ok(), "seeding sections").toBe(true);
}

const savedSections = async (page) => {
  const res = await page.request.get(`/api/jobs/${JOB_ID}`);
  return (await res.json()).sections ?? [];
};

test.describe("renaming a section", () => {
  test("double-clicking the label opens an input that keeps focus", async ({ page }) => {
    await seed(page, [{ id: "s1", name: "Verse", start: 1, end: 3, color: "#4a7fff" }]);
    await openStudio(page);

    await page.locator('.section-block[data-id="s1"] .section-label').dblclick();

    // The input must still be there a moment later. If anything steals focus,
    // the blur handler commits and re-renders, and the field vanishes as fast
    // as it appeared, which reads as "renaming does not work".
    const input = page.locator(".section-rename-input");
    await expect(input).toBeVisible();
    await page.waitForTimeout(250);
    await expect(input).toBeVisible();
    await expect(input).toBeFocused();
  });

  test("typing a new name and pressing Enter saves it", async ({ page }) => {
    await seed(page, [{ id: "s1", name: "Verse", start: 1, end: 3, color: "#4a7fff" }]);
    await openStudio(page);

    await page.locator('.section-block[data-id="s1"] .section-label').dblclick();
    await page.locator(".section-rename-input").fill("Chorus");
    await page.locator(".section-rename-input").press("Enter");

    await expect(page.locator('.section-block[data-id="s1"] .section-label')).toHaveText("Chorus");
    await expect.poll(async () => (await savedSections(page))[0]?.name).toBe("Chorus");
  });

  test("a locked section refuses to open a rename field", async ({ page }) => {
    await seed(page, [
      { id: "s1", name: "Verse", start: 1, end: 3, color: "#4a7fff", locked: true },
    ]);
    await openStudio(page);

    await page.locator('.section-block[data-id="s1"] .section-label').dblclick();

    await expect(page.locator(".section-rename-input")).toHaveCount(0);
    await expect(page.locator('.section-block[data-id="s1"] .section-label')).toHaveText("Verse");
  });

  test("unlocking restores renaming", async ({ page }) => {
    await seed(page, [
      { id: "s1", name: "Verse", start: 1, end: 3, color: "#4a7fff", locked: true },
    ]);
    await openStudio(page);

    // A locked padlock is visible at rest, so no hover is needed here.
    await page.locator('.section-block[data-id="s1"] .section-lock').click();
    await expect(page.locator('.section-block[data-id="s1"]')).not.toHaveClass(/sec-locked/);

    await page.locator('.section-block[data-id="s1"] .section-label').dblclick();
    await page.locator(".section-rename-input").fill("Bridge");
    await page.locator(".section-rename-input").press("Enter");

    await expect.poll(async () => (await savedSections(page))[0]?.name).toBe("Bridge");
  });

  test("Escape leaves the original name alone", async ({ page }) => {
    await seed(page, [{ id: "s1", name: "Verse", start: 1, end: 3, color: "#4a7fff" }]);
    await openStudio(page);

    await page.locator('.section-block[data-id="s1"] .section-label').dblclick();
    await page.locator(".section-rename-input").fill("Discarded");
    await page.locator(".section-rename-input").press("Escape");

    await expect(page.locator('.section-block[data-id="s1"] .section-label')).toHaveText("Verse");
  });
});
