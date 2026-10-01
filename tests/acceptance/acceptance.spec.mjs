// The manual test run for PR #713, automated against the real app and the
// real internet. One test per check, titled with the check's id, so a result
// here maps onto the manual checklist 1:1. The steps and expected results are
// quoted above each test; the report is written by report.mjs.
//
// The tests run in the order they can: the checks that need nothing imported
// first (including V3, which needs no track open), then every import is
// queued at once and each check waits for its own, and the checks that need
// an idle queue (the error card, cancel) and the offline relaunch come last.
//
// What a program cannot judge (does the wipe feel in time with the singing,
// does a word look broken) is annotated as "partly", with what is left for a
// person, rather than passed on a guess.

import fs from "node:fs";
import path from "node:path";
import { test as base, expect, chromium } from "@playwright/test";
import { title } from "./checks.mjs";
import {
  DATA_DIR,
  JOBS_DIR,
  PACKAGE_DIR,
  connect,
  launchApp,
  osDrag,
  readState,
  stopRecordedApp,
  writeState,
} from "./harness.mjs";
import {
  ACOUSTID_KEY,
  SKIP_LONG,
  SONGS,
  aboutOutline,
  api,
  closeAbout,
  clockSeconds,
  closeSettings,
  importFile,
  importLink,
  job,
  lrcLines,
  lyricsState,
  makeAudio,
  note,
  openAbout,
  openLyrics,
  openSettings,
  openTrack,
  partly,
  playAndWatch,
  queueImports,
  readLogs,
  seconds,
  serviceDown,
  serviceEvidence,
  setWindowWidth,
  shot,
  showLibrary,
  songJob,
  spyOnInvoke,
  tidy,
  voiceOnsets,
  waitForJob,
  watchNetwork,
  wideWindow,
} from "./helpers.mjs";
import { fingerprint, realMirrorPath } from "./global-setup.mjs";

// The app is started once by global-setup.mjs; each worker attaches to it. A
// worker restarted after a failure attaches again to the same app, so a
// failed check costs the run nothing but itself.
const test = base.extend({
  app: [
    // eslint-disable-next-line no-empty-pattern
    async ({}, use) => {
      const { browser, page, baseURL } = await connect(chromium, { timeout: 5 * 60_000 });
      watchNetwork(page);
      await use({ browser, page, baseURL });
      // Over CDP this only disconnects; the app stays up for the next worker.
      await browser.close().catch(() => {});
    },
    { scope: "worker", timeout: 10 * 60_000 },
  ],
});

const desktop = () => readState()?.mode === "desktop";
const BROKEN_LINK = "https://www.youtube.com/watch?v=xxxxxxxxxxx";

// The app's own translation tables, so an expected label is always the one
// the build ships.
const tr = (page, lang, key) =>
  page.evaluate(async ([l, k]) => (await import("/js/i18n.js")).TRANSLATIONS[l][k], [lang, key]);

let checkStartedAt = Date.now();

test.beforeEach(async ({ app }) => {
  checkStartedAt = Date.now();
  await tidy(app.page);
});

test.afterEach(async ({ app }, testInfo) => {
  if (testInfo.status === testInfo.expectedStatus) return;
  await shot(app.page, testInfo, "failure").catch(() => {});
  const message = testInfo.error?.message || "";
  // E4's refused lookup is a request to the app's own server, which this does
  // not count as an outside service.
  const evidence = serviceEvidence(app.page, checkStartedAt);
  // An outage only explains a failure it could have caused: an import that
  // YouTube refused, or a request from the page itself that failed. A service
  // in the backend log alone is noted, not blamed.
  const pageFailed = evidence.filter((e) => e.startsWith("page:"));
  if (/Service unavailable/.test(message) || /ServiceError/.test(testInfo.error?.stack || "")) {
    serviceDown(testInfo, [message.split("\n")[0], ...evidence].join(" | "));
  } else if (pageFailed.length) {
    serviceDown(testInfo, evidence.join(" | "));
  } else if (evidence.length) {
    note(testInfo, `Outside services during this check: ${evidence.join(" | ")}`);
  }
});

// A job that failed because YouTube refused it reads as an outage, not a bug.
async function trackFor(page, key) {
  try {
    const id = await songJob(page, key);
    const retried = readState().retried?.[key];
    if (retried) note(test.info(), `The first import of ${SONGS[key].label} failed and was split again: ${retried}`);
    return id;
  } catch (err) {
    if (err.constructor?.name === "ServiceError") throw new Error(`Service unavailable: ${err.message}`);
    throw err;
  }
}

async function waitForIdleQueue(timeout = 90 * 60_000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const q = await api("/api/queue").catch(() => null);
    if (q && !q.running && !(q.queued || []).length) return;
    await new Promise((r) => setTimeout(r, 3000));
  }
  throw new Error("The import queue never emptied.");
}

// ─── Setup ──────────────────────────────────────────────────────────────────

// Steps: start the build with LOCALAPPDATA pointed at an empty folder; wait
// for first-run downloads. Expect: the studio opens; later launches are quick.
test(title("S1"), async ({ app }, testInfo) => {
  const { page } = app;
  test.skip(!desktop(), "Server target: there is no desktop build to start.");
  await wideWindow(page);
  await expect(page.locator("#url")).toBeVisible();
  await expect(page.locator("#submit")).toBeVisible();
  const state = readState();
  // The backend is the package's own python, not a checkout's or an installed app's.
  const pythons = (state.tree || []).filter((p) => /python/i.test(p.exe || ""));
  expect(pythons.length, "the app started a backend").toBeGreaterThan(0);
  for (const p of pythons) expect(p.exe.toLowerCase().startsWith(PACKAGE_DIR.toLowerCase())).toBe(true);
  const health = await api("/api/health");
  note(testInfo, `Studio reached ${state.startupSec} s after launch${state.startupSec > 120 ? " (a first run, with downloads)" : ""}; separation device ${health.demucs_device}.`);
  if (/nvidia/i.test(PACKAGE_DIR) && health.demucs_device !== "cuda") {
    note(testInfo, "The NVIDIA build is separating on the CPU.");
  }
  await shot(page, testInfo, "studio");
});

// Steps: click Help. Expect: the About box shows v0.18.2.dev0; no update banner.
test(title("S2"), async ({ app }, testInfo) => {
  const { page } = app;
  const built = JSON.parse(fs.readFileSync(path.join(PACKAGE_DIR, "backend", "static", "version.json"), "utf8")).version;
  const expected = `v${process.env.STEMDECK_ACCEPTANCE_VERSION || built}`;
  writeState({ ...readState(), version: expected });
  await page.locator("#aboutBtn").click();
  await expect(page.locator("#aboutDialog")).toBeVisible();
  await expect(page.locator("#aboutVersion")).toHaveText(expected);
  await shot(page, testInfo, "about");
  await page.locator("#aboutClose").click();
  await expect(page.locator("#aboutDialog")).toBeHidden();

  // The update check runs at startup and must skip a dev build: no card in
  // Notifications offering another version, and no lit badge for one.
  const card = page.locator("#notifReleaseCard");
  const offered = await card.evaluate((el) => (el.classList.contains("hidden") ? "" : document.getElementById("notifReleaseDesc")?.textContent.trim() || "a version"));
  if (offered) {
    await page.locator("#notifBtn").click();
    await shot(page, testInfo, "update-offered");
    await page.locator(".daw-notif-close").click().catch(() => {});
  }
  expect(offered, `Notifications offers an update to ${offered} on the ${expected} build`).toBe("");
  await expect(page.locator("#notifBadge")).toHaveClass(/\bhidden\b/);
});

// Steps: look at the Library; open the package's data folder. Expect: the
// Library is empty; the data folder holds a settings.json and a jobs folder.
test(title("S3"), async ({ app }, testInfo) => {
  const { page } = app;
  await showLibrary(page);
  await page.waitForTimeout(1500);
  await expect(page.locator("#catalogList .cat-item")).toHaveCount(0);
  const where = await api("/api/settings/stems-location");
  await shot(page, testInfo, "empty-library");
  if (!desktop()) {
    note(testInfo, `Server target: stems are at ${where.path}; the data folder check applies to the desktop build.`);
    return;
  }
  expect(fs.existsSync(path.join(DATA_DIR, "settings.json")), "data\\settings.json").toBe(true);
  expect(fs.statSync(JOBS_DIR).isDirectory(), "data\\jobs").toBe(true);
  expect(path.resolve(where.path).toLowerCase()).toBe(path.resolve(JOBS_DIR).toLowerCase());
  // The installed app's settings copy is exactly as it was before the run.
  const before = readState().realMirror;
  expect(JSON.stringify(fingerprint(realMirrorPath())), `${realMirrorPath()} unchanged`).toBe(JSON.stringify(before));
  note(testInfo, `Stems at ${where.path}; ${realMirrorPath()} untouched.`);
});

// ─── Needs no track: V3 ─────────────────────────────────────────────────────

// Steps: with no track open, click Click track options. Expect: the box is
// fully opaque and takes its own clicks; only the controls inside are dimmed.
test(title("V3"), async ({ app }, testInfo) => {
  const { page } = app;
  await expect(page.locator(".app")).toHaveClass(/no-track/);
  // The default window (1280 wide): the options fold behind the disclosure.
  await setWindowWidth(page, 1296, 839);
  const more = page.locator("#t-metro-more");
  await expect(more).toBeVisible({ timeout: 10_000 });
  await more.click();
  const panel = page.locator("#t-metro-panel");
  await expect(panel).toHaveClass(/\bunavailable\b/);
  await expect(panel).toBeVisible();
  const geo = await page.evaluate(() => {
    const p = document.querySelector("#t-metro-panel");
    const pr = p.getBoundingClientRect();
    const hit = document.elementFromPoint(pr.left + pr.width / 2, pr.top + 4);
    const child = p.firstElementChild;
    return {
      opacity: getComputedStyle(p).opacity,
      pointerEvents: getComputedStyle(p).pointerEvents,
      background: getComputedStyle(p).backgroundColor,
      childOpacity: child ? getComputedStyle(child).opacity : null,
      hitsPanel: p.contains(hit),
    };
  });
  expect(geo.opacity).toBe("1");
  expect(geo.pointerEvents).not.toBe("none");
  expect(geo.hitsPanel, "a click on the box lands on the box, not the lanes").toBe(true);
  expect(geo.background, "a solid background").not.toMatch(/rgba\(.*,\s*0(\.\d+)?\)$/);
  expect(Number(geo.childOpacity)).toBeLessThan(1);
  await shot(page, testInfo, "options-no-track");
  await panel.click({ position: { x: 6, y: 6 } });
  await expect(more).toHaveAttribute("aria-expanded", "true");
  await page.keyboard.press("Escape");
  await wideWindow(page);
});

// ─── Settings: song identification ──────────────────────────────────────────

