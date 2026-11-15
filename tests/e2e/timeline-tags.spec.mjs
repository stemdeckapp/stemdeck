// Markers (#1075, "timeline tags" in the code): named points on the
// timeline. A click moves the playhead to one and nothing else, where a
// section always loops, and keys 1 to 9 reach
// the first nine. Everything goes through the real controls and is read back
// from the page and the server, so nothing passes on a test hook.
import { test, expect } from "@playwright/test";
import { openStudio, JOB_ID, SIBLING_JOB_ID } from "./helpers.mjs";

// seed.py writes a 6 second fixture, so every time here stays inside it.

test.afterEach(async ({ page }) => {
  await page.request.patch(`/api/jobs/${JOB_ID}/timeline-tags`, { data: { timeline_tags: [] } });
  await page.request.patch(`/api/jobs/${JOB_ID}/sections`, { data: { sections: [] } });
});

async function seedTags(page, tags) {
  const res = await page.request.patch(`/api/jobs/${JOB_ID}/timeline-tags`, { data: { timeline_tags: tags } });
  expect(res.ok(), "seeding tags").toBe(true);
}

async function seedSections(page, sections) {
  const res = await page.request.patch(`/api/jobs/${JOB_ID}/sections`, { data: { sections } });
  expect(res.ok(), "seeding sections").toBe(true);
}

const savedTags = async (page) => {
  const res = await page.request.get(`/api/jobs/${JOB_ID}`);
  return (await res.json()).timeline_tags ?? [];
};

/** Where the playhead is, in seconds: the ruler's own marker, as a share of
 * the duration the engine reports, so it holds if the fixture changes. */
const playhead = (page) =>
  page.evaluate(async () => {
    const { transport } = await import("/js/transport.js");
    const sec = transport()?.getDuration?.() || 0;
    const el = document.querySelector("#ruler-time .playhead-marker");
    return ((parseFloat(el?.style.left) || 0) * sec) / 100;
  });

const loopState = async (page) => ({
  enabled: await page.locator("#t-loop").evaluate((el) => el.classList.contains("active")),
  start: await page.locator("#t-loop-start").inputValue(),
  end: await page.locator("#t-loop-end").inputValue(),
});

const tag = (id) => `.tag-mark[data-id="${id}"]`;

