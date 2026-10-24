// The click sound selector and the click stem in the stems zip (#805).
//
// What can be observed from here: the selector's options come from the preset
// table, a choice reaches the live metronome and survives a reload, a switch
// while playing leaves no clicks behind, the export URLs carry the sound, and
// the zip the real backend builds has a click entry exactly as long as the
// stems when (and only when) Include click track is ticked.
//
// What cannot: whether a preset sounds right. That is a listening check.
// That the browser and the export synthesise the same samples is pinned in
// node (tests/js/click-sounds.test.mjs) and pytest (tests/test_click_sounds.py).

import { test, expect } from "@playwright/test";
import { openStudio, waitForClickTrack, exportUi } from "./helpers.mjs";

const sound = (page) => page.locator("#t-metro-sound");
const liveSound = (page) =>
  page.evaluate(async () => (await import("/js/state.js")).metronome?.getSound?.());
const choosePath = (page) => page.evaluate(() => window.__e2e.choosePath());

// Entry names and WAV frame counts of a STORED zip (the stems zip never
// compresses), read from its central directory.
function zipWavs(buf) {
  const u32 = (o) => buf.readUInt32LE(o);
  const u16 = (o) => buf.readUInt16LE(o);
  let eocd = buf.length - 22;
  while (eocd >= 0 && u32(eocd) !== 0x06054b50) eocd--;
  const count = u16(eocd + 10);
  let at = u32(eocd + 16);
  const out = {};
  for (let i = 0; i < count; i++) {
    const nameLen = u16(at + 28);
    const name = buf.toString("utf8", at + 46, at + 46 + nameLen);
    const size = u32(at + 20);
    const local = u32(at + 42);
    const data = local + 30 + u16(local + 26) + u16(local + 28);
    const wav = buf.subarray(data, data + size);
    // Walk the RIFF chunks to "fmt " and "data".
    let channels = 0, bits = 16, frames = null;
    for (let p = 12; p + 8 <= wav.length;) {
      const id = wav.toString("ascii", p, p + 4);
      const len = wav.readUInt32LE(p + 4);
      if (id === "fmt ") { channels = wav.readUInt16LE(p + 10); bits = wav.readUInt16LE(p + 22); }
      if (id === "data") { frames = len / (channels * (bits / 8)); break; }
      p += 8 + len + (len % 2);
    }
    out[name] = frames;
    at += 46 + nameLen + u16(at + 30) + u16(at + 32);
  }
  return out;
}

