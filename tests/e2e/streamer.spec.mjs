// Streamer Mode (static/js/streamer/): a view over the workspace. These pin
// the phase 1 contract: entering and leaving from the Mode menu
// keeps the song and its position; a saved mix of its own for every song,
// apart from Practice's, Advanced's and Karaoke's (mixer.js, mixMode.js); the
// footer hidden while open (as in Karaoke) and back after, with Streamer's
// own Stop and Play / Pause; the aspect is saved per song; no layers, no
// templates and no dead settings button; the safe zones switch; and one
// workspace view at a time with Karaoke.

import { test, expect } from "@playwright/test";
import { JOB_ID, SIBLING_JOB_ID, openStudio, chooseMode, leaveModeView } from "./helpers.mjs";
import { removeTracks } from "./recording-helpers.mjs";

// A take recorded here makes a Performance track on the shared e2e backend,
// and a spec that runs next in the same shard then finds a track it did not
// make ("the only track is armed by Record" no longer holds). Each test takes
// its tracks and takes away again.
test.afterEach(async ({ request }) => removeTracks(request));

const ev = (page, fn, arg) => page.evaluate(fn, arg);
const sm = (page, sel) => page.locator(`#streamer ${sel}`);
const seek = (page, s) => ev(page, async (x) => (await import("/js/transport.js")).setPlayheadTime(x), s);
const now = (page) => ev(page, async () => (await import("/js/transport.js")).transport().getCurrentTime());
// The transport reports a duration (openStudio's signal) a moment before the
// audio engine's ready resolves, and a seek before then does not hold: the
// engine comes up at 0 (reloadPlace.js says the same).
const engineReady = (page) => ev(page, async () => { await (await import("/js/state.js")).audioEngine?.ready; });
const setMode = async (page, mode) => {
  await ev(page, async (m) => (await import("/js/mode.js")).setMode(m), mode);
  await page.waitForTimeout(700);
};
const mixOf = (page) => ev(page, async () => {
  const { mixerState, trackIndex } = await import("/js/state.js");
  const { mixGlobals } = await import("/js/mixer.js");
  const lanes = Object.fromEntries(Object.keys(trackIndex).map((n) => [n, `${mixerState[n].muted ? "M" : "-"}${mixerState[n].soloed ? "S" : "-"}`]));
  return { lanes, speed: mixGlobals.speed };
});
const toggleMute = (page, name) => ev(page, async (n) => (await import("/js/mixer.js")).toggleStemMute(n), name);
const savedMix = (page, mode) => ev(page, async ([id, m]) => (await import("/js/utils.js")).storeGet(`stemdeck:mix:${id}:${m}`, null), [JOB_ID, mode]);
const savedScene = (page, id = JOB_ID) => ev(page, async (jid) => (await import("/js/utils.js")).storeGet(`stemdeck:streamer:scene:${jid}`, null), id);

// Performance is entered and left through the Mode menu (there is no rail
// button for it).
async function openStreamer(page) {
  await chooseMode(page, "streamer");
  await expect(page.locator("#streamer")).toBeVisible();
  await expect(page.locator('#modeMenu .mode-item[data-mode="streamer"]')).toHaveAttribute("aria-checked", "true");
}

async function closeStreamer(page) {
  await leaveModeView(page);
  await expect(page.locator("#streamer")).toBeHidden();
  await expect(page.locator('#modeMenu .mode-item[data-mode="streamer"]')).toHaveAttribute("aria-checked", "false");
}

