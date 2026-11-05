// Help mode: Guide in the rail, below Trash, makes the studio explain itself. Every
// control it covers shows an outline, pointing at one shows a box with its
// name and what it does, and a click while help mode is on explains instead of
// acting.

import { test, expect } from "@playwright/test";

import { JOB_ID, chooseMode, openStudio, seedLibrary, stubAudioTags, stubUpdateCheck } from "./helpers.mjs";

const tip = (page) => page.locator("#helpTip");

async function helpOn(page) {
  await page.locator("#helpModeBtn").click();
  await expect(page.locator("body")).toHaveClass(/help-mode/);
  await expect(page.locator("#helpModeBtn")).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".help-pill")).toBeVisible();
}

/**
 * Every control on screen that pointing at explains nothing, or explains it
 * with no title or a raw key, by tag, class and label. Each one is pointed at
 * as the mouse would (help mode's own pointerover), so the answer is the HELP
 * table's, first match and all.
 */
function unexplained(page) {
  return page.evaluate(() => {
    const CONTROLS = 'button, input:not([type=hidden]), select, textarea, a[href], label, [role=button], [role=switch], [role=slider], [role=tab], [role=option], [tabindex="0"]';
    const tipEl = document.getElementById("helpTip");
    const out = [];
    for (const el of document.querySelectorAll(CONTROLS)) {
      if (el.closest("#helpTip, .help-pill, [hidden], .hidden")) continue;
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height || getComputedStyle(el).visibility === "hidden") continue;
      el.dispatchEvent(new PointerEvent("pointerover", { bubbles: true }));
      const shown = !tipEl.hidden && document.querySelector(".help-current")?.contains(el);
      if (shown && tipEl.querySelector("b").textContent.trim() && !/help\.|\{name\}/.test(tipEl.textContent)) continue;
      const cls = typeof el.className === "string" ? el.className.trim().replace(/\s+/g, ".") : "";
      const label = (el.getAttribute("aria-label") || el.title || el.textContent || "").trim().slice(0, 40);
      out.push(`${el.tagName.toLowerCase()}${cls ? `.${cls}` : ""} "${label}"`);
    }
    return out;
  });
}