test.describe("click sound", () => {
  test("the selector lists the presets, Classic first and chosen by default", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page);
    await expect(sound(page)).toBeEnabled();
    await expect(sound(page)).toHaveValue("classic");
    await expect(sound(page).locator("option")).toHaveText(["Classic", "Woodblock", "Rimshot", "Soft", "Cowbell"]);
    await expect(sound(page)).toHaveAccessibleName("Click sound");
    await expect(sound(page)).toHaveAttribute("title", /sound of the click/i);
    expect(await liveSound(page)).toBe("classic");
  });

  test("a chosen sound reaches the live click and survives a reload", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page);
    // Keyboard, not a mouse pick: the select has to be reachable without one.
    await sound(page).focus();
    await sound(page).selectOption("cowbell");
    expect(await liveSound(page)).toBe("cowbell");

    await openStudio(page, { tauri: true });
    await waitForClickTrack(page);
    // Restored from the store into both the select and the new metronome.
    await expect(sound(page)).toHaveValue("cowbell");
    expect(await liveSound(page)).toBe("cowbell");
  });

  test("switching sound while the click plays leaves nothing queued behind", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page);
    await page.locator("#t-metro").click();
    await page.keyboard.press("Space");
    await page.waitForFunction(async () => (await import("/js/state.js")).metronome.pendingCount() > 0, null, { timeout: 10000 });
    // A burst of switches: each re-voices what is queued, never adds to it.
    const counts = await page.evaluate(async () => {
      const m = (await import("/js/state.js")).metronome;
      const seen = [];
      for (const id of ["woodblock", "rimshot", "soft", "cowbell", "classic", "woodblock"]) {
        const before = m.pendingCount();
        m.setSound(id);
        seen.push([before, m.pendingCount(), m.getSound()]);
      }
      return seen;
    });
    for (const [before, after] of counts) expect(after).toBe(before);
    expect(counts.at(-1)[2]).toBe("woodblock");
    // The lookahead is 250 ms at 120 BPM: a handful at most, ever.
    await page.waitForTimeout(1500);
    expect(await page.evaluate(async () => (await import("/js/state.js")).metronome.pendingCount())).toBeLessThan(6);
    await page.keyboard.press("Space");
    await expect.poll(() => page.evaluate(async () => (await import("/js/state.js")).metronome.pendingCount())).toBe(0);
  });

  test("exports carry the sound, and the zip gets an aligned click file only with the box ticked", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page);
    await sound(page).selectOption("rimshot");
    // The click options may be a popover over the footer; shut it so it does
    // not sit over the export menu.
    await page.keyboard.press("Escape");
    const ui = exportUi(page);
    const lastUrl = () => page.evaluate(() => window.__e2e.callsFor("download_to_path").at(-1).args.url);

    // Box off: the zip is the stems alone, and asks for no click.
    await ui.open();
    await ui.stems.click();
    await choosePath(page);
    let url = await lastUrl();
    expect(url).not.toContain("click");
    let names = zipWavs(Buffer.from(await (await page.request.get(url)).body()));
    expect(Object.keys(names).some((n) => /_click\.wav$/.test(n))).toBe(false);
    await page.evaluate(() => window.__e2e.finishSave());

    // Box on: the zip asks for the click with the chosen sound, and the
    // backend adds it, exactly as long as every stem.
    await ui.open();
    await page.locator("#t-export-click").check();
    await ui.stems.click();
    await choosePath(page);
    url = await lastUrl();
    expect(url).toContain("click=1");
    expect(url).toContain("click_sound=rimshot");
    names = zipWavs(Buffer.from(await (await page.request.get(url)).body()));
    const click = Object.keys(names).find((n) => /_click\.wav$/.test(n));
    expect(click).toBeTruthy();
    const stems = Object.entries(names).filter(([n]) => n !== click);
    expect(stems.length).toBeGreaterThan(0);
    for (const [, frames] of stems) expect(names[click]).toBe(frames);
    await page.evaluate(() => window.__e2e.finishSave());

    // The mix export asks for the same sound.
    await ui.open();
    await ui.mix.click();
    await choosePath(page);
    url = await lastUrl();
    expect(url).toContain("click=1");
    expect(url).toContain("click_sound=rimshot");
    await page.evaluate(() => window.__e2e.finishSave());
  });

  test("Classic exports keep the URL they always had", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page);
    await sound(page).selectOption("classic");
    await page.keyboard.press("Escape");
    const ui = exportUi(page);
    await ui.open();
    await page.locator("#t-export-click").check();
    await ui.mix.click();
    await choosePath(page);
    const url = await page.evaluate(() => window.__e2e.callsFor("download_to_path").at(-1).args.url);
    expect(url).toContain("click=1");
    expect(url).not.toContain("click_sound");
  });

  test("the Guide explains the selector", async ({ page }) => {
    // Wide enough for the click options to sit inline: in help mode a press
    // on the disclosure explains it rather than opening the popover.
    await page.setViewportSize({ width: 2200, height: 1000 });
    await openStudio(page);
    await waitForClickTrack(page);
    await page.locator("#helpModeBtn").click();
    await expect(page.locator("body")).toHaveClass(/help-mode/);
    await sound(page).hover({ force: true });
    await expect(page.locator("#helpTip")).toContainText("Click sound");
    await expect(page.locator("#helpTip")).toContainText("Woodblock");
  });
});