test.describe("Streamer Mode", () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1800, height: 1020 });
  });

  test("the Mode menu opens and closes it over the lanes, keeping the song and position", async ({ page }) => {
    await openStudio(page);
    await engineReady(page);
    await seek(page, 3);
    await openStreamer(page);
    await expect(page.locator("html")).toHaveClass(/streamer-open/);
    await expect(page.locator("#lanes")).toHaveAttribute("inert", "");
    await expect(sm(page, ".sm-head-title")).toHaveText("Performance Mode");
    expect(Math.abs((await now(page)) - 3)).toBeLessThan(0.3);
    await closeStreamer(page);
    await expect(page.locator("#lanes")).not.toHaveAttribute("inert", "");
    await expect(page.locator("html")).not.toHaveClass(/streamer-open/);
    expect(Math.abs((await now(page)) - 3)).toBeLessThan(0.3);
  });

  test("the footer is hidden while open and back after; Stop and Play / Pause are Streamer's own", async ({ page }) => {
    await openStudio(page);
    const footer = page.locator(".daw-footer");
    await expect(footer).toBeVisible();
    await openStreamer(page);
    await expect(footer).toBeHidden();
    // The view takes the footer's place: down to the bottom of the column.
    const view = await page.locator("#streamer").boundingBox();
    const col = await page.locator(".daw-main-col").boundingBox();
    expect(Math.abs(view.y + view.height - (col.y + col.height))).toBeLessThanOrEqual(1);
    const playing = () => ev(page, async () => (await import("/js/transport.js")).transport().isPlaying());
    const play = sm(page, ".sm-play");
    await expect(play).toHaveAttribute("aria-label", "Play");
    await play.click();
    await expect.poll(playing).toBe(true);
    await expect(play).toHaveAttribute("aria-label", "Pause");
    await play.click();
    await expect.poll(playing).toBe(false);
    await seek(page, 3);
    await sm(page, ".sm-stop").click();
    await expect.poll(() => now(page)).toBeLessThan(0.3);
    await closeStreamer(page);
    await expect(footer).toBeVisible();
    // Karaoke and back: the footer follows whichever view is open.
    await openStreamer(page);
    await chooseMode(page, "karaoke");
    await expect(footer).toBeHidden();
    await leaveModeView(page);
    await expect(footer).toBeVisible();
  });

  test("its own saved mix: nothing leaks into Practice, Advanced or Karaoke", async ({ page }) => {
    await openStudio(page);
    await setMode(page, "daw");
    const advanced = await mixOf(page);
    await setMode(page, "practice");
    await toggleMute(page, "drums");
    await page.waitForTimeout(500);
    const practice = await mixOf(page);
    const practiceSaved = await savedMix(page, "practice");

    await openStreamer(page);
    // Streamer starts from its defaults, not Practice's muted drums.
    await expect.poll(async () => (await mixOf(page)).lanes.drums).toBe("--");
    await toggleMute(page, "bass");
    await ev(page, async () => (await import("/js/transport.js")).setSpeed(0.75));
    await page.waitForTimeout(500);
    const streamer = await mixOf(page);
    expect(streamer.lanes.bass).toBe("M-");
    await closeStreamer(page);

    await expect.poll(() => mixOf(page)).toEqual(practice);
    expect(await savedMix(page, "practice")).toEqual(practiceSaved);
    await setMode(page, "daw");
    await expect.poll(() => mixOf(page)).toEqual(advanced);
    await setMode(page, "practice");

    // Karaoke keeps its own mix too, with the voice off. Its lanes are the
    // voice and Original (the bass is in it), and none of Streamer's muted bass.
    await chooseMode(page, "karaoke");
    await expect(page.locator("#karaoke")).toBeVisible();
    await expect.poll(async () => (await mixOf(page)).lanes.vocals).toBe("M-");
    expect((await mixOf(page)).lanes.original).toBe("--");
    expect((await mixOf(page)).lanes.bass).toBeUndefined();
    await leaveModeView(page);

    // Back in Streamer: its own record is still there.
    await openStreamer(page);
    await expect.poll(() => mixOf(page)).toEqual(streamer);
    expect(await savedMix(page, "streamer")).not.toBeNull();
  });

  test("one view at a time with Karaoke", async ({ page }) => {
    await openStudio(page);
    await openStreamer(page);
    await chooseMode(page, "karaoke");
    await expect(page.locator("#karaoke")).toBeVisible();
    await expect(page.locator("#streamer")).toBeHidden();
    await expect(page.locator("#lanes")).toHaveAttribute("inert", "");
    await chooseMode(page, "streamer");
    await expect(page.locator("#streamer")).toBeVisible();
    await expect(page.locator("#karaoke")).toBeHidden();
    await closeStreamer(page);
    await expect(page.locator("#lanes")).not.toHaveAttribute("inert", "");
  });

  test("the Mode menu lists Performance (id streamer) and shows it current while open", async ({ page }) => {
    await openStudio(page);
    await page.locator("#modeBtn").click();
    await expect(page.locator(".mode-item .mode-item-name")).toHaveText(["Practice", "Advanced", "Karaoke", "Performance"]);
    await page.locator('.mode-item[data-mode="streamer"]').click();
    await expect(page.locator("#streamer")).toBeVisible();
    await page.locator("#modeBtn").click();
    await expect(page.locator('.mode-item[data-mode="streamer"]')).toHaveAttribute("aria-checked", "true");
    await page.locator('.mode-item[data-mode="practice"]').click();
    await expect(page.locator("#streamer")).toBeHidden();
  });

  test("the camera only: no Scene Layers, no Add Layer, no Templates tab, no settings gear", async ({ page }) => {
    await openStudio(page);
    await openStreamer(page);
    await expect(sm(page, ".sm-canvas")).toBeVisible();
    // No tabs at all: the one view is the camera, the strip and the timeline.
    await expect(sm(page, ":is([role=tab], [role=tablist], [role=tabpanel])")).toHaveCount(0);
    await expect(page.locator("#streamerOtherPanel")).toHaveCount(0);
    // No layers panel, no layer rows, no Add Layer, no selection box.
    await expect(sm(page, ":is(.sm-layers, .sm-layer, .sm-add, .sm-selbox)")).toHaveCount(0);
    // The gear that only said "Coming in a later update" is gone (#794); the
    // input's own settings button (Settings, Recording) stays.
    await expect(sm(page, ".sm-settings")).toHaveCount(0);
    await expect(sm(page, '[aria-label="Performance settings"]')).toHaveCount(0);
    await expect(sm(page, ".sm-in-settings")).toHaveCount(1);
    const text = await page.locator("#streamer").innerText();
    for (const gone of ["Scene Layers", "Add Layer", "Templates", "Scene Builder", "Track Info", "Lower Third", "Not available yet"]) expect(text).not.toContain(gone);
    // Stop and Play / Pause sit in the mode header now.
    await expect(sm(page, ".sm-head .sm-play")).toBeVisible();
    await expect(sm(page, ".sm-head .sm-stop")).toBeVisible();
    // Clicking the live window selects nothing and changes nothing.
    const box = await sm(page, ".sm-preview").boundingBox();
    await page.mouse.click(box.x + box.width * 0.7, box.y + box.height * 0.3);
    await page.waitForTimeout(400);
    expect(await savedScene(page)).toBeNull();
  });

  test("the aspect is saved per song; a new song starts with the default scene", async ({ page }) => {
    await openStudio(page);
    await openStreamer(page);
    const aspect = sm(page, ".sm-aspect select");
    await expect(aspect).toHaveValue("16:9");
    await aspect.selectOption("9:16");
    await expect.poll(async () => (await savedScene(page))?.aspect).toBe("9:16");
    // The preview stays 16:9 on screen whatever the scene's aspect.
    const box = await sm(page, ".sm-preview").boundingBox();
    expect(Math.abs(box.width / box.height - 16 / 9)).toBeLessThan(0.02);

    await page.locator(`.cat-item[data-id="${SIBLING_JOB_ID}"]`).first().click();
    await expect(aspect).toHaveValue("16:9");
    expect(await savedScene(page, SIBLING_JOB_ID)).toBeNull();
    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    await expect(aspect).toHaveValue("9:16");
  });

  test("Show Safe Zones switches and is remembered", async ({ page }) => {
    await openStudio(page);
    await openStreamer(page);
    const sw = sm(page, ".sm-safe");
    await expect(sw).toHaveAttribute("aria-checked", "false");
    await sw.click();
    await expect(sw).toHaveAttribute("aria-checked", "true");
    expect(await ev(page, () => localStorage.getItem("stemdeck.streamer.safeZones"))).toBe("1");
    // Space on the switch presses it, and does not start playback.
    await sw.focus();
    await page.keyboard.press("Space");
    await expect(sw).toHaveAttribute("aria-checked", "false");
    expect(await ev(page, async () => (await import("/js/transport.js")).transport().isPlaying())).toBe(false);
  });

  test("a scene saved with layers by an older build opens, keeps its aspect and trim, and its layers are left as they were", async ({ page }) => {
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await openStudio(page);
    const old = {
      v: 2, aspect: "9:16", trim: { in: 1, out: 5 },
      layers: [
        { id: "camera", type: "camera", visible: false, frame: { x: 0, y: 0, w: 1, h: 1 }, props: { mirror: true } },
        { id: "lyrics", type: "lyrics", visible: true, frame: { x: 0.5, y: 0.6, w: 0.3, h: 0.3 }, props: { lines: 5 } },
        { id: "title", type: "title", visible: true, frame: "nope" },
        null,
      ],
    };
    await ev(page, async ([key, value]) => (await import("/js/utils.js")).storeSet(key, value), [`stemdeck:streamer:scene:${JOB_ID}`, old]);
    await openStreamer(page);
    await expect(sm(page, ".sm-aspect select")).toHaveValue("9:16");
    await expect(sm(page, ".sm-layer")).toHaveCount(0);
    // Shown, not edited: the record is untouched.
    await page.waitForTimeout(600);
    expect(await savedScene(page)).toEqual(old);
    // An edit (the aspect) writes the scene's own fields and the old layers as they were.
    await sm(page, ".sm-aspect select").selectOption("1:1");
    await expect.poll(async () => (await savedScene(page))?.aspect).toBe("1:1");
    const saved = await savedScene(page);
    expect(saved.trim).toEqual({ in: 1, out: 5 });
    expect(saved.layers).toEqual(old.layers);
    expect(errors).toEqual([]);
  });

  test("export to a file is the only way out: no upload card, Export MP4 waits for a take", async ({ page }) => {
    await openStudio(page);
    await openStreamer(page);
    await expect(sm(page, ".sm-yt")).toHaveCount(0);
    await expect(sm(page, ".sm-export-go")).toBeVisible();
    await expect(sm(page, ".sm-export-go")).toBeDisabled();
    await expect(sm(page, ".sm-export-text")).toHaveText("Record a take, then export it here as an MP4.");
  });
});