// Steps: Settings, Song details, Song identification: paste a key AcoustID refuses,
// Save. Expect: the red refusal text; the key stays in the field; nothing saved.
test(title("A1"), async ({ app }, testInfo) => {
  const { page } = app;
  const dialog = await openSettings(page, "details");
  const input = dialog.locator(".set-acoustid-key");
  await input.scrollIntoViewIfNeeded();
  // Made up. AcoustID answers an unknown key with its code 4.
  const madeUp = "Qa12Ws34Ed";
  await input.fill(madeUp);
  const [res] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith("/api/settings") && r.request().method() === "POST", { timeout: 60_000 }),
    dialog.locator(".set-acoustid-save").click(),
  ]);
  const msg = dialog.locator(".acoustid-key-msg");
  const failed = await tr(page, "en", "settings.acoustid.failed");
  await expect(msg).not.toHaveText("");
  if ((await msg.textContent()).trim() === failed) {
    throw new Error(`Service unavailable: AcoustID could not be asked about the key (HTTP ${res.status()}).`);
  }
  await expect(msg).toHaveText(
    "AcoustID does not accept this key. Use the key of an application you register at acoustid.org, not the user key on your profile.",
  );
  await expect(msg).toHaveClass(/\berror\b/);
  const color = await msg.evaluate((el) => getComputedStyle(el).color);
  const [r, g, b] = color.match(/\d+/g).map(Number);
  expect(r > g && r > b, `the text is red (${color})`).toBe(true);
  await expect(input).toHaveValue(madeUp);
  expect((await api("/api/settings")).acoustid_api_key_set).toBe(false);
  await shot(page, testInfo, "refused");
  await input.fill("");
  await closeSettings(page);
});

// Steps: paste an application key and Save; close Settings and open it again.
// Expect: "A key ending in xx is saved."; the field is empty; the key never shows.
test(title("A2"), async ({ app }, testInfo) => {
  test.skip(!ACOUSTID_KEY, "No STEMDECK_ACCEPTANCE_ACOUSTID_KEY in the environment: this needs a real AcoustID application key.");
  const { page } = app;
  const dialog = await openSettings(page, "details");
  const input = dialog.locator(".set-acoustid-key");
  await input.scrollIntoViewIfNeeded();
  // Masked before a single character goes in.
  expect(await input.getAttribute("type")).toBe("password");
  await input.fill(ACOUSTID_KEY);
  const [res] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith("/api/settings") && r.request().method() === "POST", { timeout: 60_000 }),
    dialog.locator(".set-acoustid-save").click(),
  ]);
  // Read as booleans, so no assertion message can ever print the key.
  const leaked = (await res.text()).includes(ACOUSTID_KEY);
  expect(leaked, "the save response carries the key").toBe(false);
  const tail = ACOUSTID_KEY.slice(-2);
  const msg = dialog.locator(".acoustid-key-msg");
  await expect(msg).toHaveText(`A key ending in ${tail} is saved.`, { timeout: 30_000 });
  expect(await input.evaluate((el) => el.value === ""), "the field is emptied").toBe(true);
  await shot(page, testInfo, "saved");
  await closeSettings(page);

  const again = await openSettings(page, "details");
  await expect(again.locator(".acoustid-key-msg")).toHaveText(`A key ending in ${tail} is saved.`);
  expect(await again.locator(".set-acoustid-key").evaluate((el) => el.value === ""), "the field is empty on reopen").toBe(true);
  const settings = JSON.stringify(await api("/api/settings"));
  expect(settings.includes(ACOUSTID_KEY), "GET /api/settings carries the key").toBe(false);
  await again.locator(".set-acoustid-key").scrollIntoViewIfNeeded();
  await shot(page, testInfo, "reopened");
  await closeSettings(page);
  writeState({ ...readState(), keySavedAt: Date.now() });
});

// Steps: Settings, Song details, Transcribe lyrics. Expect: Auto (NVIDIA GPU only),
// On and Off, on Auto by default.
test(title("A3"), async ({ app }, testInfo) => {
  const { page } = app;
  const dialog = await openSettings(page, "details");
  const select = dialog.locator(".set-transcribe-lyrics");
  await select.scrollIntoViewIfNeeded();
  await expect(select.locator("option")).toHaveText(["Auto (NVIDIA GPU only)", "On", "Off"]);
  await expect(select).toHaveValue("auto");
  await shot(page, testInfo, "transcribe");
  await closeSettings(page);
});

// ─── Now playing and song details ───────────────────────────────────────────

// Steps: import any song and open it. Expect, left to right: the link field,
// the Extract options, Extract stems, then the Now playing card with the
// artwork, the heart on its left and the (i) on its right.
test(title("N1"), async ({ app }, testInfo) => {
  const { page } = app;
  await wideWindow(page);
  await showLibrary(page);
  // Every import the run needs, queued now so they separate while the checks
  // below look at the ones already done.
  const jobs = await queueImports(page);
  note(testInfo, `Queued: ${Object.entries(jobs).map(([k, v]) => `${k}=${v}`).join(", ")}`);
  const id = await trackFor(page, "wicked");
  await openTrack(page, id);
  const box = (sel) => page.locator(sel).first().evaluate((el) => {
    const r = el.getBoundingClientRect();
    return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, visible: r.width > 0 && r.height > 0 };
  });
  const url = await box(".daw-url-zone");
  const extract = await box(".daw-stem-section");
  const split = await box("#submit");
  const card = await box("#nowPlayingPanel");
  for (const [name, b] of Object.entries({ url, extract, split, card })) expect(b.visible, `${name} is on screen`).toBe(true);
  // The link field and the Extract options share the row's first column, the
  // chips under the field; everything else follows them to the right.
  const besideOrUnder = url.right <= extract.left || (Math.abs(extract.left - url.left) < 40 && extract.top >= url.bottom - 2);
  expect(besideOrUnder, "the Extract options follow the link field").toBe(true);
  expect(Math.max(url.right, extract.right) <= split.left, "Extract stems follows them").toBe(true);
  expect(split.right <= card.left, "the card comes after Extract stems").toBe(true);
  // One row: every one of them overlaps the card's height.
  for (const b of [url, extract, split]) expect(b.top < card.bottom && b.bottom > card.top, "one row").toBe(true);
  await expect(page.locator("#nowPlayingPanel .np-legend")).toHaveText("Now playing");
  const heart = await box("#fav-btn");
  const art = await box("#np-art");
  const info = await box("#nowPlayingPanel .track-panel-info");
  const details = await box("#np-details-btn");
  // The words themselves, not their box: the box runs under the (i) and its
  // padding keeps the text out.
  const words = await page.evaluate(() => Math.max(...["#title", "#np-artist"].map((sel) => {
    const el = document.querySelector(sel);
    if (!el || el.hidden) return 0;
    const range = document.createRange();
    range.selectNodeContents(el);
    return range.getBoundingClientRect().right;
  })));
  expect(Math.abs(heart.left - card.left) < 6, "the heart is on the card's left edge").toBe(true);
  expect(heart.right <= art.left + 1, "then the artwork").toBe(true);
  expect(art.right <= info.left + 1, "then the title").toBe(true);
  expect(Math.abs(details.right - card.right) < 6, "the (i) is on the card's right edge").toBe(true);
  expect(words <= details.left + 1, "the text stops before the (i)").toBe(true);
  await shot(page, testInfo, "top-bar", { locator: page.locator(".daw-composer-stack") });
  await shot(page, testInfo, "studio");
});

// Steps: with a song loaded, open Click track options. Expect: under Count-in,
// a caption "Plays even with the click track off".
test(title("V4"), async ({ app }, testInfo) => {
  const { page } = app;
  const id = await trackFor(page, "wicked");
  if (await page.locator(".app").evaluate((el) => el.classList.contains("no-track"))) await openTrack(page, id);
  // The caption belongs to the popover, which the default window width shows.
  await setWindowWidth(page, 1296, 839);
  await page.waitForFunction(() => document.querySelector("#t-metro") && !document.querySelector("#t-metro").disabled, null, { timeout: 60_000 });
  const more = page.locator("#t-metro-more");
  await expect(more).toBeVisible();
  if ((await more.getAttribute("aria-expanded")) !== "true") await more.click();
  const hint = page.locator("#t-metro-panel .metro-countin-hint");
  await expect(hint).toBeVisible();
  await expect(hint).toHaveText("Plays even with the click track off");
  const below = await page.evaluate(() => {
    const s = document.querySelector("#t-metro-countin").getBoundingClientRect();
    const h = document.querySelector("#t-metro-panel .metro-countin-hint").getBoundingClientRect();
    return h.top >= s.bottom - 2;
  });
  expect(below, "the caption sits under Count-in").toBe(true);
  await shot(page, testInfo, "count-in");
  await page.keyboard.press("Escape");
  await wideWindow(page);
});

// Steps: import Wicked, Dancing Through Life; open About this song. Expect:
// the card reads Dancing Through Life and Wicked; the box starts with the
// song, then From the musical (Wicked, synopsis, credits), then Performer; no
// search field.
test(title("N2"), async ({ app }, testInfo) => {
  const { page } = app;
  await wideWindow(page);
  const id = await trackFor(page, "wicked");
  await openTrack(page, id);
  await expect(page.locator("#title")).toHaveText("Dancing Through Life", { timeout: 30_000 });
  await expect(page.locator("#np-artist")).toHaveText("Wicked");
  await openAbout(page);
  const o = await aboutOutline(page);
  note(testInfo, `Box: ${o.order.join(" > ")}; performer ${o.name || "none"}.`);
  expect(o.order[0], "the song first").toBe("artist-song");
  const work = o.order.indexOf("artist-section.artist-work");
  const performer = o.order.indexOf("artist-section.artist-performer");
  expect(work, "a From the musical section").toBeGreaterThan(0);
  expect(performer, "a Performer section after it").toBeGreaterThan(work);
  expect(o.workTitle).toBe("From the musical");
  expect(o.workName).toContain("Wicked");
  expect(o.workSynopsis.length, "a synopsis").toBeGreaterThan(40);
  expect(o.workCredits.length, "credits").toBeGreaterThan(0);
  expect(o.performerTitle).toBe("Performer");
  expect(o.searchShown, "no search field").toBe(false);
  await shot(page, testInfo, "about");
  await closeAbout(page);
});

// Steps: open the Wicked track, Lyrics, play; click a line further down.
// Expect: the words fill in time with the singing; a line click moves the
// playhead to it.
test(title("L1"), async ({ app }, testInfo) => {
  const { page } = app;
  const id = await trackFor(page, "wicked");
  await openTrack(page, id);
  await openLyrics(page);
  const s = await lyricsState(page);
  note(testInfo, `Lyrics: ${s.synced} synced lines, source "${s.meta}".`);
  expect(s.synced, `synced lyrics (status: "${s.status}")`).toBeGreaterThan(8);
  const lines = page.locator("#lyricsBody .lyrics-line");
  const count = await lines.count();
  // Start from a sung line rather than the intro.
  const from = Math.min(4, count - 1);
  await lines.nth(from).click();
  await expect(lines.nth(from)).toHaveClass(/\bcurrent\b/, { timeout: 5000 });
  const samples = await playAndWatch(page, 9000);
  const sung = samples.map((x) => x.sung);
  const times = samples.map((x) => seconds(x.time));
  expect(times[times.length - 1] - times[0], "playback advanced").toBeGreaterThan(5);
  expect(Math.max(...sung), "words were marked as sung").toBeGreaterThan(0);
  expect(samples.some((x) => x.singing > 0), "a word was part-filled while it was sung").toBe(true);
  // The marks moved forward with the music: more sung, or a later line.
  const moved = samples.some((x, i) => i > 0 && (x.sung > samples[i - 1].sung || x.current > samples[i - 1].current));
  expect(moved, "the fill advanced with playback").toBe(true);
  await shot(page, testInfo, "karaoke");

  const before = seconds(await page.locator("#t-time").textContent());
  const target = Math.min(from + 12, count - 1);
  await lines.nth(target).scrollIntoViewIfNeeded();
  await lines.nth(target).click();
  await expect(lines.nth(target)).toHaveClass(/\bcurrent\b/, { timeout: 5000 });
  const after = seconds(await page.locator("#t-time").textContent());
  expect(after, "the playhead moved down the song to the clicked line").toBeGreaterThan(before);
  await shot(page, testInfo, "line-click");
  partly(testInfo, "Whether the fill keeps time with the voice is for ears: the program saw it advance with playback and a line click move the playhead.");
});

