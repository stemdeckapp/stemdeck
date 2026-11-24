// Settings > Separation: the compute device, the quality and the idle unload
// (moved from General), and the model for each stem (separationSettings.js).
//
// The suite's backend has whatever device the runner has, so /api/settings is
// answered from the real response with the per-stem model fields set for the
// machine under test, and a model choice is answered here too: no test writes
// a setting to the shared backend. The words and the numbers themselves are
// pinned in tests/js/stem-models-view.test.mjs.
//
// GET /api/my-models answers 404 here, as an older server would: the selects
// then have no "Add a model..." option, so these tests are about the built-in
// models only. tests/e2e/my-models.spec.mjs covers the selects with it.

import { test, expect } from "@playwright/test";
import { seedLibrary } from "./helpers.mjs";

const STEMS5 = ["vocals", "drums", "bass", "guitar", "piano"];
const MODELS = {
  htdemucs_6s: { name: "HTDemucs 6s", license: "MIT", source_url: "https://github.com/facebookresearch/demucs", download_bytes: 54_996_327, cpu_rtf: 0.5, gpu_rtf: 0.068, available: true, downloaded: true, note_key: "settings.stemModels.note.htdemucs_6s" },
  htdemucs_ft: { name: "HTDemucs FT", license: "MIT", source_url: "https://github.com/facebookresearch/demucs", download_bytes: 336_565_084, cpu_rtf: 1.84, gpu_rtf: 0.145, available: true, downloaded: false, note_key: "settings.stemModels.note.htdemucs_ft" },
  kim_mel_roformer: { name: "Kim Mel-RoFormer", license: "MIT", source_url: "https://huggingface.co/KimberleyJSN/melbandroformer", download_bytes: 913_106_900, cpu_rtf: 3.4, gpu_rtf: 0.105, available: true, downloaded: false, note_key: "settings.stemModels.note.kim_mel_roformer" },
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

/** The per-stem fields a server on this machine answers, for the saved choices. */
function stemFields(machine, saved) {
  const models = machine.kim === false
    ? { ...MODELS, kim_mel_roformer: { ...MODELS.kim_mel_roformer, available: false } }
    : MODELS;
  const reason = machine.device === "cuda" ? (machine.kim === false ? "unavailable" : "gpu") : "cpu";
  const recommended = Object.fromEntries(STEMS5.map((s) => [s, "htdemucs_6s"]));
  if (reason === "gpu") recommended.vocals = "kim_mel_roformer";
  const resolved = Object.fromEntries(STEMS5.map((s) => {
    const v = saved[s];
    return [s, v && v !== "auto" && models[v]?.available !== false ? v : recommended[s]];
  }));
  return {
    demucs_device_resolved: machine.device,
    demucs_devices_available: machine.device === "cuda" ? ["cuda", "cpu"] : ["cpu"],
    separation_quality: "standard",
    stem_models: { ...saved },
    stem_models_resolved: resolved,
    stem_models_recommended: recommended,
    stem_models_reason: reason,
    stem_model_catalog: { stems: CATALOG_STEMS, models, min_vram_mb: 4096 },
  };
}

/**
 * Answer /api/settings as a server on `machine` ({ device: "cuda" | "cpu",
 * kim: false when the model is not available }). A model choice or a device
 * change is answered here and recorded in `posts`; `failSaves` refuses them.
 */
async function stubMachine(page, machine, { failSaves = false } = {}) {
  const state = { machine: { ...machine }, saved: Object.fromEntries(STEMS5.map((s) => [s, "auto"])), posts: [], base: null };
  await page.route(/\/api\/my-models(\/.*)?$/, (route) => route.fulfill({ status: 404, json: { detail: "Not Found" } }));
  await page.route("**/api/settings", async (route) => {
    const req = route.request();
    if (req.method() === "GET") {
      const res = await route.fetch();
      state.base = await res.json();
      return route.fulfill({ response: res, json: { ...state.base, ...stemFields(state.machine, state.saved) } });
    }
    const body = JSON.parse(req.postData() || "{}");
    if (!("stem_models" in body) && !("demucs_device" in body)) return route.continue();
    state.posts.push(body);
    if (failSaves) return route.fulfill({ status: 500, json: { detail: "stubbed failure" } });
    if (body.stem_models) Object.assign(state.saved, body.stem_models);
    if (body.demucs_device) state.machine.device = body.demucs_device === "auto" ? machine.device : body.demucs_device;
    const fields = stemFields(state.machine, state.saved);
    return route.fulfill({ json: { ...state.base, ...fields, demucs_device: body.demucs_device ?? state.base?.demucs_device } });
  });
  return state;
}

async function openSeparation(page) {
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  await page.locator('.settings-tab[data-tab="separation"]').click();
  await expect(page.locator('.settings-pane[data-pane="separation"]')).toBeVisible();
}

const pane = (page) => page.locator('.settings-pane[data-pane="separation"]');
const select = (page, stem) => page.locator(`select.set-stem-model[data-stem="${stem}"]`);
const msg = (page) => page.locator(".stem-models-msg");

test("Separation is the tab after General and holds the device, quality and idle rows", async ({ page }) => {
  await stubMachine(page, { device: "cuda" });
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  const tabs = page.locator(".settings-tab");
  await expect(tabs.nth(0)).toHaveAttribute("data-tab", "general");
  await expect(tabs.nth(1)).toHaveAttribute("data-tab", "separation");
  await expect(tabs.nth(1)).toHaveText("Separation");
  // Not on General any more.
  const general = page.locator('.settings-pane[data-pane="general"]');
  await expect(general.locator(".set-demucs-device, .set-separation-quality, .set-idle-unload")).toHaveCount(0);

  await tabs.nth(1).click();
  await expect(page.locator(".settings-main-title")).toHaveText("Separation");
  for (const sel of [".set-demucs-device", ".set-separation-quality", ".set-idle-unload"]) {
    await expect(pane(page).locator(sel)).toBeVisible();
  }
  // The speed test group shows only on a server with its routes.
  await expect(pane(page).locator(".settings-group:not([hidden]) > .settings-group-title").first()).toHaveText("Device and speed");
  await expect(pane(page).locator(".stem-models-group > .settings-group-title")).toHaveText("Model for each stem");
  await expect(pane(page).locator(".speed-test-group > .settings-group-title")).toHaveText("Benchmark");
});

test("on a GPU, every stem has its row and Kim is recommended for vocals", async ({ page }) => {
  await stubMachine(page, { device: "cuda" });
  await openSeparation(page);
  const rows = pane(page).locator(".stem-model-row");
  await expect(rows).toHaveCount(7);
  await expect(rows.locator(".settings-row-title")).toHaveText(["Vocals", "Lead + Backing", "Drums", "Bass", "Guitar", "Piano", "Other"]);
  // Guitar and Piano are selects too (one built-in model each), so a model
  // the user adds has somewhere to show.
  for (const stem of STEMS5) await expect(select(page, stem)).toBeVisible();
  for (const stem of ["lead_backing", "other"]) {
    await expect(page.locator(`.stem-model-fixed[data-stem="${stem}"]`)).toBeVisible();
    await expect(page.locator(`select.set-stem-model[data-stem="${stem}"]`)).toHaveCount(0);
  }
  await expect(select(page, "guitar").locator("option")).toHaveText(["HTDemucs 6s (Recommended)"]);
  await expect(select(page, "guitar")).toHaveValue("htdemucs_6s");
  await expect(page.locator('.set-stem-model-hint[data-stem="guitar"]')).toHaveText("Downloaded. License: MIT.");

  await expect(select(page, "vocals")).toHaveValue("kim_mel_roformer");
  await expect(select(page, "vocals").locator("option")).toHaveText(["HTDemucs 6s", "Kim Mel-RoFormer (Recommended)"]);
  await expect(select(page, "drums").locator('option[value="htdemucs_6s"]')).toHaveText("HTDemucs 6s (Recommended)");
  await expect(page.locator(".set-stem-models-why")).toHaveText(
    "Separation runs on a graphics card, so the cleaner vocal model is recommended for Vocals. Its pass adds about 25 s to a 4-minute song here. The first time, it downloads 913 MB.",
  );
  await expect(page.locator(".set-stem-models-estimate")).toHaveText(
    "With these models, a 4-minute song takes about 0.7 min to separate on this computer.",
  );
  await expect(page.locator('.set-stem-model-hint[data-stem="vocals"]')).toHaveText("Downloads 913 MB the first time it is used. License: MIT.");
  await expect(page.locator('.set-stem-model-warn[data-stem="vocals"]')).toBeHidden();
  // The label is tied to its select, for a screen reader and a click.
  await expect(pane(page).getByLabel("Vocals", { exact: true })).toHaveAttribute("data-stem", "vocals");
  await expect(page.locator(".stem-models-msg")).toHaveAttribute("role", "status");
});

test("on the processor, 6s is recommended, the reason gives Kim's time, and Kim warns it is slow", async ({ page }) => {
  await stubMachine(page, { device: "cpu" });
  await openSeparation(page);
  await expect(select(page, "vocals")).toHaveValue("htdemucs_6s");
  await expect(select(page, "vocals").locator("option")).toHaveText(["HTDemucs 6s (Recommended)", "Kim Mel-RoFormer"]);
  await expect(page.locator(".set-stem-models-why")).toHaveText(
    "Separation runs on the processor, so the fast model is recommended for every stem. The cleaner vocal model would take about 15.6 min for a 4-minute song here.",
  );
  await expect(page.locator(".set-stem-models-estimate")).toContainText("about 2 min");

  await select(page, "vocals").selectOption("kim_mel_roformer");
  const warn = page.locator('.set-stem-model-warn[data-stem="vocals"]');
  await expect(warn).toBeVisible();
  await expect(warn).toHaveText("Slow on this computer: about 13.6 min for a 4-minute song.");
  await expect(page.locator(".set-stem-models-estimate")).toContainText("about 15.6 min");
});

test("a change posts the model, and picking the recommended one posts auto", async ({ page }) => {
  const state = await stubMachine(page, { device: "cuda" });
  await openSeparation(page);
  await select(page, "drums").selectOption("htdemucs_ft");
  await expect.poll(() => state.posts).toContainEqual({ stem_models: { drums: "htdemucs_ft" } });
  await expect(msg(page)).toHaveText("Saved. It applies to the next track.");
  await expect(select(page, "drums")).toHaveValue("htdemucs_ft");

  await select(page, "vocals").selectOption("htdemucs_6s");
  await expect.poll(() => state.posts).toContainEqual({ stem_models: { vocals: "htdemucs_6s" } });
  await select(page, "vocals").selectOption("kim_mel_roformer");
  // The recommended model is saved as "auto", so a later machine keeps following it.
  await expect.poll(() => state.posts.at(-1)).toEqual({ stem_models: { vocals: "auto" } });
});

test("Use recommended sets all five back to auto", async ({ page }) => {
  const state = await stubMachine(page, { device: "cuda" });
  await openSeparation(page);
  await select(page, "bass").selectOption("htdemucs_ft");
  await expect(select(page, "bass")).toHaveValue("htdemucs_ft");
  await page.locator("button.set-stem-models-reset").click();
  await expect.poll(() => state.posts.at(-1)).toEqual({
    stem_models: { vocals: "auto", drums: "auto", bass: "auto", guitar: "auto", piano: "auto" },
  });
  await expect(select(page, "bass")).toHaveValue("htdemucs_6s");
  await expect(page.locator("button.set-stem-models-reset")).toBeFocused();
});

test("Kim is disabled and says why when it is not available on this install", async ({ page }) => {
  await stubMachine(page, { device: "cuda", kim: false });
  await openSeparation(page);
  const kim = select(page, "vocals").locator('option[value="kim_mel_roformer"]');
  await expect(kim).toBeDisabled();
  await expect(kim).toHaveText("Kim Mel-RoFormer (Not available on this install)");
  await expect(select(page, "vocals")).toHaveValue("htdemucs_6s");
  await expect(page.locator(".set-stem-models-why")).toHaveText(
    "The cleaner vocal model is not available on this install, so the fast model is recommended for every stem.",
  );
});

test("a failed save puts back the model in use and says so", async ({ page }) => {
  await stubMachine(page, { device: "cuda" }, { failSaves: true });
  await openSeparation(page);
  await select(page, "drums").selectOption("htdemucs_ft");
  await expect(msg(page)).toHaveText("Could not save the model. The list shows the model still in use.");
  await expect(msg(page)).toHaveClass(/error/);
  await expect(select(page, "drums")).toHaveValue("htdemucs_6s");
});

test("changing the compute device repaints the recommendation", async ({ page }) => {
  await stubMachine(page, { device: "cuda" });
  await openSeparation(page);
  await expect(select(page, "vocals")).toHaveValue("kim_mel_roformer");
  await page.locator(".set-demucs-device").selectOption("cpu");
  await expect(select(page, "vocals")).toHaveValue("htdemucs_6s");
  await expect(page.locator(".set-stem-models-why")).toContainText("runs on the processor");
});

test("an older server without model fields shows the tab without the model group", async ({ page }) => {
  const errors = [];
  page.on("pageerror", (err) => errors.push(String(err)));
  // The real backend sends the fields, so strip them.
  await page.route("**/api/settings", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const res = await route.fetch();
    const body = await res.json();
    for (const k of ["stem_models", "stem_models_resolved", "stem_models_recommended", "stem_models_reason", "stem_model_catalog"]) delete body[k];
    return route.fulfill({ response: res, json: body });
  });
  await openSeparation(page);
  await expect(pane(page).locator(".set-demucs-device")).toBeVisible();
  // The group is in the page, and hidden, so this cannot pass on a page without it.
  await expect(page.locator(".stem-models-group")).toHaveCount(1);
  await expect(page.locator(".stem-models-group")).toBeHidden();
  expect(errors).toEqual([]);
});

