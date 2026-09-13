// The loop and the sections ribbon describe the same spans, so each should be
// able to become the other (#573, and #474 asked for the same thing).
//
// Before this, arming a loop and pressing Add built a section at the start of
// the track instead. Add only ever looked for the first gap that fit, so the
// selection you had just made was ignored with nothing saying so, which reads
// as the button being broken rather than as a feature that does not exist.
//
// Everything here goes through the real controls: the loop is typed into the
// transport's own fields and read back from them, so nothing depends on a test
// hook that could keep passing after the UI stopped working.
import { test, expect } from "@playwright/test";
import { openStudio, JOB_ID } from "./helpers.mjs";

// seed.py writes a 6 second fixture, so every span here stays inside it.
const FIXTURE_SEC = 6;

test.afterEach(async ({ page }) => {
  await page.request.patch(`/api/jobs/${JOB_ID}/sections`, { data: { sections: [] } });
});

/** "00:02.000", the format fmtTimeMs writes into the loop fields. */
const tc = (seconds) => {
  const ms = Math.round(seconds * 1000);
  const m = String(Math.floor(ms / 60000)).padStart(2, "0");
  const s = String(Math.floor((ms % 60000) / 1000)).padStart(2, "0");
  return `${m}:${s}.${String(ms % 1000).padStart(3, "0")}`;
};

/** Arm the loop the way a user does: type both bounds, blur to commit. */
async function armLoop(page, start, end) {
  await page.locator("#t-loop-end").fill(tc(end));
  await page.locator("#t-loop-end").blur();
  await page.locator("#t-loop-start").fill(tc(start));
  await page.locator("#t-loop-start").blur();
  await expect(page.locator("#t-loop")).toHaveClass(/active/);
}

async function loopState(page) {
  return {
    enabled: await page.locator("#t-loop").evaluate((el) => el.classList.contains("active")),
    start: await page.locator("#t-loop-start").inputValue(),
    end: await page.locator("#t-loop-end").inputValue(),
  };
}

async function seed(page, sections) {
  const res = await page.request.patch(`/api/jobs/${JOB_ID}/sections`, { data: { sections } });
  expect(res.ok(), "seeding sections").toBe(true);
}

const savedSections = async (page) => {
  const res = await page.request.get(`/api/jobs/${JOB_ID}`);
  return (await res.json()).sections ?? [];
};

const blockIds = (page) =>
  page.evaluate(() =>
    [...document.querySelectorAll(".section-block")].map((el) => el.dataset.id),
  );

test.describe("sections and the loop", () => {
  test("Add builds the section on the armed loop, not at the start of the track", async ({
    page,
  }) => {
    await openStudio(page);
    await armLoop(page, 2, 4);

    await page.locator("#sectionsAddBtn").click();

    await expect.poll(async () => (await savedSections(page)).length).toBe(1);
    const [section] = await savedSections(page);
    // The whole complaint: this used to be 0, wherever the loop was.
    expect(section.start).toBeCloseTo(2, 2);
    expect(section.end).toBeCloseTo(4, 2);
  });

  test("Add still finds a gap when no loop is armed", async ({ page }) => {
    await openStudio(page);

    await page.locator("#sectionsAddBtn").click();

    await expect.poll(async () => (await savedSections(page)).length).toBe(1);
    const [section] = await savedSections(page);
    expect(section.start).toBeCloseTo(0, 2);
    expect(section.end).toBeLessThan(FIXTURE_SEC);
  });

  test("a loop across an existing section is refused, and says why", async ({ page }) => {
    await seed(page, [{ id: "s1", name: "Verse", start: 1, end: 3, color: "#4a7fff" }]);
    await openStudio(page);
    await expect(page.locator(".section-block")).toHaveCount(1);

    await armLoop(page, 2, 4); // straddles s1
    await page.locator("#sectionsAddBtn").click();

    // Refused rather than built somewhere else, and rather than displacing a
    // section the user may well have locked.
    expect(await blockIds(page)).toEqual(["s1"]);
    const notice = page.locator("#sectionsSaveIndicator.notice");
    await expect(notice).toBeVisible();
    await expect(notice).not.toBeEmpty();
  });

  test("clicking a section loops over it", async ({ page }) => {
    await seed(page, [{ id: "s1", name: "Chorus", start: 1, end: 3, color: "#4a7fff" }]);
    await openStudio(page);

    await page.locator('.section-block[data-id="s1"]').click();

    expect(await loopState(page)).toEqual({
      enabled: true,
      start: tc(1),
      end: tc(3),
    });
  });

  test("a locked section can still be clicked to loop", async ({ page }) => {
    await seed(page, [
      { id: "s1", name: "Chorus", start: 1, end: 3, color: "#4a7fff", locked: true },
    ]);
    await openStudio(page);

    const block = page.locator('.section-block[data-id="s1"]');
    await expect(block).toHaveClass(/sec-locked/);
    await block.click();

    // Lock is about position. Refusing to loop a locked section would be a
    // second rule nobody asked for.
    expect((await loopState(page)).enabled).toBe(true);
    expect((await loopState(page)).start).toBe(tc(1));
  });

  test("dragging a section does not also arm a loop over it", async ({ page }) => {
    await seed(page, [{ id: "s1", name: "Verse", start: 1, end: 2, color: "#4a7fff" }]);
    await openStudio(page);

    const box = await page.locator('.section-block[data-id="s1"]').boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width / 2 + 40, box.y + box.height / 2, { steps: 8 });
    await page.mouse.up();

    // A drag ends in a pointerup too, so without the "nothing moved" check
    // every move would silently retarget the loop as well.
    expect((await loopState(page)).enabled).toBe(false);
  });
});