// Steps: import This Is Me; open About this song. Expect: From the film, The
// Greatest Showman, then Keala Settle as the performer.
test(title("N3"), async ({ app }, testInfo) => {
  const { page } = app;
  await wideWindow(page);
  const id = await trackFor(page, "showman");
  await openTrack(page, id);
  await openAbout(page);
  const o = await aboutOutline(page);
  note(testInfo, `Box: ${o.order.join(" > ")}; work "${o.workName}"; performer "${o.name}".`);
  expect(o.workTitle).toBe("From the film");
  expect(o.workName).toContain("The Greatest Showman");
  expect(o.performerTitle).toBe("Performer");
  expect(o.name).toContain("Keala Settle");
  expect(o.order.indexOf("artist-section.artist-performer")).toBeGreaterThan(o.order.indexOf("artist-section.artist-work"));
  await shot(page, testInfo, "about");
  await closeAbout(page);
});

// Steps: import The Phantom of the Opera; open About this song; click Spotify
// under the performer. Expect: the musical shows Website and Instagram;
// Michael Crawford shows Website, Spotify and Apple Music; the link opens in
// the web browser, not inside StemDeck.
test(title("N4"), async ({ app }, testInfo) => {
  const { page, browser } = app;
  await wideWindow(page);
  const id = await trackFor(page, "phantom");
  await openTrack(page, id);
  await openAbout(page);
  const o = await aboutOutline(page);
  const performerLinks = await page.locator(".artist-performer .artist-link").evaluateAll((els) => els.map((a) => a.dataset.kind));
  note(testInfo, `Musical links: ${o.workLinks.join(", ") || "none"}; performer "${o.name}": ${performerLinks.join(", ") || "none"}.`);
  // No empty row anywhere: a row is there only when it has a link.
  expect(await page.locator("#artistBody .artist-links:not(:has(li))").count()).toBe(0);
  expect(o.workLinks).toEqual(expect.arrayContaining(["website", "instagram"]));
  expect(o.name).toContain("Michael Crawford");
  expect(performerLinks).toEqual(expect.arrayContaining(["website", "spotify", "appleMusic"]));
  await shot(page, testInfo, "links");

  const spotify = page.locator(".artist-performer .artist-link-spotify");
  const href = await spotify.getAttribute("href");
  const urlBefore = page.url();
  if (!desktop()) {
    const [popup] = await Promise.all([page.waitForEvent("popup", { timeout: 10_000 }), spotify.click()]);
    expect(page.url()).toBe(urlBefore);
    note(testInfo, `Server target: opened in a new browser tab (${popup.url().slice(0, 60)}).`);
    await popup.close();
    partly(testInfo, "Server target: the desktop's open_url path was not exercised.");
    await closeAbout(page);
    return;
  }
  expect(await spyOnInvoke(page), "the page's native calls can be watched").toBe(true);
  const cdp = await browser.newBrowserCDPSession();
  const pagesBefore = (await cdp.send("Target.getTargets")).targetInfos.filter((t) => t.type === "page").length;
  await spotify.click();
  await page.waitForTimeout(2500);
  const calls = await page.evaluate(() => window.__acceptance.calls.filter((c) => c.cmd === "open_url"));
  const pagesAfter = (await cdp.send("Target.getTargets")).targetInfos.filter((t) => t.type === "page").length;
  await cdp.detach();
  expect(page.url(), "the app page did not navigate").toBe(urlBefore);
  expect(pagesAfter, "no new window or page opened in the app").toBe(pagesBefore);
  expect(calls.map((c) => c.args?.url), "handed to the desktop shell's open_url").toEqual([href]);
  if (process.env.STEMDECK_ACCEPTANCE_NO_BROWSER === "1") {
    partly(testInfo, "open_url was held back (STEMDECK_ACCEPTANCE_NO_BROWSER=1), so the browser opening was not seen.");
  } else {
    expect(calls[0].settled, "the shell accepted it").toBe("ok");
    partly(testInfo, "That the page appeared in the default web browser is for eyes: the shell's open_url accepted the link and the app did not navigate or open a window.");
  }
  await expect(page.locator("#artistDialog")).toBeVisible();
  await shot(page, testInfo, "after-click");
  await closeAbout(page);
});

// Steps: import Billie Jean; open About this song. Expect: Michael Jackson
// with a photo, a History section, studio albums and official links; the card
// reads Billie Jean, Michael Jackson.
test(title("N5"), async ({ app }, testInfo) => {
  const { page } = app;
  await wideWindow(page);
  const id = await trackFor(page, "billie");
  await openTrack(page, id);
  await expect(page.locator("#title")).toHaveText("Billie Jean", { timeout: 30_000 });
  await expect(page.locator("#np-artist")).toHaveText("Michael Jackson");
  await openAbout(page);
  const o = await aboutOutline(page);
  note(testInfo, `Sections: ${o.sections.join(", ")}; ${o.albums} albums; links ${o.bandLinks.join(", ")}; photo ${o.photoState}.`);
  expect(o.name).toContain("Michael Jackson");
  expect(o.photo, "a photo").toBe(true);
  expect(o.sections).toContain("History");
  expect(o.history.length).toBeGreaterThan(100);
  expect(o.sections).toContain("Studio albums");
  expect(o.albums).toBeGreaterThan(3);
  expect(o.bandLinks.length, "official links").toBeGreaterThan(0);
  await shot(page, testInfo, "about");
  await closeAbout(page);
});

// Steps: with the key from A2 saved, import a song; Settings, Logs, Backend
// log. Expect: no "AcoustID lookup failed" or "refused" line; details right.
test(title("N6"), async ({ app }, testInfo) => {
  test.skip(!ACOUSTID_KEY, "No STEMDECK_ACCEPTANCE_ACOUSTID_KEY in the environment: needs the key A2 saves.");
  const { page } = app;
  const state = readState();
  test.skip(!state.keySavedAt, "A2 did not save a key.");
  const id = await trackFor(page, "billie");
  const j = await job(id);
  note(testInfo, `Billie Jean identified by ${j.identity?.source || "nothing"} as "${j.identity?.title}" by ${j.identity?.artist}.`);
  const lines = readLogs().split(/\r?\n/).filter((l) => l.includes(id) && /acoustid/i.test(l));
  expect(lines.filter((l) => /AcoustID lookup failed|refused/i.test(l)), "no failed or refused AcoustID line for the import").toEqual([]);
  expect(j.identity?.source, "identified by the fingerprint").toBe("acoustid");
  expect(j.identity?.title).toMatch(/Billie Jean/i);
  const logsShowKey = readLogs().includes(ACOUSTID_KEY);
  expect(logsShowKey, "a log carries the key").toBe(false);

  const dialog = await openSettings(page, "logs");
  await dialog.locator('.settings-subtab[data-sub="backend"]').click();
  const view = dialog.locator('.settings-logtail-view[data-view="backend"]');
  await expect(view).toBeVisible();
  await expect.poll(() => view.inputValue(), { timeout: 15_000 }).not.toBe("Loading…");
  const shown = await view.inputValue();
  expect(shown.includes(ACOUSTID_KEY), "the Backend log view carries the key").toBe(false);
  expect(/AcoustID lookup failed|refused/i.test(shown.split("\n").filter((l) => l.includes(id)).join("\n"))).toBe(false);
  await shot(page, testInfo, "backend-log");
  await closeSettings(page);
});

// ─── Other languages ────────────────────────────────────────────────────────

// Steps: import 周杰倫 晴天; About this song; Lyrics and play. Expect: Jay Chou
// with 周杰倫 beside the name, a history and albums; lyrics fill one character
// at a time.
test(title("C1"), async ({ app }, testInfo) => {
  const { page } = app;
  await wideWindow(page);
  const id = await trackFor(page, "jay");
  await openTrack(page, id);
  await openAbout(page);
  const o = await aboutOutline(page);
  note(testInfo, `Box: "${o.name}", sections ${o.sections.join(", ")}.`);
  expect(o.name).toContain("Jay Chou");
  expect(o.native).toBe("周杰倫");
  expect(o.sections).toContain("History");
  expect(o.albums).toBeGreaterThan(0);
  await shot(page, testInfo, "about");
  await closeAbout(page);

  await openLyrics(page);
  const s = await lyricsState(page);
  expect(s.synced, `synced lyrics (status: "${s.status}")`).toBeGreaterThan(5);
  // Han lines are cut into one span per character, each its own wipe.
  const han = await page.evaluate(() => {
    const isHan = /\p{Script=Han}/u;
    const rows = [...document.querySelectorAll("#lyricsBody .lyrics-line")].filter((l) => isHan.test(l.textContent));
    const spans = rows.flatMap((l) => [...l.querySelectorAll(".lw")]).filter((w) => isHan.test(w.textContent));
    return { rows: rows.length, spans: spans.length, single: spans.filter((w) => [...w.textContent.trim()].length === 1).length, first: rows.findIndex(() => true) };
  });
  expect(han.rows, "lines in Chinese").toBeGreaterThan(0);
  expect(han.single / han.spans, "one character per span").toBeGreaterThan(0.95);
  const lines = page.locator("#lyricsBody .lyrics-line");
  const hanIndex = await lines.evaluateAll((els) => els.findIndex((l) => /\p{Script=Han}/u.test(l.textContent)));
  await lines.nth(Math.min(hanIndex + 2, (await lines.count()) - 1)).click();
  const samples = await playAndWatch(page, 7000);
  expect(samples.some((x, i) => i > 0 && x.sung > samples[i - 1].sung), "characters filled one after another").toBe(true);
  await shot(page, testInfo, "karaoke");
  partly(testInfo, "Whether each character fills as it is sung is for ears: the program saw per-character spans fill in turn during playback.");
});

