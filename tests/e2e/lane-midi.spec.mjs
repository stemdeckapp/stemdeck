// The MIDI chip on the Bass lane and its row (static/js/laneMidiRows.js,
// Advanced mode), and the Export menu's Include MIDI, on the real backend with
// the transcription routes answered here.
//
// A real transcription would run Basic Pitch, so GET /api/transcribe/status,
// the Transcription fields of GET /api/settings, POST .../midi, its cancel and
// DELETE are answered by a small fake that also rewrites what
// GET /api/jobs/<fixture> says (`midi`). Nothing writes to the shared backend.
// The fixture song has no Piano stem, so only the Bass lane is driven here;
// the rules for both lanes are pinned in tests/js/lane-midi-view.test.mjs.
//
// 1. The chip sits under the Model chip on Bass only, and adding it changes
//    no lane row's height and no column's place (measured before and after).
// 2. Transcribe sends the stem, shows the percent and Cancel, then the note
//    count, a Download link with the .mid URL and the song's name, Re-run
//    and Remove; the Export menu then offers Include MIDI.
// 3. Cancel sends the cancel, and the row says no file was made.
// 4. Remove asks for a second press, then deletes the file.
// 5. A failed run says why in words and offers Retry; a refusal is said too.
// 6. Off in Settings hides the chip at once, without a reload.
// 7. An engine that cannot run here: the chip is dimmed, says why, opens nothing.
// 8. Practice has no MIDI chip.
// 9. Include MIDI is hidden without a MIDI file, off by default, and adds
//    midi=1 to the stems zip when ticked.
// 10. The Guide explains the chip and the row's controls (help-mode.spec
//     covers Include MIDI and the Transcription tab).

import { test, expect } from "@playwright/test";
import { openStudio, exportUi, JOB_ID } from "./helpers.mjs";

test.use({ viewport: { width: 1600, height: 900 } });

const STATUS = {
  available: true, reason: null, stems: ["piano", "bass"], max_seconds: 1200, details: ["balanced", "detailed", "clean"],
  engines: [{ id: "basic_pitch", label: "Basic Pitch", version: "0.4.0", licence: "Apache-2.0" }],
};
const DONE = {
  state: "done", pct: 100, notes: 312, engine: "basic_pitch", detail: "balanced", tempo_source: "song",
  at: "2026-11-25T10:00:00Z", error_code: null, has_file: true,
};

/**
 * The fake. `midi` is the song's `midi`; `settings` the Transcription fields;
 * `status` the engine's answer; `refuse` answers POST .../midi with that
 * { status, code } instead of starting a run. `calls` records every request.
 */