// The take timeline: zoom, the ruler, the trim, and undo of the scene's edits.
test.describe("Streamer Mode: timeline", () => {
  const undoOn = (page) => ev(page, () => !document.querySelector("#streamer .sm-undo").disabled);
  async function dragMouse(page, from, to, { alt = false } = {}) {
    await page.mouse.move(...from);
    if (alt) await page.keyboard.down("Alt");
    await page.mouse.down();
    await page.mouse.move(...to, { steps: 8 });
    await page.mouse.up();
    if (alt) await page.keyboard.up("Alt");
  }

  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1800, height: 1020 });
    await openStudio(page);
    await openStreamer(page);
  });

  test("an aspect change is one undo step; Ctrl+Z and Ctrl+Y take it back and forth", async ({ page }) => {
    await sm(page, ".sm-aspect select").selectOption("1:1");
    await expect.poll(async () => (await savedScene(page))?.aspect).toBe("1:1");
    await expect.poll(() => undoOn(page)).toBe(true);
    await page.locator("#streamer").focus();
    await page.keyboard.press("Control+z");
    await expect.poll(async () => (await savedScene(page))?.aspect).toBe("16:9");
    await expect(sm(page, ".sm-aspect select")).toHaveValue("16:9");
    await expect.poll(() => undoOn(page)).toBe(false);
    await page.keyboard.press("Control+y");
    await expect.poll(async () => (await savedScene(page))?.aspect).toBe("1:1");
  });

  test("Delete and Backspace in the view delete nothing", async ({ page }) => {
    await sm(page, ".sm-aspect select").selectOption("1:1");
    await expect.poll(async () => (await savedScene(page))?.aspect).toBe("1:1");
    const before = JSON.stringify(await savedScene(page));
    await page.locator("#streamer").focus();
    await page.keyboard.press("Delete");
    await page.keyboard.press("Backspace");
    await page.waitForTimeout(500);
    expect(JSON.stringify(await savedScene(page))).toBe(before);
    await expect(page.locator(`#catalogList .cat-item[data-id="${JOB_ID}"]`).first()).not.toHaveClass(/in-trash/);
  });

  test("zoom, Fit and the ruler", async ({ page }) => {
    // Full zoom is 120 px a second (streamerModel.js ZOOM_MAX_PPS). The fixture
    // song is 6 s long, so on a wide window the fitted view is already past
    // that and zoom has nothing to add: a narrow window leaves it room.
    await page.setViewportSize({ width: 1024, height: 900 });
    const widths = () => ev(page, () => {
      const s = document.querySelector("#streamer .sm-tl-scroll");
      return [s.clientWidth, s.scrollWidth];
    });
    // Four steps of 10% widen the song past the view (about 67 px here); ten
    // reach full zoom, where + is disabled. (The slider between - and + is
    // hidden at this width.)
    for (let i = 0; i < 4; i++) await sm(page, ".sm-zoom-in").click();
    await expect.poll(async () => { const [c, s] = await widths(); return s > c + 40; }).toBe(true);
    const [, at40] = await widths();
    for (let i = 0; i < 6; i++) await sm(page, ".sm-zoom-in").click();
    await expect(sm(page, ".sm-zoom-track")).toHaveAttribute("aria-valuenow", "100");
    await expect(sm(page, ".sm-zoom-in")).toBeDisabled();
    await expect.poll(async () => (await widths())[1]).toBeGreaterThan(at40);
    await sm(page, ".sm-fit").click();
    await expect.poll(async () => { const [c, s] = await widths(); return s <= c + 1; }).toBe(true);
    const rb = await sm(page, ".sm-tl-ruler").boundingBox();
    await page.mouse.click(rb.x + rb.width / 2, rb.y + rb.height / 2);
    const dur = await ev(page, async () => (await import("/js/state.js")).totalDuration);
    await expect.poll(() => now(page)).toBeGreaterThan(dur * 0.45);
    expect(await now(page)).toBeLessThan(dur * 0.55);
  });

  test("trim: the in handle sets the range, saved per song", async ({ page }) => {
    const h = await sm(page, ".sm-tl-trim-l").boundingBox();
    const rb = await sm(page, ".sm-tl-ruler").boundingBox();
    await dragMouse(page, [h.x + 6, h.y + h.height / 2], [h.x + 6 + rb.width * 0.3, h.y + h.height / 2], { alt: true });
    await expect.poll(async () => (await savedScene(page))?.trim?.in ?? 0).toBeGreaterThan(1);
    const saved = (await savedScene(page)).trim;
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    await openStreamer(page);
    await expect.poll(async () => (await savedScene(page))?.trim).toEqual(saved);
  });
});