// Steps: Language 简体中文, reopen About on the Jay Chou track; 日本語; back to
// English. Expect: simplified Chinese (周杰伦); Japanese; the layout holds.
test(title("C5"), async ({ app }, testInfo) => {
  const { page } = app;
  await wideWindow(page);
  const id = await trackFor(page, "jay");
  await openTrack(page, id);
  const fits = () => page.evaluate(() => {
    const body = document.getElementById("artistBody");
    const box = body.getBoundingClientRect();
    const over = [...body.querySelectorAll("*")].filter((el) => {
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.right > box.right + 1;
    });
    return { overflowX: body.scrollWidth > body.clientWidth + 1, over: over.length };
  });
  const setLanguage = async (code) => {
    const dialog = await openSettings(page, "general");
    await dialog.locator(".set-language").selectOption(code);
    await closeSettings(page);
  };
  try {
    await setLanguage("zh-Hans");
    await openAbout(page);
    let o = await aboutOutline(page);
    note(testInfo, `简体中文: "${o.name}", sections ${o.sections.join(", ")}.`);
    expect(o.name + o.native).toContain("周杰伦");
    expect(o.sections).toContain(await tr(page, "zh-Hans", "artist.history"));
    // Simplified script: the traditional 倫 is not how the name is written.
    expect(o.name).not.toContain("倫");
    expect(await fits(), "nothing overflows the box").toEqual({ overflowX: false, over: 0 });
    await shot(page, testInfo, "zh-Hans");
    await closeAbout(page);

    await setLanguage("ja");
    await openAbout(page);
    o = await aboutOutline(page);
    note(testInfo, `日本語: "${o.name}", sections ${o.sections.join(", ")}.`);
    expect(o.sections).toContain(await tr(page, "ja", "artist.history"));
    expect(/[\p{Script=Hiragana}\p{Script=Katakana}]/u.test(o.history + o.desc), "Japanese prose").toBe(true);
    expect(await fits(), "nothing overflows the box").toEqual({ overflowX: false, over: 0 });
    await shot(page, testInfo, "ja");
    await closeAbout(page);
  } finally {
    await page.keyboard.press("Escape").catch(() => {});
    await setLanguage("en");
  }
  await expect(page.locator("#settingsBtn")).toHaveAttribute("aria-label", "Settings");
  partly(testInfo, "Whether the Chinese and Japanese text reads naturally is for eyes: the program checked the script, the translated headings and that nothing overflows.");
});

// Steps: import YOASOBI 群青; About this song, then Lyrics. Expect: the artist
// is found and the Japanese lyrics play karaoke.
test(title("C2"), async ({ app }, testInfo) => {
  const { page } = app;
  await wideWindow(page);
  const id = await trackFor(page, "yoasobi");
  await openTrack(page, id);
  await openAbout(page);
  const o = await aboutOutline(page);
  note(testInfo, `Box: "${o.name}" ${o.native}.`);
  expect(o.name + o.native).toMatch(/YOASOBI/i);
  await shot(page, testInfo, "about");
  await closeAbout(page);
  await openLyrics(page);
  const s = await lyricsState(page);
  expect(s.synced, `synced lyrics (status: "${s.status}")`).toBeGreaterThan(5);
  expect(/[\p{Script=Hiragana}\p{Script=Katakana}]/u.test(s.text), "Japanese lyrics").toBe(true);
  const lines = page.locator("#lyricsBody .lyrics-line");
  const ja = await lines.evaluateAll((els) => els.findIndex((l) => l.lang === "ja"));
  expect(ja, "a line marked Japanese").toBeGreaterThanOrEqual(0);
  await lines.nth(Math.min(ja + 2, (await lines.count()) - 1)).click();
  const samples = await playAndWatch(page, 7000);
  expect(samples.some((x, i) => i > 0 && x.sung > samples[i - 1].sung), "the words filled during playback").toBe(true);
  await shot(page, testInfo, "karaoke");
  partly(testInfo, "Karaoke timing is for ears: the program saw the words fill during playback.");
});

// Steps: import IU 좋은 날; Lyrics, play, narrow the window a little. Expect:
// IU is found and the lyrics show; Korean fills word by word and wraps
// between words, never inside one.
test(title("C3"), async ({ app }, testInfo) => {
  const { page } = app;
  await wideWindow(page);
  const id = await trackFor(page, "iu");
  await openTrack(page, id);
  await openAbout(page);
  const o = await aboutOutline(page);
  note(testInfo, `Box: "${o.name}" ${o.native}.`);
  expect(o.name).toMatch(/\bIU\b/);
  await closeAbout(page);
  await openLyrics(page);
  const s = await lyricsState(page);
  expect(s.synced, `synced lyrics (status: "${s.status}")`).toBeGreaterThan(5);
  const lines = page.locator("#lyricsBody .lyrics-line");
  const ko = await lines.evaluateAll((els) => els.findIndex((l) => l.lang === "ko"));
  expect(ko, "a line marked Korean").toBeGreaterThanOrEqual(0);
  await lines.nth(Math.min(ko + 2, (await lines.count()) - 1)).click();
  const samples = await playAndWatch(page, 6000);
  expect(samples.some((x, i) => i > 0 && x.sung > samples[i - 1].sung), "the words filled during playback").toBe(true);
  // A little narrower, so the long lines wrap.
  await setWindowWidth(page, 1500);
  const wrap = await page.evaluate(() => {
    const rows = [...document.querySelectorAll('#lyricsBody .lyrics-line[lang="ko"]')];
    const words = rows.flatMap((l) => [...l.querySelectorAll(".lw")]);
    const hangul = /\p{Script=Hangul}/u;
    return {
      keepAll: rows.every((l) => getComputedStyle(l).wordBreak === "keep-all"),
      words: words.length,
      multi: words.filter((w) => hangul.test(w.textContent) && [...w.textContent.trim()].length > 1).length,
      broken: words.filter((w) => w.getClientRects().length > 1).map((w) => w.textContent),
      wrapped: rows.filter((l) => l.getClientRects().length && l.getBoundingClientRect().height > 1.6 * parseFloat(getComputedStyle(l).lineHeight || "20")).length,
    };
  });
  note(testInfo, `Korean: ${wrap.words} word spans, ${wrap.multi} of several syllables, ${wrap.wrapped} lines wrapped.`);
  expect(wrap.keepAll, "Korean lines break between words").toBe(true);
  expect(wrap.multi, "Korean fills by word, not by syllable").toBeGreaterThan(0);
  expect(wrap.broken, "no word split across lines").toEqual([]);
  await shot(page, testInfo, "narrow");
  await wideWindow(page);
  partly(testInfo, "Karaoke timing is for ears: the program saw words fill during playback and no word split across lines.");
});

// Steps: import Natalia Kukulska, W biegu; open its Lyrics. Expect: most
// accented words read correctly (biegnę, ciągle, chwilę, zobaczyć, ważne); no
// correct word is broken.
test(title("L2"), async ({ app }, testInfo) => {
  const { page } = app;
  await wideWindow(page);
  const id = await trackFor(page, "polish");
  await openTrack(page, id);
  await openLyrics(page);
  const s = await lyricsState(page);
  const words = ["biegnę", "ciągle", "chwilę", "zobaczyć", "ważne"];
  const text = s.text.normalize("NFC").toLowerCase();
  const found = words.filter((w) => text.includes(w));
  note(testInfo, `Source "${s.meta}"; found ${found.length} of 5: ${found.join(", ") || "none"}.`);
  expect(s.synced + s.plain, `lyrics (status: "${s.status}")`).toBeGreaterThan(5);
  if (!/Transcribed/.test(s.meta)) note(testInfo, "These lyrics were not transcribed, so the accent repair (#703) was not what produced them.");
  expect(found.length, "most of the accented words").toBeGreaterThanOrEqual(3);
  await shot(page, testInfo, "lyrics");
  partly(testInfo, "Whether any correct word was broken by the repair needs a Polish reader: the program only counted the five words.");
});

// ─── Lyrics timed line by line ──────────────────────────────────────────────

/**
 * A track's lyrics as the server keeps them, and the timing in effect, first
 * present wins (lyricsSync.js timingOf): "user" (Sync lines), "aligned"
 * (fitted line by line to the vocals) or "offset" (their own at the Align
 * offset). `sung` is the lines with words.
 */
async function keptTiming(id) {
  const entry = await api(`/api/jobs/${id}/lyrics`);
  let kind = "offset";
  let lines = lrcLines(entry.synced).map((l) => ({ ...l, time: l.time + (entry.offset_sec || 0) }));
  if (entry.user_synced) {
    kind = "user";
    lines = lrcLines(entry.user_synced);
  } else if (entry.aligned?.synced) {
    kind = "aligned";
    lines = lrcLines(entry.aligned.synced);
  }
  return { entry, kind, lines, sung: lines.filter((l) => l.text) };
}

/** The first sung line whose words include `words`, folded for case and accents. */
function lineWith(lines, words) {
  const fold = (s) => s.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase().replace(/\s+/g, " ");
  return lines.find((l) => fold(l.text).includes(fold(words)));
}

/** Click the list's line holding `words` and read where the playhead went. */
async function clickLineAt(page, words) {
  const row = page.locator("#lyricsBody .lyrics-line", { hasText: words }).first();
  await row.scrollIntoViewIfNeeded();
  await row.click();
  await page.waitForTimeout(400);
  return seconds(await page.locator("#t-time").textContent());
}

// Steps: import Natalia Kukulska, W biegu; wait for the import to finish
// (it times LRCLIB's lyrics to the vocals line by line); open Lyrics, click
// "Znowu to samo". Expect: the lines start where she sings them; "Znowu to
// samo" starts near 0:24, not at LRCLIB's 0:50.
//
// Measured against the vocals stem's own envelope (GET .../vocal-envelope),
// with the onset rule written again in helpers.mjs voiceOnsets, not the
// app's: at least 85% of the sung lines start within 0.5 s of a rise of the
// voice.
test(title("L5"), async ({ app }, testInfo) => {
  const { page } = app;
  await wideWindow(page);
  const id = await trackFor(page, "polish");
  const { entry, kind, lines, sung } = await keptTiming(id);
  const aligned = entry.aligned;
  note(testInfo, `Lyrics from ${entry.source}, timing "${entry.timing}"; in effect: ${kind}`
    + (aligned ? `; fitted to the vocals: ${Math.round(aligned.matched * 100)}% heard, ${aligned.lines_matched} of ${aligned.lines} lines found.` : "; nothing fitted to the vocals."));
  expect(kind, `the lyrics carry a timing line by line from the vocals (source ${entry.source}, timing ${entry.timing})`).toBe("aligned");
  expect(sung.length, "sung lines").toBeGreaterThan(8);

  const voice = await voiceOnsets(id);
  expect(voice.frames, "a vocals envelope").toBeGreaterThan(100);
  const off = sung.filter((l) => !voice.near(l.time, 0.5));
  const share = 1 - off.length / sung.length;
  note(testInfo, `${sung.length - off.length} of ${sung.length} line starts (${Math.round(share * 100)}%) within 0.5 s of a rise of the voice`
    + (off.length ? `; not: ${off.slice(0, 6).map((l) => `${l.time.toFixed(2)} s "${l.text.slice(0, 24)}"`).join(", ")}` : "") + ".");
  expect(share, "line starts on the singing").toBeGreaterThanOrEqual(0.85);

  const own = lineWith(lrcLines(entry.synced), "Znowu to samo");
  const znowu = lineWith(lines, "Znowu to samo");
  expect(znowu, `a line "Znowu to samo" (first lines: ${sung.slice(0, 4).map((l) => l.text).join(" / ")})`).toBeTruthy();
  note(testInfo, `"Znowu to samo" starts at ${znowu.time.toFixed(2)} s; LRCLIB's copy has it at ${own ? `${own.time.toFixed(2)} s` : "no such line"}.`);
  expect(Math.abs(znowu.time - 24), "Znowu to samo near 0:24").toBeLessThanOrEqual(1.5);

  // The page shows the same timing: a click on the line plays from there.
  await openTrack(page, id);
  await openLyrics(page);
  const at = await clickLineAt(page, "Znowu to samo");
  note(testInfo, `A click on the line put the playhead at ${at} s.`);
  expect(Math.abs(at - Math.floor(znowu.time)), "the playhead went to the line's start").toBeLessThanOrEqual(1);
  await shot(page, testInfo, "lyrics");
  partly(testInfo, "Whether each line lands with her voice is for ears: the program measured the line starts against the vocals' rises and one line's place.");
});