async function stubServer(page, { midi = {}, settings = {}, status = STATUS, refuse = null } = {}) {
  const state = {
    midi: { ...midi },
    settings: { transcribe_piano: "auto", transcribe_bass: "auto", transcribe_detail: "balanced", ...settings },
    calls: [],
    // Inside the 6 s fixture song: three bass notes.
    notes: [[1, 1.5, 40, 127], [2, 3, 45, 64], [4, 4.5, 52, 100]],
  };
  await page.route("**/api/transcribe/status", (route) => route.fulfill({ json: status }));
  await page.route("**/api/settings", async (route) => {
    const req = route.request();
    if (req.method() === "POST") {
      const body = JSON.parse(req.postData() || "{}");
      const mine = Object.keys(body).filter((k) => k.startsWith("transcribe_") && k !== "transcribe_lyrics");
      if (!mine.length) return route.continue();
      state.calls.push({ method: "POST", path: "/api/settings", body });
      Object.assign(state.settings, body);
      const res = await page.request.get("/api/settings");
      return route.fulfill({ json: { ...(await res.json()), ...state.settings } });
    }
    const res = await route.fetch();
    return route.fulfill({ response: res, json: { ...(await res.json()), ...state.settings } });
  });
  await page.route(new RegExp(`/api/jobs/${JOB_ID}$`), async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const res = await route.fetch();
    return route.fulfill({ response: res, json: { ...(await res.json()), midi: state.midi } });
  });
  await page.route(new RegExp(`/api/jobs/${JOB_ID}/midi(/(piano|bass)(/cancel)?)?$`), async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const body = req.postData() ? JSON.parse(req.postData()) : null;
    state.calls.push({ method: req.method(), path, body });
    if (req.method() === "DELETE") {
      const stem = path.split("/")[5];
      delete state.midi[stem];
      return route.fulfill({ json: { ok: true } });
    }
    if (path.endsWith("/cancel")) {
      const stem = path.split("/")[5];
      if (state.midi[stem]) state.midi[stem] = { ...state.midi[stem], state: "cancelled" };
      return route.fulfill({ json: { state: "cancelled" } });
    }
    if (refuse) return route.fulfill({ status: refuse.status, json: { detail: "refused", code: refuse.code } });
    const prev = state.midi[body.stem] || {};
    state.midi[body.stem] = { ...DONE, ...prev, state: "running", pct: 42, notes: null, has_file: prev.has_file === true, error_code: null };
    return route.fulfill({ status: 202, json: { state: "running" } });
  });
  await page.route(new RegExp(`/api/jobs/${JOB_ID}/stems/(piano|bass)\\.mid`), (route) =>
    route.fulfill({ status: 200, contentType: "audio/midi", body: Buffer.from("MThd") }));
  // The notes of the file, for the piano roll and the MIDI sound.
  await page.route(new RegExp(`/api/jobs/${JOB_ID}/midi/(piano|bass)/notes`), (route) => {
    const stem = new URL(route.request().url()).pathname.split("/")[5];
    if (!state.midi[stem]?.has_file) return route.fulfill({ status: 404, json: { detail: "no", code: "no_midi" } });
    return route.fulfill({ json: { notes: state.notes, bpm: 120, tempo_source: "song" } });
  });
  return state;
}

const header = (page, lane) => page.locator(`.mixer-column .lane-header[data-stem="${lane}"]`);
const chip = (page, lane) => header(page, lane).locator(":scope > .mx-model-slot > .midi-tab");
const waveRow = (page, lane) => page.locator(`.stem-waveform-row.midi-panel-lane[data-midi-lane="${lane}"]`);
const mixRow = (page, lane) => page.locator(`.mixer-column .midi-panel-row[data-midi-lane="${lane}"]`);

async function openMidiRow(page, lane = "bass") {
  await chip(page, lane).click();
  await expect(waveRow(page, lane)).toBeVisible();
}

/** Every lane header's box and the bass header's columns, to compare. */
function measure(page) {
  return page.evaluate(() => {
    const box = (e) => { const b = e.getBoundingClientRect(); return { t: Math.round(b.top * 10) / 10, h: Math.round(b.height * 10) / 10, l: Math.round(b.left * 10) / 10, w: Math.round(b.width * 10) / 10 }; };
    const rows = [...document.querySelectorAll(".mixer-column .lane-header[data-stem]")].map((e) => ({ stem: e.dataset.stem, ...box(e) }));
    const waves = [...document.querySelectorAll(".stem-waveform-layer > .stem-waveform-row[data-stem]")].map((e) => ({ stem: e.dataset.stem, ...box(e) }));
    const bass = document.querySelector('.mixer-column .lane-header[data-stem="bass"]');
    const cols = [...bass.querySelectorAll(":scope > *")].filter((e) => e.getBoundingClientRect().width > 0).map((e) => ({ cls: e.className.split(" ")[0], ...box(e) }));
    return { rows, waves, cols };
  });
}

/** Every visible row of both columns, top to bottom (as lane-model.spec reads them). */
async function expectAligned(page, label) {
  const s = await page.evaluate(() => {
    const id = (e) => (e.dataset.midiLane ? `midi:${e.dataset.midiLane}` : e.dataset.modelLane ? `model:${e.dataset.modelLane}` : e.dataset.eqLane ? `eq:${e.dataset.eqLane}` : e.dataset.stem || e.dataset.take || "?");
    const read = (sel) => [...document.querySelectorAll(sel)]
      .filter((e) => !e.classList.contains("hidden") && e.getBoundingClientRect().height > 0)
      .map((e) => { const b = e.getBoundingClientRect(); return { id: id(e), top: b.top, h: b.height }; })
      .sort((a, b) => a.top - b.top);
    return { mix: read(".mixer-column .lane-header"), wave: read(".stem-waveform-layer > .stem-waveform-row") };
  });
  expect(s.wave.map((r) => r.id), `${label}: rows`).toEqual(s.mix.map((r) => r.id));
  const worst = Math.max(0, ...s.mix.map((r, i) => Math.max(Math.abs(r.top - s.wave[i].top), Math.abs(r.h - s.wave[i].h))));
  expect(worst, `${label}: worst drift ${worst.toFixed(2)} px`).toBeLessThanOrEqual(1);
}

