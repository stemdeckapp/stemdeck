// The Model tab on a lane and its row (static/js/laneModelRows.js, Advanced
// mode), on the real backend with the re-extract routes answered here.
//
// The suite's backend has whatever device and models the runner has, and a
// real re-extract would run a separation, so /api/settings carries the
// per-stem model fields of a GPU machine, and POST .../reextract, .../undo and
// .../cancel are answered by a small fake that also rewrites what
// GET /api/jobs/<fixture> says about the run (stem_models, stem_undo,
// stem_reextract). Nothing writes to the shared backend. The words are pinned
// in tests/js/lane-model-view.test.mjs.
//
// 1. Each lane header reads Key | chip | rail: the chip's column sits
//    between the key and the rail on every lane, the rail keeps its four
//    cells, the chip is in it on Vocals, Drums and Bass and not on Other;
//    its name is "Model: <the song's model>", and its dot follows the
//    Settings default. Nothing about the model is in the EQ row. The left
//    column is --daw-col-w wide, and the label cell over the lanes too.
// 2. A click opens the row under the lane in both columns, aligned, with
//    the Settings list for the stem, starting at the song's own model. It
//    goes after the lane's EQ row when that is open, and opening or closing
//    the EQ row leaves it open.
// 3. Re-extract sends the stem and the model, shows the progress and a
//    Cancel, and on done the song reloads with the lane's volume kept.
// 4. Cancel sends the cancel and the row says the stem is as it was.
// 5. Undo and Keep show for an undo copy. Undo sends it; Keep asks for a
//    second press, then sends it, and goes back to Keep untouched after 3 s.
// 6. A song with no source: the row says why, and Re-extract goes off.
// 7. Practice: no chip and no chip column; the left column is 300 px.
// 8. The Guide explains the tab and the row's controls.
// 9. A Vocals re-extract on a song with Lead + Backing says the pair was
//    set aside.

import { test, expect } from "@playwright/test";
import { openStudio, JOB_ID } from "./helpers.mjs";

test.use({ viewport: { width: 1600, height: 900 } });

const STEMS5 = ["vocals", "drums", "bass", "guitar", "piano"];
const MODELS = {
  htdemucs_6s: { name: "HTDemucs 6s", license: "MIT", available: true, downloaded: true },
  htdemucs_ft: { name: "HTDemucs FT", license: "MIT", available: true, downloaded: false },
  kim_mel_roformer: { name: "Kim Mel-RoFormer", license: "MIT", available: true, downloaded: false },
};
const CATALOG_STEMS = [
  { stem: "vocals", choices: ["htdemucs_6s", "kim_mel_roformer"] },
  { stem: "lead_backing", fixed: "uvr_karaoke_2", available: true },
  { stem: "drums", choices: ["htdemucs_6s", "htdemucs_ft"] },
  { stem: "bass", choices: ["htdemucs_6s", "htdemucs_ft"] },
  { stem: "guitar", choices: ["htdemucs_6s"] },
  { stem: "piano", choices: ["htdemucs_6s"] },
  { stem: "other", fixed: "residual" },
];

function settingsFields(saved) {
  const recommended = { ...Object.fromEntries(STEMS5.map((s) => [s, "htdemucs_6s"])), vocals: "kim_mel_roformer" };
  const resolved = Object.fromEntries(STEMS5.map((s) => [s, saved[s] && saved[s] !== "auto" ? saved[s] : recommended[s]]));
  return {
    demucs_device_resolved: "cuda",
    separation_quality: "standard",
    stem_models: { ...Object.fromEntries(STEMS5.map((s) => [s, "auto"])), ...saved },
    stem_models_resolved: resolved,
    stem_models_recommended: recommended,
    stem_models_reason: "gpu",
    stem_model_catalog: { stems: CATALOG_STEMS, models: MODELS, min_vram_mb: 4096 },
  };
}