// ─── Local files ────────────────────────────────────────────────────────────

// Steps: upload a local MP3 and open it; narrow the window until the card's
// info line fills. Expect: the date moves to a second line whole; the format
// chip reads just MP3, the source reads Local, and Local appears once.
test(title("V1"), async ({ app }, testInfo) => {
  const { page } = app;
  await wideWindow(page);
  const id = await trackFor(page, "localMp3");
  await openTrack(page, id);
  await expect(page.locator("#track-quality")).toHaveText("MP3");
  await expect(page.locator("#track-source")).toHaveText("Local");
  const line = page.locator("#nowPlayingPanel .daw-track-meta-line");
  expect((await line.innerText()).match(/\bLocal\b/g)?.length, "Local once").toBe(1);
  const measure = () => page.evaluate(() => {
    const lineEl = document.querySelector("#nowPlayingPanel .daw-track-meta-line");
    const first = lineEl.querySelector("#t-meta-duration").getBoundingClientRect();
    const date = document.getElementById("track-extracted").parentElement;
    const rects = [...date.getClientRects()];
    const card = document.getElementById("nowPlayingPanel").getBoundingClientRect();
    const r = date.getBoundingClientRect();
    return {
      wrapped: r.top > first.bottom - 2,
      whole: rects.length === 1,
      inside: r.right <= card.right + 1 && r.left >= card.left - 1,
      width: innerWidth,
      text: date.textContent.trim(),
    };
  });
  let m = await measure();
  for (let w = 1700; !m.wrapped && w >= 1480; w -= 20) {
    await setWindowWidth(page, w);
    m = await measure();
  }
  note(testInfo, `Info line "${(await line.innerText()).replace(/\s+/g, " ")}"; the date wrapped at a ${m.width}px page: ${m.wrapped}.`);
  expect(m.wrapped, "the date moved to a second line before the card was hidden").toBe(true);
  expect(m.whole, "the date is whole on its line").toBe(true);
  expect(m.inside, "the date is inside the card, not cut off").toBe(true);
  await shot(page, testInfo, "wrapped", { locator: page.locator("#nowPlayingPanel") });
  await wideWindow(page);
});

// Steps: hover the Unsorted row; create a folder of your own and drag it onto
// Unsorted. Expect: Unsorted shows no drag grip, no New subfolder button and
// no highlight; the dragged folder moves beside Unsorted, not inside it; your
// own folder keeps its grip and button.
test(title("V2"), async ({ app }, testInfo) => {
  const { page } = app;
  await wideWindow(page);
  await showLibrary(page);
  const unsorted = page.locator('.folder[data-id="f-unsorted"] > .folder-head');
  await expect(unsorted).toBeVisible();
  const background = (loc) => loc.evaluate((el) => getComputedStyle(el).backgroundColor);
  await page.mouse.move(5, 5);
  await page.waitForTimeout(400);
  const before = await background(unsorted);
  await unsorted.hover();
  await page.waitForTimeout(400);
  await expect(unsorted.locator(".f-grip")).toHaveCount(0);
  await expect(unsorted.locator(".f-subfolder")).toHaveCount(0);
  expect(await background(unsorted), "no hover highlight").toBe(before);
  await shot(page, testInfo, "unsorted-hover", { locator: page.locator("#catalogList") });

  await page.locator("#newFolderBtn").click();
  const editor = page.locator(".folder-editor-backdrop");
  await expect(editor).toBeVisible();
  await editor.locator(".folder-editor-name").fill("Acceptance folder");
  await editor.locator(".folder-editor-save").click();
  await expect(editor).toHaveCount(0);
  const mineId = await page.evaluate(() => [...document.querySelectorAll(".folder")]
    .find((f) => f.querySelector(":scope > .folder-head")?.textContent.includes("Acceptance folder"))?.dataset.id);
  expect(mineId, "the new folder is in the library").toBeTruthy();
  const mine = page.locator(`.folder[data-id="${mineId}"] > .folder-head`);
  await mine.hover();
  await expect(mine.locator(".f-grip")).toBeVisible();
  await expect(mine.locator(".f-subfolder")).toHaveCount(1);

  const order = () => page.evaluate(() => [...document.querySelectorAll("#catalogList .folder")]
    .filter((f) => !f.parentElement.closest(".folder")).map((f) => f.dataset.id));
  const was = await order();
  // A new folder is added after Unsorted, so ending up just before it can
  // only be the drop.
  expect(was.indexOf(mineId), "the new folder starts after Unsorted").toBeGreaterThan(was.indexOf("f-unsorted"));
  // Just above the middle of the Unsorted row: on any other folder that is
  // the "drop inside" zone. Unsorted takes no subfolders, so it reorders.
  const box = await unsorted.boundingBox();
  const grip = await mine.locator(".f-grip").boundingBox();
  const to = { x: box.x + Math.min(40, box.width / 2), y: box.y + box.height * 0.45 };
  // A real mouse drag first.
  await page.mouse.move(grip.x + grip.width / 2, grip.y + grip.height / 2);
  await page.mouse.down();
  await page.mouse.move(to.x, to.y - 12, { steps: 8 });
  await page.mouse.move(to.x, to.y, { steps: 4 });
  await page.mouse.up();
  await page.waitForTimeout(500);
  let now = await order();
  let how = "the DevTools mouse";
  if (JSON.stringify(now) === JSON.stringify(was)) {
    // WebView2 leaves a DevTools mouse drag undelivered: the real cursor.
    const dpr = await page.evaluate(() => devicePixelRatio);
    const from = [grip.x + grip.width / 2, grip.y + grip.height / 2];
    if (osDrag([from, [from[0], from[1] + 6], [to.x, to.y - 12], [to.x, to.y]], dpr)) {
      await page.waitForTimeout(800);
      now = await order();
      how = "the Windows cursor";
    }
  }
  if (JSON.stringify(now) === JSON.stringify(was)) {
    how = "DOM drag events";
    // WebView2 did not deliver the mouse drag over the DevTools protocol, so
    // the same gesture is replayed as the drag events the library listens
    // to, at the same point on the Unsorted row.
    await page.evaluate(([id, x, y]) => {
      const gripEl = document.querySelector(`.folder[data-id="${id}"] > .folder-head .f-grip`);
      const target = document.querySelector('.folder[data-id="f-unsorted"]');
      const dt = new DataTransfer();
      const fire = (el, type) => el.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: dt, clientX: x, clientY: y }));
      fire(gripEl, "dragstart");
      fire(target, "dragenter");
      fire(target, "dragover");
      fire(target, "drop");
      fire(gripEl, "dragend");
    }, [mineId, to.x, to.y]);
    await page.waitForTimeout(500);
    now = await order();
    partly(testInfo, "The drag was replayed as DOM drag events (the WebView2 does not take a mouse drag over the DevTools protocol); a hand drag in the window is the remaining check.");
  }
  note(testInfo, `Dragged with ${how}. Top-level folders before ${was.join(", ")}; after ${now.join(", ")}.`);
  const inside = await page.evaluate((id) => Boolean(document.querySelector('.folder[data-id="f-unsorted"] .folder[data-id="' + id + '"]')), mineId);
  expect(inside, "the folder did not go inside Unsorted").toBe(false);
  expect(now.indexOf(mineId), "it moved to just before Unsorted").toBe(now.indexOf("f-unsorted") - 1);
  await mine.hover();
  await expect(mine.locator(".f-grip"), "your own folder keeps its grip").toBeVisible();
  await expect(mine.locator(".f-subfolder"), "and its New subfolder button").toHaveCount(1);
  await shot(page, testInfo, "after-drop", { locator: page.locator("#catalogList") });
});

// Steps: turn off the network; open a track, its Lyrics, click Look up again.
// Expect: "Could not reach LRCLIB. Check your connection and try again." in
// the Lyrics panel; nothing floats over the top bar.
//
// The page asks the server to look lyrics up (#719), and the server answers
// 502 when LRCLIB cannot be reached. That answer is given for the page alone
// (page.route over CDP), so the rest of the machine keeps its network. The
// lookup is only asked for a track the server kept no lyrics for, which the
// tagged tone file is.
test(title("E4"), async ({ app }, testInfo) => {
  const { page } = app;
  await wideWindow(page);
  const id = await trackFor(page, "localMp3");
  const kept = await fetch(`${readState().baseURL}/api/jobs/${id}/lyrics`);
  test.skip(kept.status !== 404, `The server kept lyrics for the tone file (HTTP ${kept.status}), so the page would not ask for a lookup.`);
  const LOOKUP = /\/api\/jobs\/[^/]+\/lyrics\/lookup$/;
  await page.route(LOOKUP, (route) => route.fulfill({ status: 502, json: { detail: "lyrics service unreachable" } }));
  try {
    await openTrack(page, id);
    await openLyrics(page);
    const offline = "Could not reach LRCLIB. Check your connection and try again.";
    await expect(page.locator("#lyricsPanel #lyricsStatus")).toHaveText(offline, { timeout: 30_000 });
    const again = page.locator("#lyricsVersions .lyrics-link", { hasText: "Look up again" });
    await expect(again).toBeVisible();
    await again.click();
    await expect(page.locator("#lyricsPanel #lyricsStatus")).toHaveText(offline, { timeout: 30_000 });
    await expect(page.locator("#error")).toBeHidden();
    // Nothing in a fixed box over the top bar says it either.
    const floating = await page.evaluate((text) => [...document.querySelectorAll("body *")].filter((el) => {
      const s = getComputedStyle(el);
      return (s.position === "fixed" || s.position === "absolute") && el.offsetParent !== null
        && el.textContent.includes(text) && !el.closest("#lyricsPanel");
    }).length, offline);
    expect(floating).toBe(0);
    await shot(page, testInfo, "offline");
  } finally {
    await page.unroute(LOOKUP);
  }
});

// Steps: upload a local file with no tags and a plain name; open its Lyrics.
// Expect: a message that there are no lyrics (or no tags to look them up by);
// never another song's words.
test(title("L3"), async ({ app }, testInfo) => {
  const { page } = app;
  await wideWindow(page);
  const id = await trackFor(page, "untagged");
  await openTrack(page, id);
  await openLyrics(page);
  const s = await lyricsState(page);
  const nothingKnown = await tr(page, "en", "lyrics.nothingKnown");
  note(testInfo, `Status "${s.status}"; ${s.synced + s.plain} lines; ${s.offered} versions offered; source "${s.meta}".`);
  expect(s.synced + s.plain, `no words shown (first: "${s.text.split("\n")[0]}")`).toBe(0);
  expect(s.offered, "no other song's versions offered").toBe(0);
  expect(s.status === nothingKnown || /^No lyrics found/.test(s.status), `a no-lyrics message, got "${s.status}"`).toBe(true);
  await shot(page, testInfo, "lyrics");
});