/** What the Settings dialog announces after a save, as a change in Settings would. */
const announce = (page, detail) => page.evaluate((d) => {
  document.dispatchEvent(new CustomEvent("stemdeck:settings-applied", { detail: d }));
}, detail);

test("the chip sits under the Model chip on Bass only, and moves nothing", async ({ page }) => {
  await stubServer(page, { settings: { transcribe_bass: "off" } });
  await openStudio(page);
  await expect(page.locator(".midi-tab")).toHaveCount(0);
  const before = await measure(page);
  await announce(page, { transcribe_bass: "auto" });
  await expect(chip(page, "bass")).toBeVisible();
  const after = await measure(page);
  expect(after.rows, "lane header rows").toEqual(before.rows);
  expect(after.waves, "waveform rows").toEqual(before.waves);
  expect(after.cols, "the bass header's columns").toEqual(before.cols);
  // Bass only: the other lanes have no chip.
  for (const lane of ["vocals", "drums", "other"]) await expect(chip(page, lane)).toHaveCount(0);
  // Two notes, drawn in the rail's line style, not a word.
  await expect(chip(page, "bass").locator(".midi-tab-face svg")).toHaveCount(1);
  await expect(chip(page, "bass")).toHaveAttribute("aria-label", "MIDI from Bass");
  await expect(chip(page, "bass")).toHaveAttribute("title", "MIDI from Bass");
  // Under the Model chip, the two cells filling the column.
  const slot = await header(page, "bass").locator(":scope > .mx-model-slot").boundingBox();
  const model = await header(page, "bass").locator(":scope > .mx-model-slot > .model-tab").boundingBox();
  const midi = await chip(page, "bass").boundingBox();
  expect(midi.y).toBeGreaterThanOrEqual(model.y + model.height - 0.5);
  expect(Math.abs(model.height + midi.height - slot.height)).toBeLessThanOrEqual(1);
  expect(Math.abs(midi.width - slot.width)).toBeLessThanOrEqual(1);
});