test.describe("markers", () => {
  test("clicking a tag moves the playhead there and arms no loop", async ({ page }) => {
    await seedTags(page, [
      { id: "t1", name: "Verse two", time: 1.5 },
      { id: "t2", name: "Solo", time: 3.25 },
    ]);
    await openStudio(page);

    await page.locator(`${tag("t2")} .tag-seek`).click();
    await expect.poll(() => playhead(page)).toBeCloseTo(3.25, 2);
    await page.locator(`${tag("t1")} .tag-seek`).click();
    await expect.poll(() => playhead(page)).toBeCloseTo(1.5, 2);

    expect((await loopState(page)).enabled).toBe(false);
  });

  test("two tags inside one section each keep their own start, and the section still loops", async ({ page }) => {
    await seedSections(page, [{ id: "s1", name: "Chorus", start: 1, end: 4, color: "#4a7fff" }]);
    await seedTags(page, [
      { id: "t1", name: "Chorus in", time: 1.5 },
      { id: "t2", name: "Chorus out", time: 3.5 },
    ]);
    await openStudio(page);

    await page.locator('.section-block[data-id="s1"]').click();
    const looped = await loopState(page);
    expect(looped.enabled).toBe(true);

    await page.locator(`${tag("t2")} .tag-seek`).click();
    await expect.poll(() => playhead(page)).toBeCloseTo(3.5, 2);
    // The tag moved the playhead and left the section's loop as it was.
    expect(await loopState(page)).toEqual(looped);
  });

  test("keys 1 to 9 reach the tags in time order, and a field keeps its digits", async ({ page }) => {
    await seedTags(page, [
      { id: "late", name: "Late", time: 4 },
      { id: "early", name: "Early", time: 0.75 },
    ]);
    await openStudio(page);

    await page.locator("body").press("2");
    await expect.poll(() => playhead(page)).toBeCloseTo(4, 2);
    await page.locator("body").press("1");
    await expect.poll(() => playhead(page)).toBeCloseTo(0.75, 2);
    // A key with no tag does nothing.
    await page.locator("body").press("9");
    expect(await playhead(page)).toBeCloseTo(0.75, 2);

    // Typing a digit into a field types it, and leaves the playhead alone.
    await page.locator("#catalogSearch").fill("");
    await page.locator("#catalogSearch").press("2");
    await expect(page.locator("#catalogSearch")).toHaveValue("2");
    expect(await playhead(page)).toBeCloseTo(0.75, 2);
    await page.locator("#catalogSearch").fill("");
  });

  test("Add puts a named tag at the playhead and saves it", async ({ page }) => {
    await openStudio(page);
    // ] moves the playhead 5 s on.
    await page.locator("body").press("BracketRight");
    await expect.poll(() => playhead(page)).toBeCloseTo(5, 2);

    await page.locator("#tagsAddBtn").click();
    const input = page.locator(".tag-rename-input");
    await expect(input).toBeFocused();
    await input.fill("Outro");
    await input.press("Enter");

    await expect(page.locator(".tag-mark .tag-label")).toHaveText(["Outro"]);
    await expect.poll(async () => (await savedTags(page)).map((t) => [t.name, t.time])).toEqual([["Outro", 5]]);
  });

  test("Add on a spot that already has a tag renames that tag instead of stacking one", async ({ page }) => {
    await seedTags(page, [{ id: "t1", name: "Bridge", time: 2 }]);
    await openStudio(page);
    await page.locator(`${tag("t1")} .tag-seek`).click();

    await page.locator("#tagsAddBtn").click();

    await expect(page.locator(".tag-rename-input")).toHaveValue("Bridge");
    await page.locator(".tag-rename-input").press("Escape");
    await expect(page.locator(".tag-mark")).toHaveCount(1);
  });

  test("double-click renames, Escape keeps the old name, and the cross deletes", async ({ page }) => {
    await seedTags(page, [
      { id: "t1", name: "Verse", time: 1 },
      { id: "t2", name: "Solo", time: 3 },
    ]);
    await openStudio(page);

    await page.locator(tag("t1")).dblclick();
    await page.locator(".tag-rename-input").fill("Verse 2");
    await page.locator(".tag-rename-input").press("Enter");
    await expect(page.locator(`${tag("t1")} .tag-label`)).toHaveText("Verse 2");

    await page.locator(tag("t2")).dblclick();
    await page.locator(".tag-rename-input").fill("nothing");
    await page.locator(".tag-rename-input").press("Escape");
    await expect(page.locator(`${tag("t2")} .tag-label`)).toHaveText("Solo");

    await page.locator(tag("t2")).hover();
    await page.locator(`${tag("t2")} .tag-del`).click();
    await expect(page.locator(".tag-mark")).toHaveCount(1);
    await expect.poll(async () => (await savedTags(page)).map((t) => t.name)).toEqual(["Verse 2"]);
  });

  test("tags come back after a reload", async ({ page }) => {
    await seedTags(page, [{ id: "t1", name: "Keep me", time: 2.5 }]);
    await openStudio(page);
    await expect(page.locator(`${tag("t1")} .tag-label`)).toHaveText("Keep me");

    await page.reload();
    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    await expect(page.locator(`${tag("t1")} .tag-label`)).toHaveText("Keep me");
  });

  test("Collapse > Sections puts the Markers bar away with the Sections bar", async ({ page }) => {
    await openStudio(page);
    await expect(page.locator(".daw-tags-ribbon")).toBeVisible();

    await page.locator('.daw-panel-toggle[data-panel="sections"]').click();
    await expect(page.locator(".daw-section-ribbon")).toBeHidden();
    await expect(page.locator(".daw-tags-ribbon")).toBeHidden();

    await page.locator('.daw-panel-toggle[data-panel="sections"]').click();
    await expect(page.locator(".daw-tags-ribbon")).toBeVisible();
  });

  // ── loop between markers ──
  // The loop button on a marker arms the transport's one loop, so it is read
  // back from the same state a section's loop writes.

  const loopBounds = (page) =>
    page.evaluate(async () => {
      const st = await import("/js/state.js");
      return { on: st.loopEnabled, start: st.loopStart, end: st.loopEnd };
    });

  async function pressLoop(page, id) {
    await page.locator(tag(id)).hover();
    await page.locator(`${tag(id)} .tag-loop`).click();
  }

  const THREE = [
    { id: "t1", name: "One", time: 1 },
    { id: "t2", name: "Two", time: 2.5 },
    { id: "t3", name: "Three", time: 4 },
  ];

  test("a marker's loop button loops it to the next marker, and pressing it again stops", async ({ page }) => {
    await seedTags(page, THREE);
    await openStudio(page);
    const btn = page.locator(`${tag("t1")} .tag-loop`);
    await expect(btn).toHaveAttribute("aria-pressed", "false");
    await expect(btn).toHaveAttribute("aria-label", /next marker/i);

    await pressLoop(page, "t1");
    const looped = await loopBounds(page);
    expect(looped.on).toBe(true);
    expect(looped.start).toBeCloseTo(1, 2);
    expect(looped.end).toBeCloseTo(2.5, 2);
    await expect(btn).toHaveAttribute("aria-pressed", "true");

    await pressLoop(page, "t1");
    expect((await loopBounds(page)).on).toBe(false);
    await expect(btn).toHaveAttribute("aria-pressed", "false");
  });

  test("looping another marker moves the loop, so only one marker is looping", async ({ page }) => {
    await seedTags(page, THREE);
    await openStudio(page);
    await pressLoop(page, "t1");
    await pressLoop(page, "t2");

    const moved = await loopBounds(page);
    expect(moved.start).toBeCloseTo(2.5, 2);
    expect(moved.end).toBeCloseTo(4, 2);
    await expect(page.locator(".tag-loop.active")).toHaveCount(1);
    await expect(page.locator(`${tag("t2")} .tag-loop`)).toHaveAttribute("aria-pressed", "true");
  });

  test("the last marker loops to the end of the song", async ({ page }) => {
    await seedTags(page, THREE);
    await openStudio(page);
    await expect(page.locator(`${tag("t3")} .tag-loop`)).toHaveAttribute("aria-label", /end of the song/i);

    await pressLoop(page, "t3");
    const looped = await loopBounds(page);
    expect(looped.on).toBe(true);
    expect(looped.start).toBeCloseTo(4, 2);
    expect(looped.end).toBeGreaterThan(5.5);
  });

  test("a section's loop and a marker's loop never run together", async ({ page }) => {
    await seedSections(page, [{ id: "s1", name: "Chorus", start: 0.5, end: 2, color: "#4a7fff" }]);
    await seedTags(page, THREE);
    await openStudio(page);

    await pressLoop(page, "t1");
    await expect(page.locator(".tag-loop.active")).toHaveCount(1);

    await page.locator('.section-block[data-id="s1"]').click();
    const section = await loopBounds(page);
    expect(section.start).toBeCloseTo(0.5, 2);
    expect(section.end).toBeCloseTo(2, 2);
    await expect(page.locator(".tag-loop.active")).toHaveCount(0);

    await pressLoop(page, "t2");
    const marker = await loopBounds(page);
    expect(marker.start).toBeCloseTo(2.5, 2);
    await expect(page.locator(".tag-loop.active")).toHaveCount(1);
  });

  test("the Loop button switches a marker's loop off and the marker shows it", async ({ page }) => {
    await seedTags(page, THREE);
    await openStudio(page);
    await pressLoop(page, "t1");

    await page.locator("#t-loop").click();
    expect((await loopBounds(page)).on).toBe(false);
    await expect(page.locator(".tag-loop.active")).toHaveCount(0);
  });

  test("adding a marker inside the looped span shortens the loop to it", async ({ page }) => {
    await seedTags(page, THREE);
    await openStudio(page);
    await pressLoop(page, "t1");

    // Put the playhead at 1.75 s, inside the looped span, and add there.
    await page.evaluate(async () => {
      const { setPlayheadTime } = await import("/js/transport.js");
      setPlayheadTime(1.75);
    });
    await expect.poll(() => playhead(page)).toBeCloseTo(1.75, 2);
    await page.locator("#tagsAddBtn").click();
    const input = page.locator(".tag-rename-input");
    await input.press("Enter");

    await expect.poll(async () => (await loopBounds(page)).end).toBeCloseTo(1.75, 2);
    const after = await loopBounds(page);
    expect(after.on).toBe(true);
    expect(after.start).toBeCloseTo(1, 2);
  });

  test("deleting the looping marker stops the loop, and deleting the next one extends it", async ({ page }) => {
    await seedTags(page, THREE);
    await openStudio(page);

    // The marker after the looping one goes: the loop runs on to the one after.
    await pressLoop(page, "t1");
    await page.locator(tag("t2")).hover();
    await page.locator(`${tag("t2")} .tag-del`).click();
    await expect.poll(async () => (await loopBounds(page)).end).toBeCloseTo(4, 2);
    expect((await loopBounds(page)).on).toBe(true);

    // The looping marker itself goes: nothing is left to loop.
    await page.locator(tag("t1")).hover();
    await page.locator(`${tag("t1")} .tag-del`).click();
    await expect.poll(async () => (await loopBounds(page)).on).toBe(false);
  });

  test("the loop button is reachable with the keyboard", async ({ page }) => {
    await seedTags(page, THREE);
    await openStudio(page);

    await page.locator(`${tag("t2")} .tag-loop`).focus();
    await expect(page.locator(`${tag("t2")} .tag-loop`)).toBeVisible();
    await page.keyboard.press("Enter");
    const looped = await loopBounds(page);
    expect(looped.on).toBe(true);
    expect(looped.start).toBeCloseTo(2.5, 2);
    await expect(page.locator(`${tag("t2")} .tag-loop`)).toHaveAttribute("aria-pressed", "true");
  });

  test("a marker with no room to loop says so and arms nothing", async ({ page }) => {
    await seedTags(page, [
      { id: "t1", name: "One", time: 1 },
      { id: "t2", name: "Close", time: 1.1 },
    ]);
    await openStudio(page);

    const btn = page.locator(`${tag("t1")} .tag-loop`);
    await expect(btn).toHaveAttribute("aria-disabled", "true");
    await page.locator(tag("t1")).hover();
    await btn.click();
    expect((await loopBounds(page)).on).toBe(false);
  });

  test("a marker loop is put away when another song opens", async ({ page }) => {
    await seedTags(page, THREE);
    await openStudio(page);
    await pressLoop(page, "t1");
    expect((await loopBounds(page)).on).toBe(true);

    await page.locator(`.cat-item[data-id="${SIBLING_JOB_ID}"]`).first().click();
    await expect.poll(async () => (await loopBounds(page)).on).toBe(false);
  });
});