// Steps: import Green Day, Basket Case (the video); wait for the import to
// finish; open Lyrics and play from the first line. Expect: the lines are
// timed one by one to the video, "Do you have the time" near 0:16, and each
// word lights as it is sung.
//
// Runs before L4, whose Auto-detect would move the lyrics' own timing: this
// reads what the import left.
test(title("L6"), async ({ app }, testInfo) => {
  const { page } = app;
  await wideWindow(page);
  const id = await trackFor(page, "basketCase");
  const { entry, kind, sung } = await keptTiming(id);
  const aligned = entry.aligned;
  note(testInfo, `Lyrics from ${entry.source}, timing "${entry.timing}"; in effect: ${kind}`
    + (aligned ? `; fitted to the vocals: ${Math.round(aligned.matched * 100)}% heard, ${aligned.lines_matched} of ${aligned.lines} lines found.` : "; nothing fitted to the vocals."));
  expect(kind, `the lyrics carry a timing line by line from the vocals (source ${entry.source}, timing ${entry.timing})`).toBe("aligned");
  expect(sung.length, "sung lines").toBeGreaterThan(8);

  const first = sung[0];
  note(testInfo, `First line "${first.text}" at ${first.time.toFixed(2)} s.`);
  expect(first.time, "the first line starts where the video's singing does").toBeGreaterThan(14.5);
  expect(first.time).toBeLessThan(17.5);

  // Word stamps: on most lines, in order, and inside their own line. The
  // last stamp of a line is where it ends (lyrics_retime.to_lrc), which may
  // run a moment into the next line's lead-in; the words' own may not.
  const next = (i) => sung[i + 1]?.time ?? Infinity;
  const stamped = sung.filter((l) => l.words.length > 1);
  const bad = sung.filter((l, i) => l.words.some((w, k) => {
    const last = k === l.words.length - 1;
    return w < l.time - 0.01 || w > next(i) + (last ? 0.5 : 0.01) || (k && w < l.words[k - 1]);
  }));
  note(testInfo, `${stamped.length} of ${sung.length} lines carry word stamps; ${bad.length} out of order or outside their line.`);
  expect(stamped.length / sung.length, "word stamps on most lines").toBeGreaterThanOrEqual(0.8);
  expect(bad.map((l) => `${l.time.toFixed(2)} ${l.text}`), "word stamps in order, inside their line").toEqual([]);

  // The page follows: the first line plays from there, and the words fill.
  await openTrack(page, id);
  await openLyrics(page);
  const at = await clickLineAt(page, first.text.slice(0, 12));
  note(testInfo, `A click on the first line put the playhead at ${at} s.`);
  expect(at, "the playhead went to the first line").toBeGreaterThanOrEqual(14);
  expect(at).toBeLessThanOrEqual(17);
  const samples = await playAndWatch(page, 6000);
  expect(Math.max(...samples.map((x) => x.sung)), "words were marked as sung").toBeGreaterThan(0);
  expect(samples.some((x) => x.singing > 0), "a word was part-filled while it was sung").toBe(true);
  await shot(page, testInfo, "karaoke");
  partly(testInfo, "Whether each word lights with the voice is for ears: the program checked the line and word stamps and saw the fill advance.");
});

// Steps: import Green Day, Basket Case (the video); Lyrics, Align, Auto-detect.
// Expect: the first line, "Do you have the time", starts near 16 s, where
// the video's singing does, not at LRCLIB's 0:00; a click on it plays from
// there.
test(title("L4"), async ({ app }, testInfo) => {
  const { page } = app;
  await wideWindow(page);
  const id = await trackFor(page, "basketCase");
  await openTrack(page, id);
  await openLyrics(page);
  const s = await lyricsState(page);
  note(testInfo, `Lyrics: ${s.synced} synced lines, source "${s.meta}".`);
  expect(s.synced, `synced lyrics (status: "${s.status}")`).toBeGreaterThan(8);
  const saved = await api(`/api/jobs/${id}/lyrics`);
  note(testInfo, `Saved with timing "${saved.timing}"${"offset_sec" in saved ? `, offset ${saved.offset_sec} s` : ""}.`);

  const toggle = page.locator("#lyricsVersions .lyrics-align-toggle");
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
  await expect(page.locator("#lyricsAlign")).toBeVisible();

  // Timed line by line on import (L6): a whole-song offset no longer applies,
  // and the Align panel says so instead of offering Auto-detect.
  if (saved.aligned?.synced || saved.user_synced) {
    const key = saved.user_synced ? "lyrics.align.perLineUser" : "lyrics.align.perLineAligned";
    await expect(page.locator("#lyricsAlign")).toContainText(await tr(page, "en", key));
    await expect(page.locator("#lyricsAlign .lyrics-align-btn", { hasText: await tr(page, "en", "lyrics.align.detect") })).toBeDisabled();
    const timed = (saved.user_synced || saved.aligned.synced).split(/\r?\n/)
      .map((line) => /^\[(\d+):(\d+(?:\.\d+)?)\]\s*(?:<[^>]*>)?\s*(\S.*)$/.exec(line.trim()))
      .find(Boolean);
    const start = Number(timed[1]) * 60 + Number(timed[2]);
    note(testInfo, `Timed line by line; the offset is off, as it should be. First line "${timed[3].replace(/<[^>]*>/g, "")}" at ${start.toFixed(2)} s.`);
    expect(start, "the first line starts where the video's singing does").toBeGreaterThan(14.5);
    expect(start).toBeLessThan(17.5);
    await shot(page, testInfo, "per-line");
    return;
  }

  const message = page.locator("#lyricsAlign .lyrics-align-message");
  await page.locator("#lyricsAlign .lyrics-align-btn", { hasText: await tr(page, "en", "lyrics.align.detect") }).click();
  const checking = await tr(page, "en", "lyrics.align.detecting");
  await expect.poll(async () => {
    const text = (await message.textContent()).trim();
    return text !== "" && text !== checking;
  }, { timeout: 60_000 }).toBe(true);
  const said = (await message.textContent()).trim();
  const shown = (await page.locator("#lyricsAlign .lyrics-align-offset").textContent()).trim();
  note(testInfo, `Auto-detect said "${said}"; offset shown ${shown}.`);
  expect(said, "a confident estimate").toMatch(/^Moved to fit the vocals/);

  // Where the first sung line starts now: its stamp plus the offset kept.
  const after = await api(`/api/jobs/${id}/lyrics`);
  const stamp = after.synced.split(/\r?\n/)
    .map((line) => /^\[(\d+):(\d+(?:\.\d+)?)\]\s*(\S.*)$/.exec(line.trim()))
    .find(Boolean);
  expect(stamp, "a stamped line with words").toBeTruthy();
  const first = Number(stamp[1]) * 60 + Number(stamp[2]) + (after.offset_sec || 0);
  note(testInfo, `First line "${stamp[3]}" now starts at ${first.toFixed(2)} s.`);
  expect(first, "the first line starts where the video's singing does").toBeGreaterThan(14.5);
  expect(first).toBeLessThan(17.5);

  // And the page follows: a click on the first line plays from there.
  await page.locator("#lyricsBody .lyrics-line", { hasText: stamp[3].slice(0, 12) }).first().click();
  await expect.poll(async () => seconds(await page.locator("#t-time").textContent()), { timeout: 5000 })
    .toBeGreaterThanOrEqual(14);
  expect(seconds(await page.locator("#t-time").textContent())).toBeLessThanOrEqual(17);
  await shot(page, testInfo, "aligned");
  partly(testInfo, "Whether the words now land with the voice is for ears: the program saw the first line move to the video's first sung line.");
});

// ─── Sync lines ─────────────────────────────────────────────────────────────

/** Open the Lyrics tab's Sync lines mode: its panel and the lane over the waveform. */
async function openSync(page) {
  const toggle = page.locator("#lyricsVersions .lyrics-sync-toggle");
  await expect(toggle, "a Sync lines link").toBeVisible();
  expect(await toggle.getAttribute("aria-disabled"), "Sync lines is enabled for these lyrics").not.toBe("true");
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
  const panel = page.locator("#lyricsSync");
  await expect(panel).toBeVisible();
  await expect(page.locator("#lyricsLane")).toBeVisible();
  return panel;
}

/** A button of the Sync lines panel, by its English label. */
async function syncButton(page, key) {
  return page.locator("#lyricsSync .lyrics-align-btn", { hasText: await tr(page, "en", key) }).first();
}

/** Set one of the panel's options (Snap to voice, Move later lines too). */
async function syncOption(page, key, on) {
  const box = page.locator("#lyricsSync .lyrics-sync-opt", { hasText: await tr(page, "en", key) }).locator("input");
  if ((await box.isChecked()) !== on) await box.click();
  await expect(box).toBeChecked({ checked: on });
}

/** Each line's start as the list shows it in Sync lines (data-time), in seconds. */
function listTimes(page) {
  return page.locator("#lyricsBody .lyrics-line").evaluateAll((rows) => rows.map((r) => r.dataset.time || ""))
    .then((texts) => texts.map(clockSeconds));
}

/** The playhead now, from the app's own transport. */
function playhead(page) {
  return page.evaluate(async () => (await import("/js/transport.js")).transport()?.getCurrentTime?.() ?? NaN);
}

/** Wait for the panel to say the timing is saved. */
async function waitSaved(page) {
  const saved = await tr(page, "en", "lyrics.sync.saved");
  await expect(page.locator("#lyricsSync .lyrics-sync-save")).toHaveText(saved, { timeout: 15_000 });
}