test("Transcribe shows the percent and Cancel, then the notes, Download, Re-run and Remove", async ({ page }) => {
  const server = await stubServer(page);
  await openStudio(page);
  await openMidiRow(page);
  await expect(chip(page, "bass")).toHaveAttribute("aria-expanded", "true");
  await expect(mixRow(page, "bass").locator(".eq-side-lane")).toHaveText("Bass");
  await expectAligned(page, "MIDI row open");
  const row = waveRow(page, "bass");
  await expect(row.locator(".midi-status")).toHaveText("Turns Bass into MIDI notes that a music program can open.");
  await expect(row.locator(".midi-transcribe")).toBeVisible();
  await expect(row.locator(".midi-download, .midi-rerun, .midi-remove, .midi-cancel")).toHaveCount(4);
  await expect(row.locator(".midi-download")).toBeHidden();
  await expect(exportUi(page).panel.locator("#t-export-midi-wrap")).toBeHidden();

  await row.locator(".midi-transcribe").click();
  await expect.poll(() => server.calls.find((c) => c.method === "POST" && c.path.endsWith("/midi"))?.body).toEqual({ stem: "bass" });
  await expect(row.locator(".midi-pct")).toHaveText("42%");
  await expect(row.locator(".midi-cancel")).toBeVisible();
  await expect(row.locator(".midi-transcribe")).toBeHidden();
  await expect(row.locator(".midi-status")).toHaveText("Transcribing Bass on this computer...");
  await expect(chip(page, "bass").locator(".midi-tab-mark")).toHaveText("42%");

  server.midi.bass = { ...DONE };
  await expect(row.locator(".midi-status")).toHaveText("312 notes from Bass.");
  await expect(row.locator(".midi-hint")).toHaveText("Detail: Balanced. Timed to the song's tempo.");
  const dl = row.locator(".midi-download");
  await expect(dl).toBeVisible();
  await expect(dl).toHaveText("Download .mid");
  await expect(dl).toHaveAttribute("href", `/api/jobs/${JOB_ID}/stems/bass.mid?v=2026-11-25T10%3A00%3A00Z`);
  await expect(dl).toHaveAttribute("download", "E2E_Fixture_Track_bass.mid");
  await expect(row.locator(".midi-rerun")).toBeVisible();
  await expect(row.locator(".midi-remove")).toBeVisible();
  await expect(row.locator(".midi-cancel")).toBeHidden();
  await expect(chip(page, "bass")).toHaveAttribute("data-mark", "done");
  await expect(chip(page, "bass")).toHaveAttribute("aria-label", "MIDI from Bass: a file is ready");

  // A re-run keeps the earlier file's Download while it runs.
  await row.locator(".midi-rerun").click();
  await expect(row.locator(".midi-cancel")).toBeVisible();
  await expect(dl).toBeVisible();
  await expect(row.locator(".midi-status")).toContainText("The earlier MIDI file stays until the new one is done.");
  await expectAligned(page, "MIDI row while it runs");
  // Closing the row takes it out of both columns.
  await chip(page, "bass").click();
  await expect(waveRow(page, "bass")).toHaveCount(0);
  await expect(mixRow(page, "bass")).toHaveCount(0);
  await expectAligned(page, "MIDI row closed");
});

test("Cancel sends the cancel, and the row says no file was made", async ({ page }) => {
  const server = await stubServer(page, { midi: { bass: { ...DONE, state: "running", pct: 10, notes: null, has_file: false } } });
  await openStudio(page);
  await openMidiRow(page);
  const row = waveRow(page, "bass");
  await expect(row.locator(".midi-pct")).toHaveText("10%");
  await row.locator(".midi-cancel").click();
  await expect.poll(() => server.calls.some((c) => c.method === "POST" && c.path.endsWith("/midi/bass/cancel"))).toBe(true);
  await expect(row.locator(".midi-status")).toHaveText("Stopped. No MIDI file was made.");
  await expect(row.locator(".midi-transcribe")).toBeVisible();
});

test("Remove asks for a second press, then deletes the file", async ({ page }) => {
  const server = await stubServer(page, { midi: { bass: { ...DONE } } });
  await openStudio(page);
  await openMidiRow(page);
  const row = waveRow(page, "bass");
  const remove = row.locator(".midi-remove");
  await remove.click();
  await expect(remove).toHaveText("Press again to delete the MIDI file");
  await expect(remove).toHaveClass(/armed/);
  expect(server.calls.some((c) => c.method === "DELETE")).toBe(false);
  await remove.click();
  await expect.poll(() => server.calls.find((c) => c.method === "DELETE")?.path).toBe(`/api/jobs/${JOB_ID}/midi/bass`);
  await expect(row.locator(".midi-status")).toHaveText("The MIDI file was deleted.");
  await expect(row.locator(".midi-transcribe")).toBeVisible();
  await expect(row.locator(".midi-download")).toBeHidden();
});

test("a failed run says why and offers Retry; a refusal is said in words", async ({ page }) => {
  const server = await stubServer(page, {
    midi: { bass: { ...DONE, state: "error", error_code: "worker_failed", has_file: false, notes: null } },
    refuse: { status: 409, code: "busy" },
  });
  await openStudio(page);
  await expect(chip(page, "bass")).toHaveAttribute("data-mark", "error");
  await openMidiRow(page);
  const row = waveRow(page, "bass");
  await expect(row.locator(".midi-status")).toHaveText("The transcription engine stopped with an error. Try again.");
  await expect(row.locator(".midi-status")).toHaveAttribute("data-kind", "error");
  await row.locator(".midi-retry").click();
  await expect.poll(() => server.calls.filter((c) => c.method === "POST" && c.path.endsWith("/midi")).length).toBe(1);
  await expect(row.locator(".midi-status")).toHaveText("Another job is running on this song. Try again when it finishes.");
});