/**
 * The fake: `song` is what GET /api/jobs/<fixture> adds to the real state;
 * `posts` records every POST the row sends. `refuse` answers a re-extract
 * with that { status, code } instead of starting it.
 */
async function stubServer(page, { saved = {}, song = {}, refuse = null } = {}) {
  const state = {
    song: {
      stem_models: { vocals: "kim_mel_roformer", drums: "htdemucs_6s", bass: "htdemucs_6s", guitar: "htdemucs_6s", piano: "htdemucs_6s" },
      stem_undo: {},
      stem_reextract: null,
      ...song,
    },
    posts: [],
  };
  await page.route(/\/api\/my-models(\/.*)?$/, (route) => route.fulfill({ status: 404, json: { detail: "Not Found" } }));
  await page.route("**/api/settings", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const res = await route.fetch();
    return route.fulfill({ response: res, json: { ...(await res.json()), ...settingsFields(saved) } });
  });
  await page.route(`**/api/jobs/${JOB_ID}`, async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const res = await route.fetch();
    return route.fulfill({ response: res, json: { ...(await res.json()), ...state.song } });
  });
  await page.route(new RegExp(`/api/jobs/${JOB_ID}/(reextract(/undo|/keep)?|cancel)$`), async (route) => {
    const url = new URL(route.request().url());
    const body = JSON.parse(route.request().postData() || "{}");
    state.posts.push({ path: url.pathname, body });
    if (url.pathname.endsWith("/reextract")) {
      if (refuse) return route.fulfill({ status: refuse.status, json: { detail: "refused", code: refuse.code } });
      state.song.stem_reextract = { stem: body.stem, model: body.model, action: "reextract", state: "running", phase: "separating", progress: 0.42, code: null, used_model: null, fallback: null, gain_db: null };
      return route.fulfill({ status: 202, json: { state: { job_id: JOB_ID, ...state.song } } });
    }
    if (url.pathname.endsWith("/undo")) {
      const was = state.song.stem_undo[body.stem]?.model || "htdemucs_6s";
      state.song.stem_models = { ...state.song.stem_models, [body.stem]: was };
      state.song.stem_undo = {};
      state.song.stem_reextract = { stem: body.stem, model: was, action: "undo", state: "done", phase: null, progress: 1, code: null, used_model: was, fallback: null, gain_db: null };
      return route.fulfill({ json: { state: { job_id: JOB_ID, ...state.song } } });
    }
    if (url.pathname.endsWith("/keep")) {
      const undo = { ...state.song.stem_undo };
      delete undo[body.stem];
      state.song.stem_undo = undo;
      return route.fulfill({ json: { state: { job_id: JOB_ID, ...state.song } } });
    }
    // cancel
    if (state.song.stem_reextract) state.song.stem_reextract = { ...state.song.stem_reextract, state: "cancelled", phase: null };
    return route.fulfill({ json: { status: "cancelled" } });
  });
  return state;
}

/** End the fake run as done: the stem is now made with `model`. */
function finishRun(state, model) {
  const run = state.song.stem_reextract;
  state.song.stem_undo = { [run.stem]: { model: state.song.stem_models[run.stem], saved_at: 1764000000 } };
  state.song.stem_models = { ...state.song.stem_models, [run.stem]: model };
  state.song.stem_reextract = { ...run, state: "done", phase: null, progress: 1, used_model: model };
  // A Vocals re-extract sets the Lead + Backing pair aside (the contract).
  if (run.stem === "vocals") state.song.vocal_split = "none";
}

const eqTab = (page, lane) => page.locator(`.mx-rail .eq-tab[data-eq-lane="${lane}"]`);
const tab = (page, lane) => page.locator(`.mixer-column .lane-header[data-stem="${lane}"] > .mx-model-slot > .model-tab`);