// Steps: open Wicked, Dancing Through Life; Lyrics, Sync lines; play; tap T
// at the start of three lines; drag a line's marker in the lane over the
// waveform; Undo; Done; open Sync lines again and Reset to detected.
// Expect: each tapped line starts where the playhead was, the list says so;
// the drag moves one line and Undo puts it back; Done keeps the timing
// (GET .../lyrics shows user_synced); Reset to detected drops it and the
// lines are as they were.
//
// Snap to voice is turned off so a tap lands exactly on the playhead, and
// Move later lines too so a drag moves one line only.
test(title("L7"), async ({ app }, testInfo) => {
  const { page } = app;
  await wideWindow(page);
  const id = await trackFor(page, "wicked");
  const before = await keptTiming(id);
  note(testInfo, `Timing in effect before: ${before.kind}${before.entry.user_synced ? " (a timing by hand was already kept)" : ""}.`);
  await openTrack(page, id);
  await openLyrics(page);
  const s = await lyricsState(page);
  expect(s.synced, `synced lyrics (status: "${s.status}")`).toBeGreaterThan(8);
  await openSync(page);
  await syncOption(page, "lyrics.sync.snap", false);
  await syncOption(page, "lyrics.sync.ripple", false);
  const initial = await listTimes(page);
  expect(initial.every(Number.isFinite), "every line shows its start").toBe(true);
  await shot(page, testInfo, "sync-open");

  // Tap three lines while playing, each after putting the playhead 0.6 s
  // into it: the tap is read between the playhead just before and just after.
  const rows = page.locator("#lyricsBody .lyrics-line");
  const selected = () => rows.evaluateAll((els) => els.findIndex((r) => r.classList.contains("selected")));
  const play = page.locator("#t-play");
  await play.click();
  const taps = [];
  try {
    for (let k = 0; k < 3; k++) {
      const index = await selected();
      expect(index, "a line in hand").toBeGreaterThanOrEqual(0);
      const target = (await listTimes(page))[index] + 0.6;
      await page.evaluate(async (t) => (await import("/js/transport.js")).setPlayheadTime(t), target);
      await page.waitForTimeout(300);
      const from = await playhead(page);
      await page.keyboard.press("t");
      const to = await playhead(page);
      await expect.poll(selected, { timeout: 5000 }).not.toBe(index);
      taps.push({ index, target, from, to, shown: (await listTimes(page))[index] });
    }
  } finally {
    await play.click();
  }
  note(testInfo, `Taps: ${taps.map((x) => `line ${x.index + 1} at ${x.shown.toFixed(2)} s (playhead ${x.from.toFixed(2)} to ${x.to.toFixed(2)} s)`).join("; ")}.`);
  for (const x of taps) {
    expect(x.shown, `line ${x.index + 1} starts where the playhead was`).toBeGreaterThanOrEqual(x.from - 0.03);
    expect(x.shown).toBeLessThanOrEqual(x.to + 0.03);
    expect(Math.abs(x.shown - x.target), `line ${x.index + 1} near the seek target`).toBeLessThan(1);
  }
  await expect(page.locator("#lyricsSync .lyrics-sync-kind").first()).toHaveText(await tr(page, "en", "lyrics.sync.kind.user"));
  await waitSaved(page);
  await shot(page, testInfo, "tapped");

  // Drag: the marker of a later line with room on both sides, moved right
  // by half its room (at most 3 s).
  const tapped = await listTimes(page);
  const last = taps.at(-1).index;
  let j = -1;
  let room = 0;
  for (let i = last + 1; i < tapped.length - 1; i++) {
    const gap = Math.min(tapped[i + 1] - tapped[i], tapped[i] - tapped[i - 1]);
    if (gap > room) { room = gap; j = i; }
    if (room >= 4) break;
  }
  expect(j, "a line with room to move").toBeGreaterThan(last);
  const shift = Math.min(3, (tapped[j + 1] - tapped[j]) / 2);
  // Taking the line in hand scrolls the waveform to its marker.
  await rows.nth(j).scrollIntoViewIfNeeded();
  await rows.nth(j).click();
  const mark = page.locator(`#daw-lyrics-track .lyr-mark[data-index="${j}"]`);
  await expect(mark).toBeVisible();
  const trackWidth = await page.locator("#daw-lyrics-track").evaluate((el) => el.getBoundingClientRect().width);
  // The lane spans the track's length as the studio has it (lyrics.js laneLength).
  const duration = await page.evaluate(async () => (await import("/js/state.js")).totalDuration);
  expect(duration, "the track's length").toBeGreaterThan(0);
  const box = await mark.boundingBox();
  const dx = (shift / duration) * trackWidth;
  const x0 = box.x + Math.min(4, box.width / 2);
  const y0 = box.y + box.height / 2;
  let how = "mouse";
  await page.mouse.move(x0, y0);
  await page.mouse.down();
  await page.mouse.move(x0 + dx / 2, y0, { steps: 4 });
  await page.mouse.move(x0 + dx, y0, { steps: 4 });
  await page.mouse.up();
  let dragged = (await listTimes(page))[j];
  if (Math.abs(dragged - tapped[j]) < 0.01) {
    // WebView2 leaves a DevTools mouse drag undelivered: the real cursor.
    const dpr = await page.evaluate(() => devicePixelRatio);
    if (osDrag([[x0, y0], [x0 + dx / 2, y0], [x0 + dx, y0]], dpr)) {
      await page.waitForTimeout(500);
      dragged = (await listTimes(page))[j];
      how = "the Windows cursor";
    }
  }
  if (Math.abs(dragged - tapped[j]) < 0.01) {
    // The pointer did not reach the marker through WebView2: the same
    // gesture as pointer events on the marker itself.
    how = "pointer events";
    await mark.evaluate((el, [x, y, d]) => {
      const fire = (type, cx) => el.dispatchEvent(new PointerEvent(type, {
        bubbles: true, cancelable: true, pointerId: 1, pointerType: "mouse", isPrimary: true, button: 0, buttons: type === "pointerup" ? 0 : 1, clientX: cx, clientY: y,
      }));
      fire("pointerdown", x);
      fire("pointermove", x + d / 2);
      fire("pointermove", x + d);
      fire("pointerup", x + d);
    }, [x0, y0, dx]);
    dragged = (await listTimes(page))[j];
  }
  const px = duration / trackWidth;
  note(testInfo, `Dragged line ${j + 1}'s marker by ${dx.toFixed(0)} px (${shift.toFixed(2)} s) with ${how}: ${tapped[j].toFixed(2)} s to ${dragged.toFixed(2)} s.`);
  expect(Math.abs(dragged - (tapped[j] + shift)), "the dragged line moved by the drag").toBeLessThanOrEqual(Math.max(0.15, 3 * px));
  const afterDrag = await listTimes(page);
  expect(afterDrag.filter((t, i) => i !== j && Math.abs(t - tapped[i]) > 0.005), "no other line moved").toEqual([]);
  if (how === "pointer events") partly(testInfo, "The drag was made with pointer events on the marker, because the mouse over CDP did not move it: dragging with a real mouse is for a person.");
  await shot(page, testInfo, "dragged");

  // Undo takes the drag back.
  await (await syncButton(page, "lyrics.sync.undo")).click();
  await expect.poll(async () => (await listTimes(page))[j], { timeout: 5000 }).toBeCloseTo(tapped[j], 2);
  expect(await listTimes(page), "every line as before the drag").toEqual(tapped);
  await waitSaved(page);

  // Done: the timing is kept on the server.
  await page.locator("#lyricsSync .lyrics-align-done").click();
  await expect(page.locator("#lyricsSync")).toHaveCount(0);
  await expect(page.locator("#lyricsLane")).toBeHidden();
  let kept = null;
  await expect.poll(async () => {
    kept = await keptTiming(id);
    return kept.kind;
  }, { timeout: 15_000 }).toBe("user");
  for (const x of taps) {
    expect(kept.lines[x.index]?.time, `line ${x.index + 1} kept as tapped`).toBeCloseTo(x.shown, 1);
  }
  expect(kept.lines[j]?.time, `line ${j + 1} kept as before the drag`).toBeCloseTo(tapped[j], 1);
  note(testInfo, `Kept: user_synced with ${kept.lines.length} lines.`);

  // Reset to detected: the timing by hand goes, and the lines are as they were.
  await openSync(page);
  await (await syncButton(page, "lyrics.sync.resetDetected")).click();
  const back = await page.locator("#lyricsSync .lyrics-align-message").textContent();
  note(testInfo, `Reset to detected said "${back.trim()}".`);
  expect(back.trim()).toBe(await tr(page, "en", `lyrics.sync.backTo.${before.entry.aligned ? "aligned" : "offset"}`));
  await waitSaved(page);
  const reset = await listTimes(page);
  if (before.kind !== "user") {
    expect(reset.map((t, i) => Math.abs(t - initial[i]) <= 0.011).every(Boolean), "every line back where it was").toBe(true);
  }
  await page.locator("#lyricsSync .lyrics-align-done").click();
  await expect.poll(async () => (await keptTiming(id)).kind, { timeout: 15_000 }).not.toBe("user");
  expect((await api(`/api/jobs/${id}/lyrics`)).user_synced, "no user_synced kept").toBeUndefined();
  await shot(page, testInfo, "reset");
  partly(testInfo, "Whether tapping feels in time while listening is for a person: the program tapped at known playhead times.");
});

// ─── The hour-long compilation ──────────────────────────────────────────────

// Steps: import 邓丽君经典金曲 (59 min); open About this song. Expect: the box
// opens on Teresa Teng (鄧麗君) with her history, with nothing typed.
test(title("C4"), async ({ app }, testInfo) => {
  test.skip(SKIP_LONG, "STEMDECK_ACCEPTANCE_SKIP_LONG=1: the 59 minute compilation is not imported.");
  const { page } = app;
  await wideWindow(page);
  note(testInfo, "Max track length was raised to 60 minutes in Settings before this import: at the default 20 the app refuses the video, which the manual steps do not mention.");
  const id = await trackFor(page, "teresa");
  await openTrack(page, id);
  await openAbout(page);
  const o = await aboutOutline(page);
  note(testInfo, `Box: "${o.name}" ${o.native}; sections ${o.sections.join(", ")}.`);
  expect(o.name).toContain("Teresa Teng");
  expect(o.native).toBe("鄧麗君");
  expect(o.sections).toContain("History");
  expect(o.searchShown, "no field waiting for a name").toBe(false);
  await shot(page, testInfo, "about");
  await closeAbout(page);
});

// Steps: open the Teresa Teng track, look at the ruler. Expect: labels every
// 5 minutes (0:00 ... 55:00), none overlapping; a normal song still shows
// 0:30 steps.
test(title("T1"), async ({ app }, testInfo) => {
  const { page } = app;
  await wideWindow(page);
  const ruler = () => page.evaluate(() => {
    const labels = [...document.querySelectorAll("#ruler-time .tick-label")];
    const boxes = labels.map((l) => l.getBoundingClientRect());
    const overlaps = boxes.filter((b, i) => i > 0 && b.left < boxes[i - 1].right).length;
    return { texts: labels.map((l) => l.textContent.trim()), overlaps };
  });
  if (!SKIP_LONG) {
    const long = await trackFor(page, "teresa");
    await openTrack(page, long);
    await expect.poll(async () => (await ruler()).texts.length, { timeout: 30_000 }).toBeGreaterThan(1);
    const r = await ruler();
    note(testInfo, `Hour-long ruler: ${r.texts.join(" ")}.`);
    const want = Array.from({ length: 12 }, (_, i) => `${i * 5}:00`);
    expect(r.texts).toEqual(want);
    expect(r.overlaps, "no labels overlap").toBe(0);
    await shot(page, testInfo, "hour", { locator: page.locator("#ruler-time") });
  }
  const song = await trackFor(page, "billie");
  await openTrack(page, song);
  await expect.poll(async () => (await ruler()).texts.length, { timeout: 30_000 }).toBeGreaterThan(1);
  const r = await ruler();
  note(testInfo, `Song ruler: ${r.texts.join(" ")}.`);
  const steps = r.texts.map(seconds).map((v, i, a) => (i ? v - a[i - 1] : null)).slice(1);
  expect(new Set(steps)).toEqual(new Set([30]));
  expect(r.overlaps).toBe(0);
  await shot(page, testInfo, "song", { locator: page.locator("#ruler-time") });
  if (SKIP_LONG) partly(testInfo, "The hour-long half was skipped (STEMDECK_ACCEPTANCE_SKIP_LONG=1); only the normal song's 0:30 steps were checked.");
});