test("Off in Settings hides the chip at once", async ({ page }) => {
  const server = await stubServer(page);
  await openStudio(page);
  await expect(chip(page, "bass")).toBeVisible();
  await page.locator("#settingsBtn").click();
  await page.locator('.settings-tab[data-tab="transcription"]').click();
  await page.locator(".set-transcribe-bass").selectOption("off");
  await expect.poll(() => server.calls.find((c) => c.path === "/api/settings")?.body).toEqual({ transcribe_bass: "off" });
  await expect(chip(page, "bass")).toHaveCount(0);
  await page.locator(".set-transcribe-bass").selectOption("auto");
  await expect(chip(page, "bass")).toHaveCount(1);
});

test("an engine that cannot run here: the chip is dimmed, says why, and opens nothing", async ({ page }) => {
  await stubServer(page, { status: { ...STATUS, available: false, reason: "unsupported_platform" } });
  await openStudio(page);
  await expect(chip(page, "bass")).toBeVisible();
  await expect(chip(page, "bass")).toHaveClass(/off/);
  await expect(chip(page, "bass")).toHaveAttribute("title", "Transcription is not available on this computer (Intel Mac).");
  await expect(chip(page, "bass")).toHaveAttribute("aria-disabled", "true");
  await chip(page, "bass").click();
  await expect(waveRow(page, "bass")).toHaveCount(0);
});

test("Practice has no MIDI chip", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("stemdeck.mode", "practice"));
  await stubServer(page, { midi: { bass: { ...DONE } } });
  await openStudio(page);
  await expect(page.locator(".midi-tab")).toHaveCount(0);
});

test("Include MIDI shows only with a MIDI file, is off by default, and adds midi=1", async ({ page }) => {
  await page.route(/\/api\/jobs\/[^/]+\/stems\/all\.zip/, (route) =>
    route.fulfill({ status: 200, contentType: "application/zip", headers: { "Content-Disposition": "attachment" }, body: Buffer.from("PK") }));
  const server = await stubServer(page, { midi: { bass: { ...DONE } } });
  await openStudio(page);
  const ui = exportUi(page);
  const wrap = page.locator("#t-export-midi-wrap");
  const box = page.locator("#t-export-midi");
  await ui.open();
  await expect(wrap).toBeVisible();
  await expect(wrap).toContainText("Include MIDI");
  await expect(box).not.toBeChecked();
  let req = page.waitForRequest(/\/stems\/all\.zip/);
  await ui.stems.click();
  expect(new URL((await req).url()).searchParams.get("midi")).toBeNull();

  await ui.open();
  await box.check();
  req = page.waitForRequest(/\/stems\/all\.zip/);
  await ui.stems.click();
  expect(new URL((await req).url()).searchParams.get("midi")).toBe("1");
  // Kept for the next visit.
  expect(await page.evaluate(() => localStorage.getItem("stemdeck.exportMidi"))).toBe("1");
  // The fixture song has no video, so the MP4 case (the switch hides with
  // the stems rows, daw.css #t-export-panel.fmt-mp4) is not driven here.
  expect(server.calls.filter((c) => c.method !== "GET")).toEqual([]);
});

test("Include MIDI is not offered for a song without a MIDI file", async ({ page }) => {
  await stubServer(page, { midi: { bass: { ...DONE, state: "running", pct: 5, notes: null, has_file: false } } });
  await openStudio(page);
  await exportUi(page).open();
  await expect(page.locator("#t-export-midi-wrap")).toBeHidden();
});

test("the Guide explains the chip and the row's controls", async ({ page }) => {
  await stubServer(page, { midi: { bass: { ...DONE } } });
  await openStudio(page);
  await openMidiRow(page);
  await page.locator("#helpModeBtn").click();
  await expect(page.locator("body")).toHaveClass(/help-mode/);
  const tip = page.locator("#helpTip");
  const row = waveRow(page, "bass");
  for (const [loc, words] of [
    [chip(page, "bass"), "the notes icon under the microchip"],
    [row.locator(".midi-download"), "as a .mid file"],
    [row.locator(".midi-rerun"), "Transcribes the lane again"],
    [row.locator(".midi-remove"), "Deletes the lane's MIDI file"],
  ]) {
    await loc.hover();
    await expect(tip).toBeVisible();
    await expect(tip).toContainText(words);
  }
});