// Phase 5: narrow layouts, empty and error states, the second lane.
test.describe("Streamer Mode polish", () => {
  test("under 1000 px: Output opens a drawer, focus moves in, Escape closes it", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await openStudio(page);
    await openStreamer(page);
    const out = sm(page, ".sm-output-btn");
    await expect(out).toBeVisible();
    await out.click();
    await expect(out).toHaveAttribute("aria-expanded", "true");
    expect(await ev(page, () => document.querySelector("#streamer .sm-side").contains(document.activeElement))).toBe(true);
    await page.keyboard.press("Escape");
    await expect(out).toHaveAttribute("aria-expanded", "false");
    await expect(out).toBeFocused();
  });

  test("under 1000 px: the label column is 56 px and the preview never under 480 px", async ({ page }) => {
    await page.setViewportSize({ width: 800, height: 700 });
    await openStudio(page);
    await openStreamer(page);
    const label = await sm(page, ".sm-tl-label-video").boundingBox();
    const preview = await sm(page, ".sm-preview").boundingBox();
    expect(Math.round(label.width)).toBe(56);
    expect(preview.width).toBeGreaterThanOrEqual(479);
    expect(await ev(page, () => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  });

  // The live window gives way so no control needs scrolling: the workspace,
  // the right panel and the drawer.
  for (const [w, h] of [[1920, 1080], [1600, 900], [1366, 768], [1280, 720]]) {
    test(`${w} x ${h}: every control fits without scrolling`, async ({ page }) => {
      await page.setViewportSize({ width: w, height: h });
      await openStudio(page);
      await openStreamer(page);
      const out = sm(page, ".sm-output-btn");
      if (await out.isVisible()) await out.click();
      const over = await ev(page, () => {
        const root = document.querySelector("#streamer");
        const found = [];
        for (const e of [root, ...root.querySelectorAll("*")]) {
          if (!e.getClientRects().length) continue;
          const oy = getComputedStyle(e).overflowY;
          if ((e === root || oy === "auto" || oy === "scroll") && e.scrollHeight > e.clientHeight + 1) found.push(`${e.className} ${e.scrollHeight}>${e.clientHeight}`);
        }
        return found;
      });
      expect(over).toEqual([]);
      const preview = await sm(page, ".sm-preview").boundingBox();
      expect(preview.width).toBeGreaterThanOrEqual(359);
      const go = await sm(page, ".sm-export-go").boundingBox();
      expect(go.y + go.height).toBeLessThanOrEqual(h);
    });
  }

  test("no song open: the view says so and nothing under it is reachable", async ({ page }) => {
    await page.goto("/");
    await openStreamer(page);
    await expect(sm(page, ".sm-no-track")).toBeVisible();
    expect(await ev(page, () => document.querySelector("#streamer .sm-center").inert)).toBe(true);
  });

  test("I play leaves the part out of what Streamer plays, for Streamer only", async ({ page }) => {
    await openStudio(page);
    await openStreamer(page);
    const sel = page.locator("#streamerPart");
    // The parts are listed once the song's stems are known: read them then,
    // or the skip below fires on a list that is still empty.
    await expect.poll(() => sel.locator("option").count()).toBeGreaterThan(1);
    const values = await sel.locator("option").evaluateAll((os) => os.map((o) => o.value));
    expect(values[0]).toBe("");
    // Keys and Voice, the instrument families, not the stem names Piano and Vocals.
    const texts = await sel.locator("option").evaluateAll((os) => Object.fromEntries(os.map((o) => [o.value, o.textContent])));
    if ("piano" in texts) expect(texts.piano).toBe("Keys");
    if ("vocals" in texts) expect(texts.vocals).toBe("Voice");
    test.skip(!values.includes("drums"), "the test song has no drums stem");
    await sel.selectOption("drums");
    await expect.poll(() => ev(page, async () => [...(await import("/js/player.js")).playbackExclusion().names])).toContain("drums");
    await expect(sm(page, ".sm-tl-label-stem").first()).toHaveText("Backing track");
    expect(await ev(page, (id) => JSON.parse(localStorage.getItem(`stemdeck:stems:${id}:streamer`)).remove, JOB_ID)).toBe("drums");
    expect(await ev(page, (id) => localStorage.getItem(`stemdeck:stems:${id}:practice`), JOB_ID)).toBe(null);
    await closeStreamer(page);
    await expect.poll(() => ev(page, async () => (await import("/js/player.js")).playbackExclusion().names.size)).toBe(0);
  });

  // Wide enough that Recording & Output is a panel, not the drawer behind the
  // Output button (under 1000 px of view, as at the default 1280 x 720).
  test("an export failure says why and offers Retry", async ({ page }) => {
    await page.setViewportSize({ width: 1800, height: 1020 });
    await openStudio(page);
    await ev(page, (id) => localStorage.setItem(`stemdeck:streamer:take:${id}`, "fake-take"), JOB_ID);
    await page.route("**/takes/fake-take/render", (r) => r.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "ffmpeg exited with code 1" }) }));
    // Streamer checks that the last take is still on the server: the job lists it.
    await page.route(`**/api/jobs/${JOB_ID}`, async (r) => {
      if (r.request().method() !== "GET") return r.continue();
      const res = await r.fetch();
      const job = await res.json();
      return r.fulfill({ response: res, json: { ...job, takes: [...(job.takes || []), { id: "fake-take" }] } });
    });
    await openStreamer(page);
    await sm(page, ".sm-export-go").click();
    await expect(sm(page, ".sm-export-text")).toContainText("ffmpeg exited with code 1");
    await expect(sm(page, ".sm-export-go")).toHaveText("Try again");
  });

  test("no audio input: the line says the take records the song only", async ({ page }) => {
    await page.setViewportSize({ width: 1800, height: 1020 });
    await openStudio(page);
    await openStreamer(page);
    await page.selectOption("#streamerAudioSrc", "none");
    await expect(sm(page, ".sm-dev-state").nth(1)).toContainText("song only");
  });
});