// Steps: open Wicked, Dancing Through Life; Lyrics, Sync lines, Re-time from
// vocals; wait. Expect: the panel says it is listening, then either that the
// lines were timed from the vocals (how many matched) or that the vocals did
// not match well enough and nothing changed; never stuck, never an error.
//
// The run waits behind any import in progress (the pipeline lock), so this
// check waits for an idle queue first and then gives the run 10 minutes.
test(title("L8"), async ({ app }, testInfo) => {
  const { page } = app;
  await waitForIdleQueue();
  await wideWindow(page);
  const id = await trackFor(page, "wicked");
  const before = await keptTiming(id);
  await openTrack(page, id);
  await openLyrics(page);
  await openSync(page);
  const retime = await syncButton(page, "lyrics.sync.retime");
  const message = page.locator("#lyricsSync .lyrics-align-message");
  const t0 = Date.now();
  await retime.click();
  await expect(retime).toHaveAttribute("aria-busy", "true", { timeout: 5000 });
  const listening = await tr(page, "en", "lyrics.sync.retiming");
  await expect.poll(async () => (await message.textContent()).trim(), { timeout: 10_000 }).toContain(listening.replace(/…$/, ""));
  await shot(page, testInfo, "listening");
  await expect(retime).not.toHaveAttribute("aria-busy", "true", { timeout: 10 * 60_000 });
  const took = (Date.now() - t0) / 1000;
  const said = (await message.textContent()).trim();
  const state = await api(`/api/jobs/${id}/lyrics/retime`);
  note(testInfo, `Re-time took ${took.toFixed(0)} s and said "${said}"; the server: ${JSON.stringify(state)}.`);
  expect(said, "not a failure").not.toBe(await tr(page, "en", "lyrics.sync.retimeFailed"));
  expect(["done", "unsure"], "the server's answer").toContain(state.state);
  if (state.state === "done") {
    const want = (await tr(page, "en", "lyrics.sync.retimed"))
      .replace("{matched}", String(state.lines_matched)).replace("{count}", String(state.lines));
    expect(said, "the panel reports the result").toBe(want);
    await expect(page.locator("#lyricsSync .lyrics-sync-kind").first()).toHaveText(await tr(page, "en", "lyrics.sync.kind.aligned"));
    const after = await keptTiming(id);
    expect(after.kind, "the new timing is the one in effect").toBe("aligned");
    const shown = await listTimes(page);
    expect(shown.map((t, i) => Math.abs(t - (after.lines[i]?.time ?? NaN)) <= 0.011).every(Boolean), "the list shows the new timing").toBe(true);
  } else {
    expect(said, "the panel says the vocals did not match").toBe(await tr(page, "en", "lyrics.sync.retimeUnsure"));
    const after = await keptTiming(id);
    expect(after.kind, "the timing is as it was").toBe(before.kind);
    note(testInfo, "The vocals did not match the lines well enough, so the timing was left as it was: an allowed answer.");
  }
  await shot(page, testInfo, "result");
  await page.locator("#lyricsSync .lyrics-align-done").click();
  await expect(page.locator("#lyricsSync")).toHaveCount(0);
});

// ─── Errors and cancel, on an idle queue ────────────────────────────────────

/** A fresh page with nothing open, so an import holds the foreground. */
async function freshStudio(page) {
  await waitForIdleQueue();
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.locator("#url")).toBeVisible({ timeout: 60_000 });
  await expect(page.locator(".app")).toHaveClass(/no-track/, { timeout: 30_000 });
  await wideWindow(page);
}

async function failAnImport(page) {
  await importLink(page, BROKEN_LINK);
  await expect(page.locator("#error")).toBeVisible({ timeout: 120_000 });
}

// Steps: split the broken link; close the card with the x; again, and press
// Escape. Expect: a solid card at the top right with a red edge, readable
// text and a Try again button; the x closes it, so does Escape.
test(title("E1"), async ({ app }, testInfo) => {
  const { page } = app;
  await freshStudio(page);
  await failAnImport(page);
  const box = page.locator("#error");
  const look = await box.evaluate((el) => {
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return {
      position: s.position,
      background: s.backgroundColor,
      color: s.color,
      edgeWidth: s.borderLeftWidth,
      edge: s.borderLeftColor,
      fromRight: innerWidth - r.right,
      fromTop: r.top,
      text: el.querySelector(".error-msg")?.textContent.trim() || "",
    };
  });
  note(testInfo, `Card text: "${look.text.slice(0, 160)}".`);
  expect(look.position).toBe("fixed");
  expect(look.fromRight, "at the right").toBeLessThan(40);
  expect(look.fromTop, "at the top").toBeLessThan(40);
  expect(look.background, "solid").toMatch(/^rgb\(/);
  expect(parseFloat(look.edgeWidth), "a thick left edge").toBeGreaterThanOrEqual(3);
  const [r, g, b] = look.edge.match(/\d+/g).map(Number);
  expect(r > g && r > b, `the edge is red (${look.edge})`).toBe(true);
  expect(look.text.length, "it says something").toBeGreaterThan(5);
  await expect(box.locator(".retry-btn")).toHaveText("Try again");
  await shot(page, testInfo, "card");
  await box.locator(".error-close").click();
  await expect(box).toBeHidden();

  await failAnImport(page);
  await page.keyboard.press("Escape");
  await expect(box).toBeHidden();
  partly(testInfo, "Readability is for eyes: the program checked a solid background, the red edge and that the card has text.");
});

// Steps: trigger the error again and click Try again. Expect: the card closes
// and the link field is focused with its text selected.
test(title("E2"), async ({ app }, testInfo) => {
  const { page } = app;
  await freshStudio(page);
  await failAnImport(page);
  await shot(page, testInfo, "card");
  await page.locator("#error .retry-btn").click();
  await expect(page.locator("#error")).toBeHidden();
  const field = await page.evaluate(() => {
    const el = document.getElementById("url");
    return { focused: document.activeElement === el, start: el.selectionStart, end: el.selectionEnd, length: el.value.length };
  });
  expect(field.focused, "the link field has focus").toBe(true);
  expect(field.length, "the link is still there").toBeGreaterThan(0);
  expect([field.start, field.end], "its text is selected").toEqual([0, field.length]);
  await shot(page, testInfo, "after-try-again");
});

// Steps: start the broken import and at once open About this song on any
// track; when the card appears, press Escape, then again. Expect: the first
// Escape closes only the card; the About box stays; the second closes it.
test(title("E3"), async ({ app }, testInfo) => {
  const { page } = app;
  const any = await trackFor(page, "billie");
  await freshStudio(page);
  await importLink(page, BROKEN_LINK);
  await openTrack(page, any);
  await openAbout(page);
  const card = page.locator("#error");
  const dialog = page.locator("#artistDialog");
  const appeared = await card.waitFor({ state: "visible", timeout: 60_000 }).then(() => true, () => false);
  if (!appeared) {
    // As written the steps cannot show the card: opening a track moves the
    // running import to the background (job.js detachForegroundJob), and a
    // background failure goes to Notifications. The layering itself is
    // checked with a failure raised while the box is open: a link the server
    // refuses, which is shown in the card whatever else is open.
    const notified = await page.locator("#notifBadge").evaluate((el) => !el.classList.contains("hidden"));
    note(testInfo, `As written, no card appeared: the failed import went to Notifications (badge lit: ${notified}), because opening a track sends the import to the background.`);
    await page.evaluate(() => {
      const url = document.getElementById("url");
      url.value = "https://example.com/not-a-video";
      document.getElementById("job-form").requestSubmit();
    });
    await expect(card).toBeVisible({ timeout: 30_000 });
    partly(testInfo, "As written the card never appears: opening a track sends the running import to the background, so its failure goes to Notifications. The Escape order was checked with a refused link while the About box was open.");
  }
  await expect(dialog).toBeVisible();
  await shot(page, testInfo, "card-over-about");
  await page.keyboard.press("Escape");
  await expect(card).toBeHidden();
  await expect(dialog, "the About box stays open after the first Escape").toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
});

// Steps: queue two imports; click Cancel on the first while it runs. Expect:
// the first stops within a few seconds and the second starts; nothing hangs.
test(title("E5"), async ({ app }, testInfo) => {
  const { page } = app;
  await waitForIdleQueue();
  await wideWindow(page);
  await showLibrary(page);
  const first = await importLink(page, "https://www.youtube.com/watch?v=dQw4w9WgXcQ");
  const second = await importLink(page, "https://www.youtube.com/watch?v=fJ9rUzIMcZQ");
  await expect.poll(async () => (await job(first)).status, { timeout: 60_000 }).not.toBe("queued");
  const queue = page.locator(".rail-queue");
  await expect(queue).toBeVisible();
  await queue.click();
  const cancel = page.locator(`.queue-row[data-id="${first}"] .queue-cancel`);
  await expect(cancel).toBeVisible();
  await shot(page, testInfo, "queue");
  const stageAtCancel = (await job(first)).stage;
  const t0 = Date.now();
  await cancel.click();
  await expect.poll(async () => (await job(first)).status, { timeout: 30_000, intervals: [250] }).toBe("cancelled");
  const stoppedSec = (Date.now() - t0) / 1000;
  await expect.poll(async () => (await job(second)).status, { timeout: 60_000, intervals: [500] }).not.toBe("queued");
  const startedSec = (Date.now() - t0) / 1000;
  note(testInfo, `Cancel pressed during "${stageAtCancel}"; the first stopped ${stoppedSec.toFixed(1)} s later and the second started ${startedSec.toFixed(1)} s after Cancel.`);
  expect(stoppedSec, "stopped within a few seconds").toBeLessThan(10);
  await shot(page, testInfo, "second-running");
  // Let the second prove it is really running, then take it out too.
  await expect.poll(async () => (await job(second)).progress, { timeout: 120_000 }).toBeGreaterThan(0);
  await page.locator(`.queue-row[data-id="${second}"] .queue-cancel`).click();
  await expect.poll(async () => (await job(second)).status, { timeout: 30_000 }).toBe("cancelled");
  await showLibrary(page);
});

// Steps: turn off the network; upload a local file and split it. Expect: the
// stems are ready as usual; no details or lyrics; no error, no long wait.
//
// The machine's network is not touched. The app is started a second time with
// every outside request sent to a proxy nobody listens on (HTTP(S)_PROXY for
// the backend, --proxy-server for WebView2), loopback excepted, which is what
// an unplugged cable looks like to it.
test(title("E6"), async ({ app }, testInfo) => {
  test.skip(!desktop(), "Server target: the offline relaunch needs the desktop build.");
  await app.browser.close().catch(() => {});
  await stopRecordedApp();
  await launchApp({ offline: true, label: "offline" });
  const { browser, page } = await connect(chromium, { timeout: 5 * 60_000 });
  watchNetwork(page);
  try {
    await expect(page.locator("#url")).toBeVisible({ timeout: 120_000 });
    await wideWindow(page);
    await showLibrary(page);
    const online = readState().jobs?.localMp3;
    const onlineJob = online && !online.startsWith("failed") ? await job(online).catch(() => null) : null;
    const file = makeAudio("offline check.mp3", { tags: { title: "Offline Check", artist: "StemDeck Acceptance" } });
    const t0 = Date.now();
    const id = await importFile(page, file);
    const done = await waitForJob(id, { timeout: 20 * 60_000, label: "offline upload" });
    const took = (Date.now() - t0) / 1000;
    const sum = (t) => Object.values(t || {}).reduce((a, b) => a + (Number(b) || 0), 0);
    const timings = done.stage_timings || {};
    const waited = (Number(timings.identify_wait) || 0) + (Number(timings.lyrics_wait) || 0);
    note(testInfo, `Offline import took ${took.toFixed(0)} s from upload, ${sum(timings).toFixed(0)} s of stages`
      + `${onlineJob ? ` (the same kind of file online: ${sum(onlineJob.stage_timings).toFixed(0)} s of stages)` : ""}`
      + `; waiting on identification and lyrics ${waited.toFixed(1)} s; identity source ${done.identity?.source || "none"}, lyrics ${done.has_lyrics}.`);
    expect(done.status).toBe("done");
    expect(done.stems.length, "stems are ready").toBeGreaterThan(0);
    expect(done.has_lyrics, "no lyrics").toBe(false);
    expect(done.identity?.source === "acoustid" || done.identity?.source === "musicbrainz", "no details from a service").toBe(false);
    expect(waited, "no long wait on the unreachable services").toBeLessThan(30);
    await expect(page.locator("#error")).toBeHidden();
    await openTrack(page, id);
    await expect(page.locator("#error")).toBeHidden();
    await shot(page, testInfo, "offline-track");
  } finally {
    await browser.close().catch(() => {});
    await stopRecordedApp();
  }
});