/** The heard playhead's x and a roll note's left edge, after a seek to `t`. */
async function noteAndPlayhead(page, index, t) {
  return page.evaluate(async ([i, at]) => {
    const st = await import("/js/state.js");
    st.audioEngine.seek(at);
    await new Promise((r) => setTimeout(r, 300));
    const rect = document.querySelectorAll('.midi-roll[data-midi-lane="bass"] rect')[i].getBoundingClientRect();
    const line = document.querySelector(".lane-playhead-line").getBoundingClientRect();
    return { note: rect.left, playhead: line.left + line.width / 2 };
  }, [index, t]);
}

test("the notes icon opens the row and draws the notes over the waveform, on its timeline", async ({ page }) => {
  await stubServer(page, { midi: { bass: { ...DONE } } });
  await openStudio(page);
  await openMidiRow(page);
  await expect(chip(page, "bass")).toHaveClass(/viewing/);
  const row = page.locator('.stem-waveform-layer > .stem-waveform-row[data-stem="bass"]');
  const roll = row.locator(':scope > svg.midi-roll[data-midi-lane="bass"]');
  await expect(roll).toHaveCount(1);
  await expect(row).toHaveClass(/midi-view/);
  await expect(roll.locator("rect")).toHaveCount(3);
  // Highest note at the top, a harder note stronger.
  const ys = await roll.locator("rect").evaluateAll((els) => els.map((e) => Number(e.getAttribute("y"))));
  expect(ys[2]).toBeLessThan(ys[1]);
  expect(ys[1]).toBeLessThan(ys[0]);
  await expect(roll.locator("rect").first()).toHaveAttribute("fill-opacity", "1");
  // The waveform is still there, under it.
  await expect(row.locator(":scope > .stem-waveform-svg")).toHaveCount(1);
  for (const [i, t] of [[0, 1], [1, 2], [2, 4]]) {
    const at = await noteAndPlayhead(page, i, t);
    expect(Math.abs(at.note - at.playhead), `note ${i} at zoom 1`).toBeLessThanOrEqual(1.5);
  }
  // Zoomed in, the lanes are redrawn and the roll comes back, still in line.
  await page.evaluate(async () => (await import("/js/transport.js")).setWaveZoomLevel(4));
  await expect(roll).toHaveCount(1);
  const at = await noteAndPlayhead(page, 1, 2);
  expect(Math.abs(at.note - at.playhead), "note 1 at zoom 4").toBeLessThanOrEqual(1.5);
  // Closing the row takes the roll away.
  await chip(page, "bass").click();
  await expect(roll).toHaveCount(0);
  await expect(chip(page, "bass")).not.toHaveClass(/viewing/);
});

test("Stem, MIDI and Both switch what the lane plays", async ({ page }) => {
  await stubServer(page, { midi: { bass: { ...DONE } } });
  await openStudio(page);
  await openMidiRow(page);
  const source = waveRow(page, "bass").locator(".midi-source");
  await expect(source).toBeVisible();
  await expect(source.locator(".midi-source-btn")).toHaveText(["Stem", "MIDI", "Both"]);
  await expect(source.locator('[data-source="stem"]')).toHaveAttribute("aria-pressed", "true");
  const tracks = () => page.evaluate(async () => (await import("/js/state.js")).audioEngine.getAnalysers("bass").length);
  expect(await tracks()).toBe(1);
  await source.locator('[data-source="midi"]').click();
  await expect(source.locator('[data-source="midi"]')).toHaveAttribute("aria-pressed", "true");
  // The MIDI sound is added to the lane, in the engine, beside the stem.
  await expect.poll(tracks).toBe(2);
  await source.locator('[data-source="both"]').click();
  await expect(source.locator('[data-source="both"]')).toHaveAttribute("aria-pressed", "true");
  await source.locator('[data-source="stem"]').click();
  await expect(source.locator('[data-source="stem"]')).toHaveAttribute("aria-pressed", "true");
  // The visuals still read the stems' buffers only.
  const keys = await page.evaluate(async () => [...(await import("/js/state.js")).audioEngine.getBuffers().keys()]);
  expect(keys.filter((k) => k.startsWith("bass"))).toEqual(["bass"]);
});