test.describe("help mode", () => {
  test("Guide turns it on, and Esc or Guide turns it off", async ({ page }) => {
    await openStudio(page);
    await helpOn(page);
    await page.keyboard.press("Escape");
    await expect(page.locator("body")).not.toHaveClass(/help-mode/);
    await helpOn(page);
    await page.locator("#helpModeBtn").click();
    await expect(page.locator("body")).not.toHaveClass(/help-mode/);
    await expect(page.locator(".help-target")).toHaveCount(0);
    await expect(page.locator(".help-pill")).toBeHidden();
  });

  test("turning Guide on makes every explainable control glow once, then fade", async ({ page }) => {
    await openStudio(page);
    await page.locator("#helpModeBtn").click();
    const rings = page.locator(".help-intro-ring");
    expect(await rings.count()).toBeGreaterThan(20);
    await expect(page.locator(".help-intro")).toBeVisible();
    // Gone by itself, so the screen is calm again.
    await expect(rings).toHaveCount(0, { timeout: 4000 });
    await expect(page.locator(".help-intro")).toBeHidden();

    // Leaving Guide mid-glow clears it at once.
    await page.keyboard.press("Escape");
    await page.locator("#helpModeBtn").click();
    await page.keyboard.press("Escape");
    await expect(rings).toHaveCount(0);
  });

  test("pointing at a control explains it, under its own label", async ({ page }) => {
    await openStudio(page);
    await helpOn(page);
    await page.locator("#t-speed-075").hover();
    await expect(tip(page)).toBeVisible();
    await expect(tip(page).locator("b")).toHaveText("0.75x");
    await expect(tip(page)).toContainText("three-quarter speed");
    await expect(page.locator("#t-speed-075")).toHaveClass(/help-current/);
  });

  test("a lane's control names its instrument", async ({ page }) => {
    await openStudio(page);
    await helpOn(page);
    const drumsMute = page.locator(".mx-row, .lane-header").filter({ hasText: "Drums" }).locator(".mx-btn.mute").first();
    await drumsMute.hover();
    await expect(tip(page)).toContainText("Silences Drums");
    // Headed by what the button does, not by its one letter.
    await expect(tip(page).locator("b")).toHaveText("Mute Drums");

    // The number: its value wrapper is display: contents and has no box.
    const drumsKey = page.locator(".lane-key").filter({ has: page.locator('[aria-label*="Drums"]') }).locator(".lane-key-num").first();
    await drumsKey.hover({ force: true });
    await expect(tip(page).locator("b")).toHaveText("Key of Drums");
  });

  test("each song fact and presence card explains itself", async ({ page }) => {
    await openStudio(page);
    await helpOn(page);
    await page.locator('.daw-meta-card[data-meta="lufs"]').hover();
    await expect(tip(page)).toContainText("streaming services");
    await page.locator('.daw-meta-card[data-meta="stability"]').hover();
    await expect(tip(page)).toContainText("How steady the tempo is");
    await page.locator('.stem-presence-panel .stem-card[data-stem="drums"]').hover();
    await expect(tip(page)).toContainText("How loud the Drums part is");
  });

  test("with no song open, controls the studio switches off still explain themselves", async ({ page }) => {
    await seedLibrary(page);
    await stubUpdateCheck(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.locator(".cat-item").first().waitFor();
    await helpOn(page);
    for (const sel of ["#t-export-btn", "#np-details-btn", '.daw-meta-card[data-meta="key"]', '.stem-presence-panel .stem-card[data-stem="vocals"]']) {
      const box = await page.locator(sel).first().boundingBox();
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await expect(tip(page), sel).toBeVisible();
      await expect(page.locator(sel).first(), sel).toHaveClass(/help-current/);
    }
  });

  test("Karaoke and Performance explain themselves in the Mode menu", async ({ page }) => {
    await openStudio(page);
    await helpOn(page);
    // The Mode button still opens its menu while Guide is on: the two modes
    // are reached only there.
    await page.locator("#modeBtn").click();
    await expect(page.locator("#modeMenu")).toBeVisible();
    await page.locator('#modeMenu .mode-item[data-mode="karaoke"]').hover();
    await expect(tip(page)).toContainText("Karaoke Mode");
    await page.locator('#modeMenu .mode-item[data-mode="streamer"]').hover();
    await expect(tip(page)).toContainText("Performance Mode");
    // Choosing is explained, not done.
    await page.locator('#modeMenu .mode-item[data-mode="streamer"]').click();
    await expect(page.locator("#streamer")).toBeHidden();
    await expect(page.locator(".rail-karaoke, .rail-streamer")).toHaveCount(0);
  });

  test("Practice and Advanced explain themselves in the Mode menu", async ({ page }) => {
    await openStudio(page);
    await helpOn(page);
    await page.locator("#modeBtn").click();
    await page.locator('#modeMenu .mode-item[data-mode="practice"]').hover();
    await expect(tip(page)).toContainText("The simple studio");
    await page.locator('#modeMenu .mode-item[data-mode="daw"]').hover();
    await expect(tip(page)).toContainText("Everything in Practice");
  });

  test("the Stems popover and the Export menu open in Guide, and their parts explain themselves", async ({ page }) => {
    await openStudio(page);
    await helpOn(page);
    await page.locator("#laneStemsBtn").click();
    await expect(page.locator("#laneStemsPop")).toBeVisible();
    await page.locator("#laneStemsPop .stems-pop-row").first().hover();
    await expect(tip(page)).toContainText("gets its own lane");
    // Apply names the one rule it refuses on (#793).
    await page.locator("#laneStemsPop .stems-pop-apply").hover();
    await expect(tip(page)).toContainText("At least one stem must be ticked.");
    await page.locator("#laneStemsBtn").click();

    await page.locator("#t-export-btn").click();
    await expect(page.locator("#t-export-panel")).toBeVisible();
    await page.locator("#t-fmt-mp3").hover();
    await expect(tip(page)).toContainText("The file type of the export");
    // Choosing in the menu is explained, not done.
    await page.locator("#t-fmt-mp3").click();
    await expect(page.locator("#t-fmt-mp3")).toHaveAttribute("aria-checked", "false");
  });

  test("a click explains instead of acting", async ({ page }) => {
    await openStudio(page);
    await helpOn(page);
    const fav = page.locator("#fav-btn");
    await expect(fav).toHaveAttribute("aria-pressed", "false");
    await fav.click();
    await expect(tip(page)).toContainText("Favorites");
    await expect(fav).toHaveAttribute("aria-pressed", "false");

    // Off again, the same click works.
    await page.keyboard.press("Escape");
    await fav.click();
    await expect(fav).toHaveAttribute("aria-pressed", "true");
  });

  test("Settings lists every tab and its options", async ({ page }) => {
    await openStudio(page);
    await helpOn(page);
    await page.locator("#settingsBtn").hover();
    for (const text of ["General", "Library", "Song details", "Network", "Export", "Recording", "Logs", "Registry", "Port", "Compute device", "Make StemDeck available on your network", "Video encoder", "Microphone"]) {
      await expect(tip(page)).toContainText(text);
    }
  });

  test("every outlined control has a title and a real explanation", async ({ page }) => {
    await openStudio(page);
    await helpOn(page);
    const targets = page.locator(".help-target");
    const count = await targets.count();
    expect(count).toBeGreaterThan(40);
    const bad = [];
    for (let i = 0; i < count; i++) {
      const el = targets.nth(i);
      if (!(await el.isVisible())) continue;
      // Hover the centre of the box, which for the small lane steppers is the
      // only point not shared with a neighbour.
      await el.hover({ force: true });
      const title = (await tip(page).locator("b").textContent())?.trim();
      const text = (await tip(page).textContent())?.trim();
      if (!title || !text || /help\.|\{name\}/.test(text) || text === title) bad.push({ i, title, text });
    }
    expect(bad).toEqual([]);
  });
});

// Every control a person can see explains itself, in the views and states the
// first pass above never opens. The song has sections, so the controls that
// show only with sections are on screen too.
test.describe("help mode leaves no control unexplained", () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1800, height: 1020 });
    const sections = [
      { id: "s1", name: "Verse", start: 0, end: 3, color: "#4a7fff" },
      { id: "s2", name: "Chorus", start: 3, end: 6, color: "#e0a030" },
    ];
    const res = await page.request.patch(`/api/jobs/${JOB_ID}/sections`, { data: { sections } });
    expect(res.ok(), "seeding sections").toBe(true);
  });

  test.afterEach(async ({ page }) => {
    await page.request.patch(`/api/jobs/${JOB_ID}/sections`, { data: { sections: [] } });
  });

  test("in the studio, in Advanced and in Practice", async ({ page }) => {
    // openStudio opens Advanced (seedLibrary stores it).
    await openStudio(page);
    await expect(page.locator("html")).toHaveClass(/mode-daw/);
    await helpOn(page);
    expect(await unexplained(page)).toEqual([]);
    await page.keyboard.press("Escape");
    await chooseMode(page, "practice");
    await expect(page.locator("html")).toHaveClass(/mode-practice/);
    await helpOn(page);
    expect(await unexplained(page)).toEqual([]);
  });

  test("in the library, with no song open", async ({ page }) => {
    await seedLibrary(page);
    await stubUpdateCheck(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.locator(".cat-item").first().waitFor();
    await helpOn(page);
    expect(await unexplained(page)).toEqual([]);
  });

  test("in Performance Mode, wide and narrow", async ({ page }) => {
    await openStudio(page);
    await chooseMode(page, "streamer");
    await expect(page.locator("#streamer")).toBeVisible();
    await helpOn(page);
    expect(await unexplained(page)).toEqual([]);
    // Inside the live window, which has its own entry: Allow has its own.
    await page.locator("#streamer .sm-cam-act").hover();
    await expect(tip(page)).toContainText("Asks for permission");
    // A section's marker on the timeline, headed by its section's name.
    await page.locator("#streamer .sm-marker").last().hover();
    await expect(tip(page).locator("b")).toHaveText("Chorus");
    await expect(tip(page)).toContainText("move the playhead there");
    // Narrow, the output panel folds into a drawer behind its own button.
    await page.keyboard.press("Escape");
    await page.setViewportSize({ width: 1100, height: 900 });
    await expect(page.locator("#streamer .sm-output-btn")).toBeVisible();
    await helpOn(page);
    expect(await unexplained(page)).toEqual([]);
  });

  test("in Karaoke Mode", async ({ page }) => {
    await openStudio(page);
    await chooseMode(page, "karaoke");
    await expect(page.locator("#karaoke .kk-stem").first()).toBeVisible();
    await helpOn(page);
    expect(await unexplained(page)).toEqual([]);
  });

  test("in the lyrics Align and Sync lines tools", async ({ page }) => {
    const synced = "[00:00.00]\n[00:00.50] Line one\n[00:02.00] Line two\n[00:04.00] Line three";
    // Nothing here reaches the network: the lyrics are answered, the
    // lookups refused.
    await page.route(/^https:\/\/(lrclib\.net|[a-z.-]*wiki(data|pedia)\.org)\//, (route) => route.abort("internetdisconnected"));
    await stubAudioTags(page, null);
    await page.route(`**/api/jobs/${JOB_ID}/lyrics`, (route) => route.fulfill({ json: {
      v: 1, source: "lrclib", track: "Fixture", artist: "Band", album: "", duration: 6, synced,
      plain: "Line one\nLine two\nLine three", instrumental: false, lrclib_id: 201, timing: "exact", others: [],
    } }));
    await openStudio(page);
    await page.locator(".rail-lyrics").click();
    await page.locator("#lyricsPanel .lyrics-align-toggle").click();
    await expect(page.locator("#lyricsAlign")).toBeVisible();
    await helpOn(page);
    expect(await unexplained(page)).toEqual([]);
    await page.keyboard.press("Escape");
    await page.locator("#lyricsPanel .lyrics-sync-toggle").click();
    await expect(page.locator("#lyricsSync")).toBeVisible();
    await page.locator("#lyricsPanel .lyrics-line").nth(1).click();
    await expect(page.locator("#lyricsPanel .lyrics-line-tools")).toBeVisible();
    await helpOn(page);
    expect(await unexplained(page)).toEqual([]);
  });

  test("in search results and a preview", async ({ page }) => {
    await page.route("**/api/search", (route) => route.fulfill({ json: { max_duration_sec: 1200, items: [
      { title: "Result one", url: "https://www.youtube.com/watch?v=aaaaaaaaaaa", duration: 200, uploader: "Channel" },
      { title: "Result two", url: "https://www.youtube.com/watch?v=bbbbbbbbbbb", duration: 9000, uploader: "Channel", too_long: true },
    ] } }));
    await page.route("**/api/search/preview**", (route) => route.fulfill({ status: 200, contentType: "audio/mpeg", body: Buffer.alloc(0) }));
    await openStudio(page);
    await page.locator("#url").fill("some song ");
    await page.locator("#search-panel .search-preview-btn").first().click();
    await expect(page.locator("#search-panel .search-player")).toBeVisible();
    await helpOn(page);
    // Clicking Guide closed the panel; typing with Guide on opens it again.
    await page.locator("#url").focus();
    await page.keyboard.type("x");
    await expect(page.locator("#search-panel .search-row").first()).toBeVisible();
    await page.locator("#search-panel .search-row").first().hover();
    await expect(tip(page)).toContainText("puts its link in the search box");
    expect(await unexplained(page)).toEqual([]);
  });

  test("in the paused queue, a failure notice and the error box", async ({ page }) => {
    const queued = ["a0a0a0a0a001", "a0a0a0a0a002", "a0a0a0a0a003"].map((job_id, i) => ({ job_id, title: `Waiting ${i + 1}`, status: "queued", progress: 0 }));
    const running = { job_id: "a0a0a0a0a000", title: "Splitting", status: "separating", stage: "Separating 40%", progress: 0.4 };
    await page.route("**/api/queue", (route) => route.fulfill({ json: {
      running, queued, paused: true,
      max_pending_uploads: 5, max_pending_urls: 50, capacity_left_uploads: 5, capacity_left_urls: 50,
    } }));
    await page.route("**/api/queue/events", (route) => route.abort());
    await openStudio(page);
    await page.evaluate(async () => {
      await (await import("/js/queue.js")).refreshQueue();
      (await import("/js/notifications.js")).notifyFailure({ kind: "export", message: "Export failed", context: {} });
      (await import("/js/job.js")).showError("Could not import", "detail");
    });
    await page.locator(".rail-queue").click();
    await expect(page.locator("#catalogList .queue-start-btn")).toBeVisible();
    await helpOn(page);
    // Three rows in one list, each with its own sentence (first match wins).
    await page.locator("#catalogList .queue-row.queue-running").hover();
    await expect(tip(page)).toContainText("being split now");
    await page.locator("#catalogList .queue-row.queue-waiting").first().hover();
    await expect(tip(page)).toContainText("Drag it up or down");
    await page.locator("#catalogList .queue-row .queue-top").first().hover();
    await expect(tip(page)).toContainText("front of the queue");
    await page.locator("#notifBtn").click();
    await expect(page.locator("#notifList .daw-notif-error")).toBeVisible();
    expect(await unexplained(page)).toEqual([]);
  });

  test("on the sections bar, Hide names the way back", async ({ page }) => {
    await openStudio(page);
    await helpOn(page);
    await page.locator("#sectionsHideBtn").hover();
    await expect(tip(page).locator("b")).toHaveText("Hide the sections bar");
    await expect(tip(page)).toContainText("Collapse > Sections brings it back");
  });
});
