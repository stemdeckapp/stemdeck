// Karaoke Mode (static/js/karaoke/): a view over the workspace. These pin the
// contract: one source of truth for speed, key and stems, both ways; the stem
// cards in their order (Original, the voice, the instruments) with the voice
// off on every entry; a saved mix of its own for every song, apart from
// Practice's and Advanced's (mixer.js); the queue; the progress bar; the
// lyrics by the media clock; and fullscreen falling back to a layer.

import { test, expect } from "@playwright/test";
import { JOB_ID, SIBLING_JOB_ID, openStudio, chooseMode, leaveModeView } from "./helpers.mjs";

const LRC = "[00:01.00] one two three\n[00:02.50] four five six\n[00:04.00] seven eight\n[00:05.00] nine ten";

async function stubLyrics(page) {
  await page.route(new RegExp(`/api/jobs/${JOB_ID}/lyrics$`), (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    return route.fulfill({ json: { v: 1, source: "lrclib", track: "Fixture", artist: "E2E", album: "", duration: 6, synced: LRC, plain: "" } });
  });
}

// The fixture song was separated with every stem but Original (the sum of the
// stems that were not extracted: the backing track). A real vocals-only
// extraction has it, so the song's state is given one, its audio the drums'.
async function withOriginal(page) {
  await page.route(new RegExp(`/api/jobs/${JOB_ID}$`), async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    const res = await route.fetch();
    const json = await res.json();
    const drums = json.stems?.find((s) => s.name === "drums");
    if (json.stems && drums && !json.stems.some((s) => s.name === "original")) {
      json.stems.unshift({ name: "original", url: drums.url });
    }
    return route.fulfill({ response: res, json });
  });
}

const savedMix = (page, mode = "karaoke") => ev(page, async ([id, m]) => (await import("/js/utils.js")).storeGet(`stemdeck:mix:${id}:${m}`, null), [JOB_ID, mode]);
const setMode = async (page, mode) => {
  await ev(page, async (m) => (await import("/js/mode.js")).setMode(m), mode);
  await page.waitForTimeout(700);
};
const speedOf = (page) => ev(page, async () => (await import("/js/state.js")).playbackSpeed);
const mute = (page, name, on) => ev(page, async ([n, v]) => {
  const m = await import("/js/mixer.js");
  const { mixerState } = await import("/js/state.js");
  mixerState[n].muted = v;
  m.refreshMixerVisuals();
  m.applyMix();
  m.persistMix();
}, [name, on]);
const mixOf = (page) => ev(page, async () => {
  const { mixerState, trackIndex } = await import("/js/state.js");
  return Object.fromEntries(Object.keys(trackIndex).map((n) => [n, `${mixerState[n].muted ? "M" : "-"}${mixerState[n].soloed ? "S" : "-"}`]));
});
const cardsOf = (page) => ev(page, () => [...document.querySelectorAll("#karaoke .kk-stem")].map((c) => `${c.dataset.stem}:${c.classList.contains("on") ? "on" : "off"}`));

const kk = (page, sel) => page.locator(`#karaoke ${sel}`);
const ev = (page, fn, arg) => page.evaluate(fn, arg);
const muted = (page, name) => ev(page, async (n) => (await import("/js/state.js")).mixerState[n]?.muted, name);
const seek = (page, s) => ev(page, async (x) => (await import("/js/transport.js")).setPlayheadTime(x), s);
const now = (page) => ev(page, async () => (await import("/js/transport.js")).transport().getCurrentTime());

async function openKaraoke(page) {
  await chooseMode(page, "karaoke");
  await expect(page.locator("#karaoke")).toBeVisible();
  await expect(page.locator('#modeMenu .mode-item[data-mode="karaoke"]')).toHaveAttribute("aria-checked", "true");
}