// The fix pass: the song is never trashed while Streamer is open, focus, a
// take's protections, the export at the recorded shape, no 4K.
test.describe("Streamer Mode fix pass", () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1800, height: 1020 });
  });

  test("Delete and Backspace never trash the song while Streamer is open, wherever focus is", async ({ page }) => {
    await openStudio(page);
    await openStreamer(page);
    // Focus moved into the view on open.
    expect(await ev(page, () => document.getElementById("streamer").contains(document.activeElement))).toBe(true);
    for (const target of ["#modeBtn", `#catalogList .cat-item[data-id="${JOB_ID}"]`]) {
      await page.locator(target).first().focus();
      await page.keyboard.press("Delete");
      await page.keyboard.press("Backspace");
    }
    await page.waitForTimeout(400);
    await expect(page.locator(`#catalogList .cat-item[data-id="${JOB_ID}"]`).first()).not.toHaveClass(/in-trash/);
    await closeStreamer(page);
    // Focus is back on what opened it: the Mode menu's item is gone with the
    // menu, so the Mode button.
    await expect(page.locator("#modeBtn")).toBeFocused();
  });

  test("no 4K: 720p and 1080p only, and settings saved at 4K open as 1080p", async ({ page }) => {
    await openStudio(page);
    await ev(page, async () => {
      const { storeSet } = await import("/js/utils.js");
      const { SETTINGS_KEY, defaultRecordingSettings } = await import("/js/streamer/outputSettings.js");
      await storeSet(SETTINGS_KEY, { ...defaultRecordingSettings(), resolution: "2160", fps: 30 });
    });
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    await openStreamer(page);
    await expect(page.locator("#streamerRes option")).toHaveCount(2);
    expect(await page.locator("#streamerRes option").evaluateAll((os) => os.map((o) => o.value).sort())).toEqual(["1080", "720"]);
    await expect(page.locator("#streamerRes")).toHaveValue("1080");
    await expect(page.locator("#streamerFps")).toHaveValue("30");
    await expect(page.locator('#streamerFps option[value="60"]')).toBeEnabled();
    await expect(sm(page, ".sm-heavy")).toBeHidden();
    await page.locator("#streamerFps").selectOption("60");
    await expect(sm(page, ".sm-heavy")).toBeVisible();
    expect(errors).toEqual([]);
  });

  test("viewing a song's scene saves nothing", async ({ page }) => {
    await openStudio(page);
    await ev(page, (id) => localStorage.removeItem(`stemdeck:streamer:scene:${id}`), JOB_ID);
    await openStreamer(page);
    await page.waitForTimeout(1000);
    expect(await savedScene(page)).toBe(null);
  });

  test("a take is protected while it records, and exports at its recorded aspect", async ({ page }) => {
    await openStudio(page);
    await ev(page, async (id) => {
      const m = await import("/js/streamer/streamerModel.js");
      const { storeSet } = await import("/js/utils.js");
      // The whole 6 s song: every refusal below has to land while the take
      // still records (a 3 s trim ended before the last one under load).
      await storeSet(`stemdeck:streamer:scene:${id}`, m.serializeScene({ ...m.defaultScene("16:9"), trim: { in: 0, out: 6 } }));
    }, JOB_ID);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    await openStreamer(page);
    await sm(page, ".sm-rec").click();
    await expect(sm(page, ".sm-rec")).toHaveClass(/recording/, { timeout: 15000 });
    await expect(sm(page, ".sm-undo")).toBeDisabled();
    await expect(page.locator("#streamerAudioSrc")).toBeDisabled();
    // Karaoke, and leaving for Practice or Advanced, are refused while it
    // records, with the reason in the Mode menu.
    await chooseMode(page, "karaoke");
    await expect(page.locator("#karaokeNote")).toContainText("Stop recording first");
    await expect(page.locator("#modeMenu .mode-note")).toContainText("Stop recording first");
    await page.locator('#modeMenu .mode-item[data-mode="practice"]').click();
    await expect(page.locator("#modeMenu .mode-note")).toContainText("Stop the Performance take first");
    await page.keyboard.press("Escape");
    await expect(page.locator("html")).toHaveClass(/streamer-open/);
    await expect(sm(page, ".sm-rec")).toHaveClass(/recording/);
    // It stops at the trim's end; the scene then goes 9:16; the export says
    // it stays 16:9.
    await expect(sm(page, ".sm-export-go")).toBeEnabled({ timeout: 30000 });
    await sm(page, ".sm-aspect select").selectOption("9:16");
    await expect(sm(page, ".sm-export-text")).toContainText("16:9");
  });
});