test("without a MIDI file the row has no source switch", async ({ page }) => {
  await stubServer(page);
  await openStudio(page);
  await openMidiRow(page);
  await expect(waveRow(page, "bass").locator(".midi-source")).toBeHidden();
});

/** Every download link click, recorded and not followed. */
async function recordDownloads(page) {
  await page.evaluate(() => {
    window.__dl = [];
    HTMLAnchorElement.prototype.click = function click() {
      window.__dl.push({ href: this.getAttribute("href"), download: this.download });
    };
  });
  return () => page.evaluate(() => window.__dl);
}

test("the lane's download button offers Audio, MIDI, or Audio and MIDI", async ({ page }) => {
  await stubServer(page, { midi: { bass: { ...DONE } } });
  await openStudio(page);
  const dl = header(page, "bass").locator("a.lane-dl");
  await expect(dl).toHaveAttribute("aria-haspopup", "menu");
  await expect(header(page, "vocals").locator("a.lane-dl")).not.toHaveAttribute("aria-haspopup", /.+/);
  const downloads = await recordDownloads(page);
  await dl.click();
  const menu = page.locator(".lane-dl-menu");
  await expect(menu).toBeVisible();
  await expect(menu.locator(".lane-dl-opt")).toHaveText(["Audio", "MIDI", "Audio and MIDI"]);
  await expect(menu.locator('.lane-dl-opt[data-choice="audio"]')).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(menu.locator('.lane-dl-opt[data-choice="midi"]')).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(menu).toHaveCount(0);
  await expect(dl).toBeFocused();
  for (const choice of ["audio", "midi", "both"]) {
    await dl.click();
    await menu.locator(`.lane-dl-opt[data-choice="${choice}"]`).click();
  }
  const got = await downloads();
  expect(got.map((d) => d.href)).toEqual([
    await dl.getAttribute("href"),
    `/api/jobs/${JOB_ID}/stems/bass.mid?v=2026-11-25T10%3A00%3A00Z`,
    `/api/jobs/${JOB_ID}/stems/all.zip?format=wav&stems=bass&midi=1`,
  ]);
  expect(got[1].download).toBe("E2E_Fixture_Track_bass.mid");
  expect(got[2].download).toBe("E2E_Fixture_Track_bass_audio_midi.zip");
});

test("without a MIDI file the download button is the plain download", async ({ page }) => {
  await stubServer(page);
  await openStudio(page);
  const dl = header(page, "bass").locator("a.lane-dl");
  await expect(dl).not.toHaveAttribute("aria-haspopup", /.+/);
  const downloads = await recordDownloads(page);
  await dl.click();
  await expect(page.locator(".lane-dl-menu")).toHaveCount(0);
  expect((await downloads()).length).toBe(1);
});

test("the Guide explains the icon, the source switch, the roll and the download choice", async ({ page }) => {
  await stubServer(page, { midi: { bass: { ...DONE } } });
  await openStudio(page);
  await openMidiRow(page);
  await expect(page.locator(".midi-roll")).toHaveCount(1);
  await page.locator("#helpModeBtn").click();
  await expect(page.locator("body")).toHaveClass(/help-mode/);
  const tip = page.locator("#helpTip");
  for (const [loc, words] of [
    [chip(page, "bass"), "the notes icon under the microchip"],
    [waveRow(page, "bass").locator('.midi-source-btn[data-source="midi"]'), "What the lane plays"],
    [page.locator('.midi-roll[data-midi-lane="bass"]'), "on the same timeline"],
    [header(page, "bass").locator("a.lane-dl"), "asks what to save"],
  ]) {
    await loc.hover({ force: true });
    await expect(tip).toBeVisible();
    await expect(tip).toContainText(words);
  }
});