test("search finds a stem's row and opens the Separation tab on it", async ({ page }) => {
  await stubMachine(page, { device: "cuda" });
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  // The rows are drawn from the settings answer: wait for them before searching.
  await expect(page.locator('.stem-model-row[data-stem="piano"]')).toHaveCount(1);
  await page.locator(".settings-search-input").fill("Vocals");
  const result = page.locator(".settings-result").filter({ has: page.locator(".settings-result-title", { hasText: /^Vocals$/ }) });
  await expect(result).toHaveCount(1);
  await expect(result.locator(".settings-result-path")).toHaveText("Separation / Model for each stem");
  await result.click();
  await expect(page.locator(".settings-tab.active")).toHaveAttribute("data-tab", "separation");
  await expect(select(page, "vocals")).toBeFocused();
});

test("at phone width the rows stack and nothing runs off the side", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await stubMachine(page, { device: "cpu" });
  await openSeparation(page);
  const row = page.locator('.stem-model-row[data-stem="vocals"]');
  await expect(select(page, "vocals")).toBeVisible();
  const [rowBox, selBox, textBox] = await Promise.all([
    row.boundingBox(),
    select(page, "vocals").boundingBox(),
    row.locator(".settings-row-text").boundingBox(),
  ]);
  // Stacked: the select sits under the text, full width of the row.
  expect(selBox.y).toBeGreaterThanOrEqual(textBox.y + textBox.height - 1);
  expect(Math.abs(selBox.width - rowBox.width)).toBeLessThanOrEqual(2);
  const overflow = await pane(page).evaluate((el) => el.scrollWidth - el.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  const reset = await page.locator("button.set-stem-models-reset").boundingBox();
  expect(reset.x).toBeGreaterThanOrEqual(0);
  expect(reset.x + reset.width).toBeLessThanOrEqual(375);
});