/** Open the lane's model row with its chip. */
async function openModelRow(page, lane) {
  await tab(page, lane).click();
  await expect(waveRow(page, lane)).toBeVisible();
}
const waveRow = (page, lane) => page.locator(`.stem-waveform-row.model-panel-lane[data-model-lane="${lane}"]`);
const mixRow = (page, lane) => page.locator(`.mixer-column .model-panel-row[data-model-lane="${lane}"]`);

/** Every visible row of both columns, top to bottom (as eq-panels.spec reads them). */
async function expectAligned(page, label) {
  const s = await page.evaluate(() => {
    const id = (e) => (e.dataset.modelLane ? `model:${e.dataset.modelLane}` : e.dataset.eqLane ? `eq:${e.dataset.eqLane}` : e.dataset.stem || e.dataset.take || "?");
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

test("each lane header reads Key, chip, rail; the chip is on the stems with a model", async ({ page }) => {
  await stubServer(page, { saved: { drums: "htdemucs_ft" } });
  await openStudio(page);
  const railXs = new Set();
  for (const lane of ["vocals", "drums", "bass", "other"]) {
    const row = page.locator(`.mixer-column .lane-header[data-stem="${lane}"]`);
    // Key | chip | rail, side by side, the chip's column on every lane.
    const cols = await row.locator(":scope > .mx-keyrail, :scope > .mx-model-slot, :scope > .mx-rail").evaluateAll((els) => els.map((e) => {
      const b = e.getBoundingClientRect();
      return { cls: e.className.split(" ")[0], l: b.left, r: b.right, t: b.top, h: b.height };
    }));
    expect(cols.map((c) => c.cls)).toEqual(["mx-keyrail", "mx-model-slot", "mx-rail"]);
    expect(cols[1].l).toBeGreaterThanOrEqual(cols[0].r - 0.5);
    expect(cols[2].l).toBeGreaterThanOrEqual(cols[1].r - 0.5);
    expect(cols[1].r - cols[1].l).toBeGreaterThanOrEqual(22);
    railXs.add(Math.round(cols[2].l));
    // The rail keeps its four cells: M, S, EQ, download.
    const rail = await row.locator(".mx-rail > *").evaluateAll((els) => els.map((e) => e.className.split(" ")[0]));
    expect(rail).toEqual(["lane-icon-toggle", "solo", "mx-eq-slot", "lane-dl"]);
  }
  expect([...railXs]).toHaveLength(1);
  await expect(page.locator(".mx-rail .model-tab")).toHaveCount(0);
  await expect(page.locator('.mixer-column .lane-header[data-stem="other"] .model-tab')).toHaveCount(0);
  // The left column and the label cell over the lanes are --daw-col-w wide.
  const widths = await page.evaluate(() => ({
    v: parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--daw-col-w")),
    col: document.querySelector(".daw-stems-panel").getBoundingClientRect().width,
    label: document.querySelector(".daw-wave-label").getBoundingClientRect().width,
  }));
  expect(widths.v).toBe(334);
  expect(Math.abs(widths.col - widths.v)).toBeLessThanOrEqual(1);
  expect(Math.abs(widths.label - widths.v)).toBeLessThanOrEqual(1);
  for (const lane of ["vocals", "drums", "bass"]) {
    await expect(tab(page, lane)).toBeVisible();
    await expect(tab(page, lane).locator("svg")).toHaveCount(1);
    // One cell the full height of the row, the chip centred in it.
    const slot = await page.locator(`.mixer-column .lane-header[data-stem="${lane}"] > .mx-model-slot`).boundingBox();
    const box = await tab(page, lane).boundingBox();
    expect(Math.abs(box.height - slot.height)).toBeLessThanOrEqual(1);
    const svg = await tab(page, lane).locator("svg").boundingBox();
    expect(svg.height).toBeGreaterThanOrEqual(17);
  }
  await expect(tab(page, "vocals")).toHaveAttribute("aria-label", "Model: Kim Mel-RoFormer");
  await expect(tab(page, "vocals")).toHaveAttribute("title", "Model: Kim Mel-RoFormer");
  await expect(tab(page, "drums")).toHaveAttribute("aria-label", "Model: HTDemucs 6s");
  // Settings uses HTDemucs FT for new songs' drums; this song's were made with 6s.
  await expect(tab(page, "drums")).toHaveClass(/differs/);
  await expect(tab(page, "vocals")).not.toHaveClass(/differs/);
  await expect(tab(page, "drums").locator(".model-tab-dot")).toBeVisible();
  await expect(tab(page, "vocals").locator(".model-tab-dot")).toBeHidden();
  // The EQ is only the EQ: its row has no model control, its tab no mark.
  await eqTab(page, "drums").click();
  await expect(page.locator('.mixer-column .eq-panel-row[data-eq-lane="drums"] .model-tab')).toHaveCount(0);
  await expect(page.locator(".eq-model-slot, .eq-tab-mdot")).toHaveCount(0);
});

test("the row opens under the lane on its own, aligned, with the Settings list at the song's model", async ({ page }) => {
  await stubServer(page);
  await openStudio(page);
  await openModelRow(page, "vocals");
  await expect(tab(page, "vocals")).toHaveAttribute("aria-expanded", "true");
  await expect(mixRow(page, "vocals")).toBeVisible();
  await expect(mixRow(page, "vocals").locator(".eq-side-lane")).toHaveText("Vocals");
  const select = waveRow(page, "vocals").locator(".model-select");
  await expect(select.locator("option")).toHaveText(["HTDemucs 6s", "Kim Mel-RoFormer (Recommended)"]);
  await expect(select).toHaveValue("kim_mel_roformer");
  await expect(waveRow(page, "vocals").locator(".model-hint")).toContainText("Vocals was made with Kim Mel-RoFormer.");
  await expect(waveRow(page, "vocals").locator(".model-undo")).toBeHidden();
  await expect(page.locator('.eq-panel-lane[data-eq-lane="vocals"]')).toHaveCount(0);
  await expectAligned(page, "model row open alone");
  // The EQ row opens above it; closing the EQ row leaves it open.
  await eqTab(page, "vocals").click();
  await expect(page.locator('.eq-panel-lane[data-eq-lane="vocals"]')).toBeVisible();
  const ids = await page.locator(".stem-waveform-layer > .stem-waveform-row").evaluateAll((els) => els.map((e) => e.dataset.modelLane ? `model:${e.dataset.modelLane}` : e.dataset.eqLane ? `eq:${e.dataset.eqLane}` : e.dataset.stem));
  expect(ids.indexOf("eq:vocals")).toBe(ids.indexOf("vocals") + 1);
  expect(ids.indexOf("model:vocals")).toBe(ids.indexOf("eq:vocals") + 1);
  await expectAligned(page, "EQ and model rows open");
  await eqTab(page, "vocals").click();
  await expect(page.locator('.eq-panel-lane[data-eq-lane="vocals"]')).toHaveCount(0);
  await expect(waveRow(page, "vocals")).toBeVisible();
  await expectAligned(page, "EQ row closed, model row open");
  await tab(page, "vocals").click();
  await expect(waveRow(page, "vocals")).toHaveCount(0);
  await expectAligned(page, "model row closed");
});

test("Re-extract sends the stem and model, shows progress, and the song reloads with the mix kept", async ({ page }) => {
  const server = await stubServer(page);
  await openStudio(page);
  // A volume of the person's own on the lane, saved with the mix.
  await page.evaluate(async () => (await import("/js/mixer.js")).setLaneVolume("drums", 0.5));
  await openModelRow(page, "drums");
  const row = waveRow(page, "drums");
  await row.locator(".model-select").selectOption("htdemucs_ft");
  await row.locator(".model-reextract").click();
  await expect.poll(() => server.posts.find((p) => p.path.endsWith("/reextract"))?.body).toEqual({ stem: "drums", model: "htdemucs_ft" });
  await expect(row.locator(".model-run")).toBeVisible();
  await expect(row.locator(".model-pct")).toHaveText("42%");
  await expect(row.locator(".model-cancel")).toBeVisible();
  await expect(row.locator(".model-reextract")).toBeHidden();
  await expect(row.locator(".model-status")).toHaveText("Separating Drums again with HTDemucs FT...");
  // The other lanes cannot start one meanwhile.
  await openModelRow(page, "bass");
  await expect(waveRow(page, "bass").locator(".model-reextract")).toBeDisabled();
  await expect(waveRow(page, "bass").locator(".model-status")).toHaveText("Drums of this song is being separated again. Wait until it ends.");

  // The run ends: the page reloads the song (a new engine) and keeps its place.
  const engineBefore = await page.evaluate(async () => { const s = await import("/js/state.js"); window.__engineBefore = s.audioEngine; return true; });
  expect(engineBefore).toBe(true);
  finishRun(server, "htdemucs_ft");
  await expect.poll(() => page.evaluate(async () => (await import("/js/state.js")).audioEngine !== window.__engineBefore), { timeout: 15000 }).toBe(true);
  await expect(tab(page, "drums")).toHaveAttribute("aria-label", "Model: HTDemucs FT");
  await expect(waveRow(page, "drums").locator(".model-status")).toHaveText("Drums is now made with HTDemucs FT.");
  await expect(waveRow(page, "drums").locator(".model-undo")).toBeVisible();
  const volume = await page.evaluate(async () => (await import("/js/state.js")).mixerState.drums?.volume);
  expect(volume).toBeCloseTo(0.5, 5);
});

test("Cancel sends the cancel, and the row says the stem is as it was", async ({ page }) => {
  const server = await stubServer(page);
  await openStudio(page);
  await openModelRow(page, "bass");
  const row = waveRow(page, "bass");
  await row.locator(".model-reextract").click();
  await expect(row.locator(".model-cancel")).toBeVisible();
  await row.locator(".model-cancel").click();
  await expect.poll(() => server.posts.some((p) => p.path.endsWith("/cancel"))).toBe(true);
  await expect(row.locator(".model-status")).toHaveText("Stopped. Bass is as it was.");
  await expect(row.locator(".model-reextract")).toBeEnabled();
});

test("Undo shows for an undo copy and puts the stem back", async ({ page }) => {
  const server = await stubServer(page, {
    song: {
      stem_models: { vocals: "htdemucs_6s", drums: "htdemucs_6s", bass: "htdemucs_6s", guitar: "htdemucs_6s", piano: "htdemucs_6s" },
      stem_undo: { vocals: { model: "kim_mel_roformer", saved_at: 1764000000 } },
    },
  });
  await openStudio(page);
  await openModelRow(page, "vocals");
  const row = waveRow(page, "vocals");
  await expect(row.locator(".model-undo")).toBeVisible();
  await expect(row.locator(".model-undo")).toHaveAttribute("title", /Kim Mel-RoFormer/);
  await row.locator(".model-undo").click();
  await expect.poll(() => server.posts.find((p) => p.path.endsWith("/undo"))?.body).toEqual({ stem: "vocals" });
  await expect(tab(page, "vocals")).toHaveAttribute("aria-label", "Model: Kim Mel-RoFormer");
  await expect(waveRow(page, "vocals").locator(".model-undo")).toBeHidden();
  await expect(waveRow(page, "vocals").locator(".model-status")).toHaveText("Vocals is back as it was, made with Kim Mel-RoFormer.");
});

test("Keep asks for a second press, then discards the undo copy", async ({ page }) => {
  const server = await stubServer(page, { song: { stem_undo: { bass: { model: "htdemucs_ft", saved_at: 1764000000 } } } });
  await openStudio(page);
  await openModelRow(page, "bass");
  const row = waveRow(page, "bass");
  const keep = row.locator(".model-keep");
  await expect(keep).toBeVisible();
  await expect(keep).toHaveText("Keep");
  await expect(keep).toHaveAttribute("title", /discard the earlier one for good/);
  // The first press only arms it, and it disarms by itself.
  await keep.click();
  await expect(keep).toHaveText("Press again to discard the earlier version for good");
  await expect(keep).toHaveClass(/armed/);
  await expect(keep).toHaveText("Keep", { timeout: 5000 });
  expect(server.posts.some((p) => p.path.endsWith("/keep"))).toBe(false);
  // Two presses send it.
  await keep.click();
  await keep.click();
  await expect.poll(() => server.posts.find((p) => p.path.endsWith("/keep"))?.body).toEqual({ stem: "bass" });
  await expect(row.locator(".model-status")).toHaveText("The earlier Bass was discarded. Bass stays as it is.");
  await expect(keep).toBeHidden();
  await expect(row.locator(".model-undo")).toBeHidden();
});

test("a Vocals re-extract says the Lead + Backing pair was set aside", async ({ page }) => {
  const server = await stubServer(page, { song: { vocal_split: "done" } });
  await openStudio(page);
  await openModelRow(page, "vocals");
  const row = waveRow(page, "vocals");
  await row.locator(".model-select").selectOption("htdemucs_6s");
  await row.locator(".model-reextract").click();
  await expect(row.locator(".model-run")).toBeVisible();
  finishRun(server, "htdemucs_6s");
  await expect(waveRow(page, "vocals").locator(".model-status")).toContainText("Lead and Backing were made from the old Vocals", { timeout: 15000 });
});

test("a song with no source says why, and Re-extract stays off", async ({ page }) => {
  await stubServer(page, { refuse: { status: 409, code: "no_source" } });
  await openStudio(page);
  await openModelRow(page, "drums");
  const row = waveRow(page, "drums");
  await row.locator(".model-reextract").click();
  await expect(row.locator(".model-status")).toContainText("the original track is not kept");
  await expect(row.locator(".model-reextract")).toBeDisabled();
  // The tab stays.
  await expect(tab(page, "drums")).toBeVisible();
});

test("a busy server is said in words", async ({ page }) => {
  await stubServer(page, { refuse: { status: 409, code: "busy" } });
  await openStudio(page);
  await openModelRow(page, "bass");
  const row = waveRow(page, "bass");
  await row.locator(".model-reextract").click();
  await expect(row.locator(".model-status")).toHaveText("StemDeck is separating something else. Try again when it ends.");
  await expect(row.locator(".model-reextract")).toBeEnabled();
});

test("Practice has no Model tab", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("stemdeck.mode", "practice"));
  await stubServer(page);
  await openStudio(page);
  await expect(page.locator(".model-tab")).toHaveCount(0);
  await expect(page.locator(".mx-model-slot:visible")).toHaveCount(0);
  const col = await page.evaluate(() => document.querySelector(".daw-stems-panel").getBoundingClientRect().width);
  expect(Math.abs(col - 300)).toBeLessThanOrEqual(1);
});

test("the Guide explains the tab and the row's controls", async ({ page }) => {
  await stubServer(page, { song: { stem_undo: { drums: { model: "htdemucs_6s", saved_at: 1764000000 } } } });
  await openStudio(page);
  await openModelRow(page, "drums");
  await page.locator("#helpModeBtn").click();
  await expect(page.locator("body")).toHaveClass(/help-mode/);
  const tip = page.locator("#helpTip");
  const row = waveRow(page, "drums");
  for (const [loc, words] of [
    [tab(page, "drums"), "beside this lane's Key controls"],
    [row.locator(".model-select"), "the same list as in Settings"],
    [row.locator(".model-reextract"), "Separates this one stem again"],
    [row.locator(".model-undo"), "Puts back the stem"],
    [row.locator(".model-keep"), "discards the earlier version for good"],
  ]) {
    await loc.hover();
    await expect(tip).toBeVisible();
    await expect(tip).toContainText(words);
  }
});