test.describe("Karaoke Mode", () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1800, height: 1020 });
    await stubLyrics(page);
  });

  test("Original is the Backing track card and plays; the voice is off", async ({ page }) => {
    await withOriginal(page);
    await openStudio(page);
    await openKaraoke(page);
    await expect.poll(() => cardsOf(page)).toEqual(["original:on", "vocals:off"]);
    // Original on the left, the voice right after it, and no instrument cards.
    const names = (await cardsOf(page)).map((c) => c.split(":")[0]);
    expect(names).toEqual(["original", "vocals"]);
    // Only the voice can be switched: the backing track's switch is hidden.
    await expect(kk(page, ".kk-stem-row .kk-switch:visible")).toHaveCount(1);
    await expect(kk(page, '.kk-stem[data-stem="original"] .kk-stem-label')).toHaveText("Backing track");
    const mix = await mixOf(page);
    expect(mix.original).toBe("--");
    expect(mix.vocals[0]).toBe("M");
    // Turning the voice on adds it; it does not replace the backing track.
    await kk(page, '.kk-stem[data-stem="vocals"] .kk-switch').click();
    expect((await mixOf(page)).vocals[0]).toBe("-");
    expect((await mixOf(page)).original[0]).toBe("-");
  });

  test("each mode keeps its own saved mix and speed of a song", async ({ page }) => {
    await withOriginal(page);
    await openStudio(page);
    // Advanced (the tests' mode): the vocals muted, played at 0.75x.
    await mute(page, "vocals", true);
    await page.locator("#t-speed-075").click();
    await expect.poll(async () => (await savedMix(page, "daw"))?.__global?.speed).toBe(0.75);

    // Practice: its own, not Advanced's.
    await setMode(page, "practice");
    expect((await mixOf(page)).vocals[0]).toBe("-");
    expect(await speedOf(page)).toBe(1);
    await mute(page, "bass", true);

    // Advanced again: as it was left, without Practice's bass.
    await setMode(page, "daw");
    expect((await mixOf(page)).vocals[0]).toBe("M");
    expect((await mixOf(page)).bass[0]).toBe("-");
    expect(await speedOf(page)).toBe(0.75);

    // Karaoke: its own again: the voice off, the backing track on, speed 1x.
    // Karaoke's lanes are the voice and Original (every instrument folded into
    // it, partChoice.js KARAOKE_LANES), so there is no Bass lane here.
    await openKaraoke(page);
    await expect.poll(async () => (await mixOf(page)).vocals[0]).toBe("M");
    expect(Object.keys(await mixOf(page)).sort()).toEqual(["original", "vocals"]);
    expect((await mixOf(page)).original[0]).toBe("-");
    expect(await speedOf(page)).toBe(1);
    await ev(page, async () => (await import("/js/transport.js")).setSpeed(1.25));
    await expect.poll(async () => (await savedMix(page, "karaoke"))?.__global?.speed).toBe(1.25);

    // Back out: Advanced is untouched by what was done in Karaoke.
    await leaveModeView(page);
    await expect(page.locator("#karaoke")).toBeHidden();
    await expect.poll(async () => (await mixOf(page)).drums[0]).toBe("-");
    expect(await speedOf(page)).toBe(0.75);
    expect((await savedMix(page, "daw")).vocals.muted).toBe(true);
    expect((await savedMix(page, "daw")).drums.muted).toBe(false);
    expect((await savedMix(page, "practice")).bass.muted).toBe(true);

    // Karaoke again: its own is remembered, but the voice is off every time.
    await mute(page, "vocals", false);
    await openKaraoke(page);
    await expect.poll(async () => (await mixOf(page)).vocals[0]).toBe("M");
    expect((await mixOf(page)).original[0]).toBe("-");
    expect(await speedOf(page)).toBe(1.25);
    await leaveModeView(page);

    // And all of it comes back after a reload, mode by mode.
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    await expect.poll(async () => (await mixOf(page)).vocals?.[0]).toBe("-");
    await setMode(page, "practice");
    await expect.poll(async () => (await mixOf(page)).bass[0]).toBe("M");
    expect(await speedOf(page)).toBe(1);
  });

  test("a song's older single saved mix starts Practice and Advanced, and not Karaoke", async ({ page }) => {
    await page.addInitScript((id) => {
      if (sessionStorage.getItem("seeded-old-mix")) return;
      sessionStorage.setItem("seeded-old-mix", "1");
      localStorage.setItem(`stemdeck:mix:${id}`, JSON.stringify({ bass: { volume: 1, muted: true, soloed: false, pitch: 0, fx: [] } }));
    }, JOB_ID);
    await openStudio(page);
    expect((await mixOf(page)).bass[0]).toBe("M");
    await setMode(page, "practice");
    expect((await mixOf(page)).bass[0]).toBe("M");
    // Karaoke does not start from it: the voice is off and the backing track
    // (Original, which holds the bass in Karaoke) plays.
    await openKaraoke(page);
    await expect.poll(async () => (await mixOf(page)).original?.[0]).toBe("-");
    expect((await mixOf(page)).bass).toBeUndefined();
    // The old record is never written.
    expect(await ev(page, (id) => JSON.parse(localStorage.getItem(`stemdeck:mix:${id}`)).bass.muted, JOB_ID)).toBe(true);
  });

  test("the queue: songs dragged in from the library, put in order, kept", async ({ page }) => {
    await page.setViewportSize({ width: 1800, height: 1020 });
    await openStudio(page);
    await openKaraoke(page);
    const queue = kk(page, ".kk-side");
    await expect(queue).toContainText("Drag and drop the next song from the library");
    await expect(queue.locator("button")).toHaveCount(0);

    const libRow = (id) => page.locator(`#catalogList .cat-item[data-id="${id}"]`).first();
    await libRow(SIBLING_JOB_ID).dragTo(kk(page, ".kk-queue"));
    await expect(kk(page, ".kk-q-row")).toHaveCount(1);
    await libRow(JOB_ID).dragTo(kk(page, ".kk-q-hint"));
    await expect(kk(page, ".kk-q-row")).toHaveCount(2);
    const ids = () => ev(page, () => [...document.querySelectorAll("#karaoke .kk-q-row")].map((r) => r.dataset.id));
    expect(await ids()).toEqual([SIBLING_JOB_ID, JOB_ID]);
    // Dragging a row inside the queue reorders it; Alt + arrows too.
    await kk(page, ".kk-q-row").nth(1).dragTo(kk(page, ".kk-q-row").first(), { targetPosition: { x: 40, y: 3 } });
    await expect.poll(ids).toEqual([JOB_ID, SIBLING_JOB_ID]);
    await kk(page, ".kk-q-row").first().focus();
    await page.keyboard.press("Alt+ArrowDown");
    await expect.poll(ids).toEqual([SIBLING_JOB_ID, JOB_ID]);
    // A song is in it once.
    await libRow(SIBLING_JOB_ID).dragTo(kk(page, ".kk-q-hint"));
    await expect.poll(ids).toEqual([JOB_ID, SIBLING_JOB_ID]);
    // Kept across a reload.
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    await openKaraoke(page);
    await expect.poll(ids).toEqual([JOB_ID, SIBLING_JOB_ID]);
    // Removed with its x.
    await kk(page, ".kk-q-row").nth(1).locator(".kk-q-remove").click();
    await expect.poll(ids).toEqual([JOB_ID]);
  });

  test("when a song ends, the next in the queue plays", async ({ page }) => {
    await page.setViewportSize({ width: 1800, height: 1020 });
    await openStudio(page);
    await ev(page, (id) => localStorage.setItem("stemdeck:karaoke:queue", JSON.stringify([id])), SIBLING_JOB_ID);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    await openKaraoke(page);
    await expect(kk(page, ".kk-q-row")).toHaveCount(1);
    // The fixture song is 6 s long: play its last second.
    await ev(page, async () => {
      const t = await import("/js/transport.js");
      t.setPlayheadTime(5);
      if (!t.transport().isPlaying()) t.togglePlayPause();
    });
    await expect.poll(() => ev(page, async () => (await import("/js/state.js")).currentJobId), { timeout: 30000 }).toBe(SIBLING_JOB_ID);
    await expect(kk(page, ".kk-q-row")).toHaveCount(0);
    await expect.poll(() => ev(page, async () => (await import("/js/transport.js")).transport().isPlaying()), { timeout: 30000 }).toBe(true);
    // Its voice is off.
    expect((await mixOf(page)).vocals[0]).toBe("M");
  });

  test("opens over the lanes, applies its defaults to the mixer, and closes back to the studio", async ({ page }) => {
    await openStudio(page);
    await openKaraoke(page);
    await expect(page.locator("#lanes")).toHaveAttribute("inert", "");
    // The lead (here the one Vocals stem) off, the rest on, no solo.
    await expect.poll(() => muted(page, "vocals")).toBe(true);
    expect(await muted(page, "drums")).toBe(false);
    await expect(kk(page, '.kk-stem[data-stem="vocals"]')).toHaveClass(/\boff\b/);
    await expect(kk(page, '.kk-stem[data-stem="drums"]')).toHaveCount(0);

    await leaveModeView(page);
    await expect(page.locator("#karaoke")).toBeHidden();
    expect(await page.locator("#lanes").getAttribute("inert")).toBeNull();
    // Karaoke's changes are the view's: the mixer shows them while it is open.
    // And puts the studio back as it was: the vocals were not muted before.
    await expect(page.locator('#mixer .lane-header[data-stem="vocals"]')).not.toHaveClass(/\bmuted\b/);
    await expect(page.locator('#mixer .lane-header[data-stem="drums"]')).not.toHaveClass(/\bmuted\b/);
  });

  test("stems, speed and key are the studio's own state, both ways", async ({ page }) => {
    await openStudio(page);
    await openKaraoke(page);

    await kk(page, '.kk-stem[data-stem="vocals"] .kk-switch').click();
    expect(await muted(page, "vocals")).toBe(false);
    await ev(page, async () => (await import("/js/mixer.js")).toggleStemMute("vocals"));
    await expect(kk(page, '.kk-stem[data-stem="vocals"] .kk-switch')).toHaveAttribute("aria-checked", "false");

    await ev(page, async () => (await import("/js/transport.js")).setSpeed(1.25));
    expect(await ev(page, async () => (await import("/js/state.js")).playbackSpeed)).toBe(1.25);
    for (const id of ["#t-speed-05", "#t-speed-075", "#t-speed-1"]) await expect(page.locator(id)).not.toHaveClass(/active/);
    // Karaoke has no speed box, and the footer with the studio's speed
    // buttons is hidden while it is open.
    await expect(page.locator("#t-speed-1")).toBeHidden();
    await expect(kk(page, ".kk-speed")).toHaveCount(0);

    const pitch = await ev(page, async () => (await import("/js/transport.js")).getGlobalPitch());
    test.skip(!pitch.available, "no pitch stage in this browser");
    await kk(page, ".kk-key-btns .kk-sq").nth(1).click();
    await expect(page.locator("#t-pitch-value")).toHaveText("+1");
    await expect(kk(page, ".kk-badge")).toHaveText("+1");
    // The other way: the studio's key (its footer is hidden here) shows in Karaoke.
    await ev(page, async () => (await import("/js/transport.js")).setGlobalPitch(-2));
    await expect(kk(page, ".kk-badge")).toHaveText("\u22122");
    await kk(page, ".kk-key-btns .kk-reset").click();
    await expect(page.locator("#t-pitch-value")).toHaveText("0");
    await expect(kk(page, ".kk-badge")).toHaveText("0");
  });

  test("lyrics follow the media clock, and the side panel is the queue alone", async ({ page }) => {
    await openStudio(page);
    // The song has sections; Karaoke does not list them.
    await ev(page, async (id) => {
      (await import("/js/sections.js")).initSections(id, [
        { id: "a", name: "Intro", start: 0, end: 2, color: "#4a7fff" },
        { id: "b", name: "Verse", start: 2, end: 6, color: "#2ab8e8" },
      ], 6);
    }, JOB_ID);
    await openKaraoke(page);
    await expect(kk(page, ".kk-side-title")).toHaveText("Queue");
    await expect(kk(page, ".kk-queue")).toContainText("Drag and drop the next song from the library");
    await expect(kk(page, ".kk-tab")).toHaveCount(0);
    await expect(kk(page, ".kk-sec")).toHaveCount(0);
    await expect(page.locator("#karaoke")).not.toContainText("Intro");

    await seek(page, 0.3);
    await expect(kk(page, ".kk-lyrics-window .kk-intro")).toBeVisible();
    await expect(kk(page, ".kk-lyrics-window .kk-next")).toContainText("one two three");

    await seek(page, 2.9);
    await expect(kk(page, ".kk-lyrics-window .kk-current")).toContainText("one two three");
    await expect(kk(page, ".kk-lyrics-window .kk-current")).toContainText("four five six");
    const sung = () => kk(page, ".kk-lyrics-window .kk-current .kk-w.on").count();
    await expect.poll(sung).toBeGreaterThan(3);
    const later = await sung();
    await seek(page, 1.2);
    await expect.poll(sung).toBeLessThan(later);

    // The progress bar seeks.
    const bar = await kk(page, ".kk-bar").boundingBox();
    await page.mouse.click(bar.x + bar.width / 2, bar.y + bar.height / 2);
    await expect.poll(() => now(page)).toBeCloseTo(3, 0);
    // [ and ] are the studio's own again (5 s), not a section jump.
    await seek(page, 4);
    await page.locator("#karaoke .kk-title").click();
    await page.keyboard.press("BracketLeft");
    await expect.poll(() => now(page)).toBeCloseTo(0, 0);
  });

  test("the Mode menu opens it, and Practice or Advanced close it", async ({ page }) => {
    await openStudio(page);
    await page.locator("#modeBtn").click();
    await page.locator('.mode-item[data-mode="karaoke"]').click();
    await expect(page.locator("#karaoke")).toBeVisible();
    await page.locator("#modeBtn").click();
    await expect(page.locator('.mode-item[data-mode="karaoke"]')).toHaveAttribute("aria-checked", "true");
    await page.locator('.mode-item[data-mode="practice"]').click();
    await expect(page.locator("#karaoke")).toBeHidden();
    await expect(page.locator("html")).toHaveAttribute("data-mode", "practice");
  });

  test("fullscreen refused by the browser falls back to a layer that Esc closes", async ({ page }) => {
    await openStudio(page);
    await openKaraoke(page);
    await ev(page, () => { Element.prototype.requestFullscreen = () => Promise.reject(new Error("blocked")); });
    await kk(page, ".kk-fs-btn").click();
    await expect(page.locator(".kk-fs")).toBeVisible();
    await expect(page.locator(".kk-fs")).toHaveClass(/kk-fs-fallback/);
    // The pill names the key and the voice (the speed left it with the speed box).
    await expect(page.locator(".kk-fs .kk-pill")).toContainText("Vocals off");
    await page.keyboard.press("Escape");
    await expect(page.locator(".kk-fs")).toBeHidden();
    await expect(page.locator("#karaoke")).toBeVisible();
  });
});