// The input a take records and the take lane (fix: the drums on an audio
// interface were never recorded; the played part's lane showed the stem).
test.describe("Streamer Mode input and take lane", () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1800, height: 1020 });
  });

  // Audio Source with the studio's inputs needs a microphone: streamer-media.spec.mjs.

  test("the played part's lane is empty until a take, never the stem's wave", async ({ page }) => {
    await openStudio(page);
    await openStreamer(page);
    await expect.poll(() => page.locator("#streamerPart option").count()).toBeGreaterThan(1);
    const values = await page.locator("#streamerPart option").evaluateAll((os) => os.map((o) => o.value));
    test.skip(!values.includes("drums"), "the test song has no drums stem");
    await page.locator("#streamerPart").selectOption("drums");
    await expect(sm(page, ".sm-tl-label-stem").nth(1)).toContainText("(you)");
    await expect(sm(page, ".sm-tl-take-hint")).toBeVisible();
    const ink = await ev(page, () => {
      const c = document.querySelector("#streamer .sm-tl-lane-1");
      const d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
      for (let i = 3; i < d.length; i += 4) if (d[i] > 0) return true;
      return false;
    });
    expect(ink).toBe(false);
  });
});

// The take review window (streamer.js, reviewModel.js): the recorded take
// played beside the live preview in step with the transport, a note when
// there is nothing to play, and one window in place of two when there is no
// room for both.
test.describe("Streamer Mode take review", () => {
  const review = (page) => ev(page, async () => (await import("/js/streamer/streamer.js")).reviewState());
  const box = (page, sel) => sm(page, sel).evaluate((e) => { const r = e.getBoundingClientRect(); return { w: r.width, h: r.height, right: r.right }; });

  test("two windows side by side when there is room, one when there is not", async ({ page }) => {
    await page.setViewportSize({ width: 1800, height: 1020 });
    await openStudio(page);
    await page.locator("#sidebarCollapseBtn").click();
    await openStreamer(page);
    await expect.poll(async () => (await review(page)).tier).toMatch(/^[ab]$/);
    await expect(sm(page, ".sm-review")).toBeVisible();
    const live = await box(page, ".sm-preview");
    const rv = await box(page, ".sm-review");
    expect(Math.abs(live.w - rv.w)).toBeLessThanOrEqual(1);
    expect(Math.abs(live.h - rv.h)).toBeLessThanOrEqual(1);
    expect(live.w).toBeGreaterThanOrEqual(360);
    // Nothing beside them: the pair is centred in the builder.
    const builder = await sm(page, ".sm-builder").evaluate((e) => { const r = e.getBoundingClientRect(); return { left: r.left, right: r.right }; });
    const pair = await sm(page, ".sm-screens").evaluate((e) => { const r = e.getBoundingClientRect(); return { left: r.left, right: r.right }; });
    expect(Math.abs((pair.left - builder.left) - (builder.right - pair.right))).toBeLessThanOrEqual(2);
    // No take yet: the note says how to get one.
    await expect(sm(page, ".sm-review-note")).toHaveText("Record a take to review it here");
    // A small window: one window, the live preview, and the review hidden.
    // (Without the layers panel, 1280 x 720 has room for two windows.)
    await page.locator("#sidebarCollapseBtn").click();
    await page.setViewportSize({ width: 1024, height: 700 });
    await expect.poll(async () => (await review(page)).tier).toBe("c");
    await expect(sm(page, ".sm-preview")).toBeVisible();
    await expect(sm(page, ".sm-review")).toBeHidden();
    await expect(sm(page, ".sm-view-switch")).toBeHidden();
  });

  test("a take plays in the review window in step with the song, and Stop brings the note back", async ({ page }) => {
    await page.setViewportSize({ width: 1800, height: 1020 });
    await openStudio(page);
    await ev(page, async (id) => {
      const m = await import("/js/streamer/streamerModel.js");
      const { storeSet } = await import("/js/utils.js");
      await storeSet(`stemdeck:streamer:scene:${id}`, m.serializeScene({ ...m.defaultScene("16:9"), trim: { in: 1, out: 4 } }));
    }, JOB_ID);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    await page.locator("#sidebarCollapseBtn").click();
    await openStreamer(page);
    await sm(page, ".sm-rec").click();
    await expect(sm(page, ".sm-rec")).toHaveClass(/recording/, { timeout: 15000 });
    // While a new take records, the review waits.
    await expect.poll(async () => (await review(page)).view).toBe("recording");
    expect((await review(page)).paused).toBe(true);
    await expect(sm(page, ".sm-export-go")).toBeEnabled({ timeout: 30000 });
    // Saved: the note until Play.
    await expect.poll(async () => (await review(page)).view, { timeout: 20000 }).toBe("idle");
    await expect(sm(page, ".sm-review-note")).toHaveText("Press Play to review your take");
    await seek(page, 1.5);
    await sm(page, ".sm-play").click();
    await expect.poll(async () => (await review(page)).view).toBe("play");
    await expect.poll(async () => (await review(page)).paused, { timeout: 10000 }).toBe(false);
    await page.waitForTimeout(600);
    // The picture is the take's at the playhead, as the export places it.
    const s = await review(page);
    expect(s.src).toMatch(/\/takes\/[^/]+\/camera$/);
    expect(Math.abs(s.videoTime - s.takeTime)).toBeLessThan(0.15);
    await sm(page, ".sm-stop").click();
    await expect.poll(async () => (await review(page)).view).toBe("idle");
    await expect(sm(page, ".sm-review-note")).toBeVisible();
    // Closing the mode lets the video go.
    await closeStreamer(page);
    expect((await review(page)).hasVideo).toBe(false);
  });
});
