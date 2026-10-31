// The Sections "Add" button is greyed out exactly when pressing it would do
// nothing: a song whose sections leave no room for another one (#775).
//
// Before, a song with detected sections that fill the track showed an Add
// button that looked live and silently did nothing.
import { test, expect } from "@playwright/test";
import { openStudio, JOB_ID } from "./helpers.mjs";

test.afterEach(async ({ page }) => {
  await page.request.patch(`/api/jobs/${JOB_ID}/sections`, { data: { sections: [] } });
});

async function seed(page, sections) {
  const res = await page.request.patch(`/api/jobs/${JOB_ID}/sections`, { data: { sections } });
  expect(res.ok(), "seeding sections").toBe(true);
}

// seed.py writes a 6 second fixture.
const FULL = [
  { id: "a", name: "Verse", start: 0, end: 3, color: "#4a7fff" },
  { id: "b", name: "Chorus", start: 3, end: 6, color: "#ff7a4a" },
];
const ROOM = [{ id: "a", name: "Verse", start: 0, end: 2, color: "#4a7fff" }];

const add = (page) => page.locator("#sectionsAddBtn");

test("Add is greyed out, with a reason, when sections fill the song", async ({ page }) => {
  await seed(page, FULL);
  await openStudio(page);
  await expect(page.locator(".section-block")).toHaveCount(2);

  await expect(add(page)).toBeDisabled();
  await expect(add(page)).toHaveAttribute("title", "No free space for a new section");

  // A press that gets through anyway must still change nothing.
  await add(page).evaluate((el) => el.click());
  await expect(page.locator(".section-block")).toHaveCount(2);
});

test("Add stays enabled, and works, when there is room", async ({ page }) => {
  await seed(page, ROOM);
  await openStudio(page);
  await expect(page.locator(".section-block")).toHaveCount(1);

  await expect(add(page)).toBeEnabled();
  await expect(add(page)).toHaveAttribute("title", "Add section");
  await add(page).click();
  await expect(page.locator(".section-block")).toHaveCount(2);
});

test("Add is enabled with no sections, and again after clearing a full set", async ({ page }) => {
  await seed(page, []);
  await openStudio(page);
  await expect(add(page)).toBeEnabled();
  await add(page).click();
  await expect(page.locator(".section-block")).toHaveCount(1);

  await seed(page, FULL);
  await openStudio(page);
  await expect(add(page)).toBeDisabled();
  await page.locator("#sectionsClearBtn").click();
  await page.locator("#sectionsClearBtn").click();
  await expect(page.locator(".section-block")).toHaveCount(0);
  await expect(add(page)).toBeEnabled();
});

test("Add updates as a section is deleted", async ({ page }) => {
  await seed(page, FULL);
  await openStudio(page);
  await expect(add(page)).toBeDisabled();
  await page.locator('.section-block[data-id="b"] .section-del').evaluate((el) => el.click());
  await expect(page.locator(".section-block")).toHaveCount(1);
  await expect(add(page)).toBeEnabled();
});

test("Add follows the loop: a loop in a small gap makes room", async ({ page }) => {
  // The only gap, 2.5s to 3.1s, is narrower than a default section, so Add has
  // nowhere to put one until a loop there says where.
  await seed(page, [
    { id: "a", name: "Verse", start: 0, end: 2.5, color: "#4a7fff" },
    { id: "b", name: "Chorus", start: 3.1, end: 6, color: "#ff7a4a" },
  ]);
  await openStudio(page);
  await expect(add(page)).toBeDisabled();

  await page.locator("#t-loop-end").fill("03.100");
  await page.locator("#t-loop-end").blur();
  await page.locator("#t-loop-start").fill("02.500");
  await page.locator("#t-loop-start").blur();
  await expect(page.locator("#t-loop")).toHaveClass(/active/);
  await expect(add(page)).toBeEnabled();

  await page.locator("#t-loop").click();
  await expect(page.locator("#t-loop")).not.toHaveClass(/active/);
  await expect(add(page)).toBeDisabled();
});
