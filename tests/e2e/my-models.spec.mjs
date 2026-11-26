// Settings > Models > My models (static/js/myModels.js, myModelsView.js). The
// section is in the Models tab; the stem selects that offer a model of yours
// are in the Separation tab, so a test that picks one goes there first.
//
// No test adds a real model: that needs a model file of hundreds of MB, a
// network for the link route and torch for the check. /api/my-models is
// answered here from a small in-page state, and /api/settings is the real
// answer with the per-stem model fields and online sources set by the test,
// so nothing is written to the shared backend. The rules themselves (the link
// rule, the missing line, the reason sentences) are pinned in
// tests/js/my-models-view.test.mjs; the routes in tests/test_my_models_api.py.

import { test, expect } from "@playwright/test";
import { seedLibrary } from "./helpers.mjs";

const STEMS5 = ["vocals", "drums", "bass", "guitar", "piano"];
const BUILT_IN = {
  htdemucs_6s: { name: "HTDemucs 6s", user: false, engine: "demucs", license: "MIT", source_url: "https://github.com/facebookresearch/demucs", download_bytes: 54_996_327, cpu_rtf: 0.5, gpu_rtf: 0.068, available: true, downloaded: true, note_key: "settings.stemModels.note.htdemucs_6s" },
  htdemucs_ft: { name: "HTDemucs FT", user: false, engine: "demucs", license: "MIT", source_url: "https://github.com/facebookresearch/demucs", download_bytes: 336_565_084, cpu_rtf: 1.84, gpu_rtf: 0.145, available: true, downloaded: false, note_key: "settings.stemModels.note.htdemucs_ft" },
  kim_mel_roformer: { name: "Kim Mel-RoFormer", user: false, engine: "mel_roformer", license: "MIT", source_url: "https://huggingface.co/KimberleyJSN/melbandroformer", download_bytes: 913_106_900, cpu_rtf: 3.4, gpu_rtf: 0.105, available: true, downloaded: false, note_key: "settings.stemModels.note.kim_mel_roformer" },
};
const CHOICES = { vocals: ["htdemucs_6s", "kim_mel_roformer"], drums: ["htdemucs_6s", "htdemucs_ft"], bass: ["htdemucs_6s", "htdemucs_ft"], guitar: ["htdemucs_6s"], piano: ["htdemucs_6s"] };

const GUITAR_ID = "user_0123456789ab";
const VOCALS_ID = "user_aaaaaaaaaaaa";
const SHA = "3f9a12" + "0".repeat(54) + "c21e";

/** A ready entry as GET /api/my-models serves it. */
function readyEntry(id, name, stems, extra = {}) {
  return {
    id, name, status: "ready", pct: 100, step: null, code: null, size: 912_000_000, sha256: SHA, stems,
    arch: "bs_roformer", params: 228_000_000,
    source: { kind: "hf", label: `owner/${name}@0123456789ab`, license_declared: "MIT" },
    added_at: 1_790_000_000, checked_at: 1_790_000_100, cpu_rtf: 2.5, usable: true, missing: false, ...extra,
  };
}

/**
 * Answer /api/my-models and /api/settings. `state.models` is what GET lists;
 * a test changes it to move an entry along. Every request is logged.
 */
async function stubMyModels(page, { models = [], online = true, available = true, routes = true } = {}) {
  const state = { models: [...models], online, available, saved: {}, base: {}, log: { link: [], upload: [], check: [], cancel: [], remove: [], settingsPosts: [] }, nextLink: null };
  await page.route(/\/api\/my-models(\/.*)?$/, async (route) => {
    if (!routes) return route.fulfill({ status: 404, json: { detail: "Not Found" } });
    const req = route.request();
    const path = new URL(req.url()).pathname.replace(/^\/api\/my-models/, "");
    const method = req.method();
    if (method === "GET" && path === "") {
      return route.fulfill({ json: { models: state.models, limits: { max_bytes: 2_147_483_648, max_config_bytes: 262_144, max_models: 12, max_params: 500_000_000 }, registry_reset: false, available: state.available } });
    }
    if (method === "POST" && path === "/link") {
      const body = JSON.parse(req.postData() || "{}");
      state.log.link.push(body);
      if (!state.online) return route.fulfill({ status: 403, json: { detail: "off", code: "online_sources_off" } });
      if (state.nextLink) {
        const refusal = state.nextLink;
        state.nextLink = null;
        return route.fulfill(refusal);
      }
      const entry = { ...readyEntry("user_bbbbbbbbbbbb", body.name || "repo", body.stems), status: "downloading", pct: 0, usable: false, size: null, sha256: null };
      state.models.push(entry);
      return route.fulfill({ status: 202, json: entry });
    }
    if (method === "POST" && path === "/upload") {
      state.log.upload.push({ type: req.headers()["content-type"] || "", body: req.postData() || "" });
      const entry = { ...readyEntry("user_cccccccccccc", "drums", ["drums"]), status: "checking", pct: 10, usable: false, source: { kind: "upload", label: "drums.safetensors", license_declared: null } };
      state.models.push(entry);
      return route.fulfill({ status: 202, json: entry });
    }
    const m = /^\/(user_[a-f0-9]{12})(?:\/(check|cancel))?$/.exec(path);
    if (m) {
      const [, id, action] = m;
      if (method === "DELETE") {
        state.log.remove.push(id);
        state.models = state.models.filter((e) => e.id !== id);
        return route.fulfill({ json: { removed: id } });
      }
      if (action === "check") {
        state.log.check.push(id);
        state.models = state.models.map((e) => (e.id === id ? { ...e, status: "checking", pct: 0, usable: false } : e));
        return route.fulfill({ status: 202, json: state.models.find((e) => e.id === id) });
      }
      if (action === "cancel") {
        state.log.cancel.push(id);
        state.models = state.models.filter((e) => e.id !== id);
        return route.fulfill({ json: { id, model: null } });
      }
    }
    return route.fulfill({ status: 404, json: { detail: "Not Found" } });
  });
  await page.route("**/api/settings", async (route) => {
    const req = route.request();
    if (req.method() === "GET") {
      const res = await route.fetch();
      state.base = await res.json();
      return route.fulfill({ response: res, json: { ...state.base, ...stemFields(state, state.saved) } });
    }
    const body = JSON.parse(req.postData() || "{}");
    if (!("stem_models" in body)) return route.continue();
    state.log.settingsPosts.push(body);
    Object.assign(state.saved, body.stem_models);
    return route.fulfill({ json: { ...state.base, ...stemFields(state, state.saved) } });
  });
  return state;
}

/** The per-stem fields a CPU server answers, with the usable user models in the choices. */
function stemFields(state, saved = {}) {
  const usable = state.models.filter((e) => e.status === "ready" && e.usable);
  const models = { ...BUILT_IN };
  for (const e of usable) {
    models[e.id] = { name: e.name, user: true, engine: "user_roformer", stems: e.stems, license: e.source?.license_declared ?? null, source_url: null, download_bytes: 0, size: e.size, cpu_rtf: e.cpu_rtf, gpu_rtf: 0.1, estimated: true, available: true, downloaded: true, note_key: null };
  }
  const recommended = Object.fromEntries(STEMS5.map((s) => [s, "htdemucs_6s"]));
  const stemModels = { ...Object.fromEntries(STEMS5.map((s) => [s, "auto"])), ...saved };
  return {
    online_sources: state.online,
    demucs_device_resolved: "cpu",
    demucs_devices_available: ["cpu"],
    separation_quality: "standard",
    stem_models: stemModels,
    stem_models_resolved: Object.fromEntries(STEMS5.map((s) => [s, stemModels[s] === "auto" ? recommended[s] : stemModels[s]])),
    stem_models_recommended: recommended,
    stem_models_reason: "cpu",
    stem_model_catalog: {
      stems: [
        ...STEMS5.map((s) => ({ stem: s, choices: [...CHOICES[s], ...usable.filter((e) => e.stems.includes(s)).map((e) => e.id)] })),
        { stem: "lead_backing", fixed: "uvr_karaoke_2", available: true },
        { stem: "other", fixed: "residual" },
      ],
      models,
      min_vram_mb: 4096,
      deployment: null,
    },
  };
}

async function openMyModels(page, { language = null, expand = true } = {}) {
  if (language) {
    await page.addInitScript((code) => localStorage.setItem("stemdeck.language", JSON.stringify(code)), language);
  }
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  await page.locator('.settings-tab[data-tab="models"]').click();
  await expect(group(page)).toBeVisible();
  if (expand) await page.locator(".my-models-toggle").click();
}

/** The Separation tab, where the stem selects are. */
async function toSeparation(page) {
  await page.locator('.settings-tab[data-tab="separation"]').click();
  await expect(page.locator('.settings-pane[data-pane="separation"]')).toBeVisible();
}

const group = (page) => page.locator(".my-models-group");
const select = (page, stem) => page.locator(`select.set-stem-model[data-stem="${stem}"]`);
const add = (page) => page.locator(".my-models-add");
const row = (page, id) => page.locator(`.my-model-row[data-id="${id}"]`);

test("the section is closed at first, and opens on the notice and the form", async ({ page }) => {
  await stubMyModels(page);
  await openMyModels(page, { expand: false });
  const toggle = page.locator(".my-models-toggle");
  await expect(toggle).toHaveText("Advanced: My models");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(page.locator(".my-models-body")).toBeHidden();
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator(".my-models-notice")).toHaveText(
    "These models come from third parties. Models you load are your own responsibility, including their licence and your right to use them.",
  );
  await expect(page.locator(".my-models-licence-input")).toHaveAttribute("aria-describedby", "myModelsNotice");
  await expect(page.locator(".my-models-empty")).toHaveText("No models yet. Add one above.");
  await expect(page.locator(".my-models-file-input")).toHaveAttribute("accept", ".safetensors,.ckpt,.pth,.pt");
  await expect(page.locator(".my-models-config-input")).toHaveAttribute("accept", ".yaml,.yml");
});

test("Add model stays off until a link, a stem and the licence box, and says what is missing", async ({ page }) => {
  const state = await stubMyModels(page);
  await openMyModels(page);
  const missing = page.locator(".my-models-missing");
  await expect(add(page)).toBeDisabled();
  await expect(missing).toHaveText("Still needed: a link or a file, at least one stem, and the licence box.");
  await page.locator(".my-models-link").fill("huggingface.co/owner/repo");
  await expect(missing).toHaveText("Still needed: a Hugging Face link like https://huggingface.co/owner/repo, at least one stem, and the licence box.");
  await page.locator(".my-models-link").fill("https://huggingface.co/owner/guitar-roformer");
  await page.locator('.my-models-stem[value="guitar"]').check();
  await expect(add(page)).toBeDisabled();
  await page.locator(".my-models-licence-input").check();
  await expect(add(page)).toBeEnabled();
  await expect(missing).toHaveText("");

  await add(page).click();
  await expect.poll(() => state.log.link).toEqual([{ link: "https://huggingface.co/owner/guitar-roformer", stems: ["guitar"], licence_confirmed: true, name: "guitar-roformer" }]);
  const newRow = row(page, "user_bbbbbbbbbbbb");
  await expect(newRow.locator(".my-model-status")).toHaveText("Downloading 0%");
  await expect(newRow.locator(".my-model-cancel")).toBeVisible();
  await expect(newRow.locator(".my-model-check")).toBeDisabled();
  // The form empties, and focus goes to the list.
  await expect(page.locator(".my-models-link")).toHaveValue("");
  await expect(page.locator(".my-models-list-title")).toBeFocused();

  // The server moves it along; the page follows by polling.
  state.models = state.models.map((e) => ({ ...e, pct: 40 }));
  await expect(newRow.locator(".my-model-status")).toHaveText("Downloading 50%");
  state.models = state.models.map((e) => ({ ...e, status: "checking", pct: 90 }));
  await expect(newRow.locator(".my-model-status")).toHaveText("Checking the model...");
  state.models = state.models.map((e) => ({ ...e, status: "ready", pct: 100, usable: true, size: 912_000_000, sha256: SHA }));
  await expect(newRow.locator(".my-model-status")).toHaveText("Ready");
  await expect(page.locator(".my-models-live")).toHaveText("guitar-roformer is ready.");
  // Ready: the settings are read again, and Guitar lists it as yours.
  await expect(select(page, "guitar").locator('option[value="user_bbbbbbbbbbbb"]')).toHaveText("guitar-roformer (yours)");
  await expect(select(page, "vocals").locator('option[value="user_bbbbbbbbbbbb"]')).toHaveCount(0);
});

test("files from the computer need the config file too, and go up as multipart", async ({ page }) => {
  const state = await stubMyModels(page);
  await openMyModels(page);
  await page.locator('.my-models-stem[value="drums"]').check();
  await page.locator(".my-models-licence-input").check();
  await page.locator(".my-models-file-input").setInputFiles({ name: "drums.safetensors", mimeType: "application/octet-stream", buffer: Buffer.from("not a real model") });
  await expect(page.locator(".my-models-file-name").first()).toHaveText("drums.safetensors");
  await expect(add(page)).toBeDisabled();
  await expect(page.locator(".my-models-missing")).toHaveText("Still needed: the config file.");
  await page.locator(".my-models-config-input").setInputFiles({ name: "config.yaml", mimeType: "text/yaml", buffer: Buffer.from("model: {}\n") });
  await expect(page.locator(".my-models-config-name")).toHaveText("config.yaml");
  await expect(add(page)).toBeEnabled();
  await add(page).click();
  await expect.poll(() => state.log.upload.length).toBe(1);
  const sent = state.log.upload[0];
  expect(sent.type).toMatch(/^multipart\/form-data/);
  for (const part of ['name="model"; filename="drums.safetensors"', 'name="config"; filename="config.yaml"', 'name="stems"', '["drums"]', 'name="licence"', 'name="name"']) {
    expect(sent.body).toContain(part);
  }
  await expect(row(page, "user_cccccccccccc").locator(".my-model-status")).toHaveText("Checking the model...");
  await expect(row(page, "user_cccccccccccc").locator(".my-model-source")).toHaveText("From your computer: drums.safetensors.");
});

test("a refused add says the server's reason in the page's own words", async ({ page }) => {
  const state = await stubMyModels(page);
  state.nextLink = { status: 409, json: { detail: "This model file is already in the list.", code: "duplicate" } };
  await openMyModels(page);
  await page.locator(".my-models-link").fill("https://huggingface.co/owner/repo");
  await page.locator('.my-models-stem[value="vocals"]').check();
  await page.locator(".my-models-licence-input").check();
  await add(page).click();
  const msg = page.locator(".my-models-msg");
  await expect(msg).toHaveText("Not added. You already added this model.");
  await expect(msg).toHaveClass(/error/);
});

test("a refused model shows why; Check again and Remove act on it", async ({ page }) => {
  const state = await stubMyModels(page, {
    models: [
      readyEntry(GUITAR_ID, "Guitar RoFormer", ["guitar"]),
      { ...readyEntry(VOCALS_ID, "Vocal model", ["vocals"]), status: "refused", code: "key_mismatch", usable: false },
    ],
  });
  await openMyModels(page);
  const refused = row(page, VOCALS_ID);
  await expect(refused.locator(".my-model-status")).toHaveText("Refused");
  await expect(refused.locator(".my-model-reason")).toHaveText("The model file does not match its config file.");
  await expect(row(page, GUITAR_ID).locator(".my-model-meta")).toHaveText("912 MB · separates Guitar · SHA-256 3f9a12...c21e");
  await expect(row(page, GUITAR_ID).locator(".my-model-source")).toHaveText("From Hugging Face: owner/Guitar RoFormer@0123456789ab. Licence declared by its source: MIT. StemDeck does not check it.");

  await refused.locator(".my-model-check").click();
  await expect.poll(() => state.log.check).toEqual([VOCALS_ID]);
  await expect(refused.locator(".my-model-status")).toHaveText("Checking the model...");

  // Remove the first row: focus moves to the Remove that takes its place.
  await row(page, GUITAR_ID).locator(".my-model-remove").click();
  await expect.poll(() => state.log.remove).toEqual([GUITAR_ID]);
  await expect(row(page, GUITAR_ID)).toHaveCount(0);
  await expect(row(page, VOCALS_ID).locator(".my-model-remove")).toBeFocused();
  await expect(page.locator(".my-models-live")).toHaveText("Guitar RoFormer removed.");
  // The last one: focus goes to the list title.
  await row(page, VOCALS_ID).locator(".my-model-remove").click();
  await expect(page.locator(".my-models-list-title")).toBeFocused();
  await expect(page.locator(".my-models-empty")).toBeVisible();
});

test("a model of yours is offered for its stems only, never recommended, and posts its id", async ({ page }) => {
  const state = await stubMyModels(page, { models: [readyEntry(GUITAR_ID, "Guitar RoFormer", ["guitar"])] });
  await openMyModels(page, { expand: false });
  await toSeparation(page);
  // Guitar is a select now, with its built-in model, the user's, then Add.
  await expect(select(page, "guitar").locator("option")).toHaveText(["HTDemucs 6s (Recommended)", "Guitar RoFormer (yours)", "Add a model..."]);
  await expect(select(page, "piano").locator("option")).toHaveText(["HTDemucs 6s (Recommended)", "Add a model..."]);
  await expect(select(page, "vocals").locator(`option[value="${GUITAR_ID}"]`)).toHaveCount(0);
  await expect(select(page, "guitar").locator("option[data-recommended]")).toHaveCount(1);
  await select(page, "guitar").selectOption(GUITAR_ID);
  await expect.poll(() => state.log.settingsPosts).toContainEqual({ stem_models: { guitar: GUITAR_ID } });
  await expect(page.locator('.set-stem-model-hint[data-stem="guitar"]')).toHaveText("Added by you. Licence declared by its source: MIT.");
});

test("a link entry with no files says Check again downloads, and needs online sources", async ({ page }) => {
  const id = "user_dddddddddddd";
  const entry = { ...readyEntry(id, "Lost download", ["vocals"]), status: "refused", code: "network", usable: false, size: null, sha256: null };
  await stubMyModels(page, { models: [entry], online: false });
  await openMyModels(page);
  await expect(row(page, id).locator(".my-model-reason")).toHaveText("Hugging Face could not be reached. Check the connection and try again.");
  await expect(row(page, id).locator(".my-model-redownload")).toHaveText(
    "Check again downloads it again from Hugging Face, which needs Online search and link import turned on.",
  );
  await expect(row(page, id).locator(".my-model-check")).toBeDisabled();
});

test("a ready model whose file changed, and one whose file went, both say so", async ({ page }) => {
  await stubMyModels(page, {
    models: [
      readyEntry(GUITAR_ID, "Changed", ["guitar"], { usable: false, code: "changed" }),
      readyEntry(VOCALS_ID, "Gone", ["vocals"], { status: "missing", usable: false, missing: true }),
    ],
  });
  await openMyModels(page);
  await expect(row(page, GUITAR_ID).locator(".my-model-status")).toHaveText("Not available");
  await expect(row(page, GUITAR_ID).locator(".my-model-reason")).toHaveText("The model file changed or is missing since it was checked.");
  await expect(row(page, VOCALS_ID).locator(".my-model-status")).toHaveText("File missing");
  // Neither is offered in the stem lists.
  await expect(select(page, "guitar").locator(`option[value="${GUITAR_ID}"]`)).toHaveCount(0);
  await expect(select(page, "vocals").locator(`option[value="${VOCALS_ID}"]`)).toHaveCount(0);
});

test("Add a model... opens the Models tab at the link field and is never posted", async ({ page }) => {
  const state = await stubMyModels(page);
  await openMyModels(page, { expand: false });
  await expect(page.locator(".my-models-body")).toBeHidden();
  await toSeparation(page);
  await select(page, "piano").selectOption({ label: "Add a model..." });
  await expect(page.locator(".settings-tab.active")).toHaveAttribute("data-tab", "models");
  await expect(page.locator('.settings-pane[data-pane="models"]')).toBeVisible();
  await expect(page.locator(".my-models-body")).toBeVisible();
  await expect(page.locator(".my-models-link")).toBeFocused();
  await expect(select(page, "piano")).toHaveValue("htdemucs_6s");
  expect(state.log.settingsPosts).toEqual([]);
});

test("with online sources off, the link field is off and says why; files still work", async ({ page }) => {
  const state = await stubMyModels(page, { online: false });
  await openMyModels(page);
  await expect(page.locator(".my-models-link")).toBeDisabled();
  await expect(page.locator(".my-models-link-off")).toHaveText(
    "Adding by link needs Online search and link import, which is off. Turn it on in Settings, Experimental. Files from your computer work either way.",
  );
  await page.locator('.my-models-stem[value="bass"]').check();
  await page.locator(".my-models-licence-input").check();
  await expect(page.locator(".my-models-missing")).toHaveText("Still needed: the model file and its config file.");
  await page.locator(".my-models-file-input").setInputFiles({ name: "bass.ckpt", mimeType: "application/octet-stream", buffer: Buffer.from("PK") });
  await page.locator(".my-models-config-input").setInputFiles({ name: "config.yml", mimeType: "text/yaml", buffer: Buffer.from("a: 1\n") });
  await expect(add(page)).toBeEnabled();
  // "Add a model..." lands on the file button, the first control that works.
  await page.locator(".my-models-toggle").click();
  await toSeparation(page);
  await select(page, "bass").selectOption({ label: "Add a model..." });
  await expect(page.locator(".settings-tab.active")).toHaveAttribute("data-tab", "models");
  await expect(page.locator(".my-models-file-btn")).toBeFocused();
  expect(state.log.link).toEqual([]);
});

test("an install that cannot run models says so and keeps Add off", async ({ page }) => {
  await stubMyModels(page, { available: false });
  await openMyModels(page);
  await expect(page.locator(".my-models-unavailable")).toHaveText("Models you add cannot run on this install.");
  await page.locator(".my-models-link").fill("https://huggingface.co/owner/repo");
  await page.locator('.my-models-stem[value="vocals"]').check();
  await page.locator(".my-models-licence-input").check();
  await expect(add(page)).toBeDisabled();
});

test("a server without the routes shows no section and no Add a model...", async ({ page }) => {
  const errors = [];
  page.on("pageerror", (err) => errors.push(String(err)));
  await stubMyModels(page, { routes: false });
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  await page.locator('.settings-tab[data-tab="separation"]').click();
  await expect(select(page, "guitar")).toBeVisible();
  // The section is in the page, and hidden, so this cannot pass on a page without it.
  await expect(group(page)).toHaveCount(1);
  await expect(group(page)).toBeHidden();
  await expect(select(page, "guitar").locator("option")).toHaveText(["HTDemucs 6s (Recommended)"]);
  expect(errors).toEqual([]);
});

test("in German and in European Portuguese, a row reads in that language", async ({ page }) => {
  await stubMyModels(page, { models: [{ ...readyEntry(VOCALS_ID, "Vocal model", ["vocals"]), status: "refused", code: "too_big_model", usable: false }] });
  await openMyModels(page, { language: "de" });
  await expect(page.locator(".my-models-toggle")).toHaveText("Erweitert: Meine Modelle");
  await expect(row(page, VOCALS_ID).locator(".my-model-status")).toHaveText("Abgelehnt");
  await expect(row(page, VOCALS_ID).locator(".my-model-reason")).toHaveText("Das Modell ist zu groß: Es bräuchte mehr Speicher als erlaubt.");
  await expect(page.locator(".my-models-file-btn")).toHaveText("Modelldatei wählen...");
});

test("European Portuguese uses its own words where they differ", async ({ page }) => {
  await stubMyModels(page);
  await openMyModels(page, { language: "pt-PT" });
  await expect(page.locator(".my-models-file-btn")).toHaveText("Escolher ficheiro do modelo...");
  // A word that is the same in both comes from pt.
  await expect(page.locator(".my-models-add")).toHaveText("Adicionar modelo");
});

test("at phone width the form and the rows stack and nothing runs off the side", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await stubMyModels(page, { models: [readyEntry(GUITAR_ID, "A guitar model with a long name that has to wrap somewhere", ["guitar"])] });
  await openMyModels(page);
  const pane = page.locator('.settings-pane[data-pane="models"]');
  const overflow = await pane.evaluate((el) => el.scrollWidth - el.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  for (const sel of [".my-models-link", ".my-models-add", `.my-model-row[data-id="${GUITAR_ID}"] .my-model-remove`]) {
    const box = await page.locator(sel).boundingBox();
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(375);
  }
});

test("the section is in the Models tab, and no longer in Separation", async ({ page }) => {
  await stubMyModels(page);
  await openMyModels(page, { expand: false });
  await expect(page.locator('.settings-pane[data-pane="models"] .my-models-group')).toHaveCount(1);
  await expect(page.locator('.settings-pane[data-pane="separation"] .my-models-group')).toHaveCount(0);
  // Every element id the section had is still there, once.
  for (const id of ["myModelsBody", "myModelsNotice", "myModelsLink", "myModelsFile", "myModelsConfig", "myModelsListTitle", "myModelsMissing"]) {
    await expect(page.locator(`#${id}`)).toHaveCount(1);
  }
});

test("Add or remove models in Models, in the Separation tab, opens the Models tab", async ({ page }) => {
  await stubMyModels(page);
  await openMyModels(page, { expand: false });
  await toSeparation(page);
  const link = page.locator('.settings-pane[data-pane="separation"] .models-open');
  await expect(link).toHaveText("Add or remove models in Models");
  await link.click();
  await expect(page.locator(".settings-tab.active")).toHaveAttribute("data-tab", "models");
  await expect(page.locator('.settings-tab[data-tab="models"]')).toBeFocused();
  await expect(group(page)).toBeVisible();
});

test("each model of yours says what it is used for, and follows a new choice", async ({ page }) => {
  const state = await stubMyModels(page, { models: [readyEntry(GUITAR_ID, "Guitar RoFormer", ["guitar"])] });
  await openMyModels(page);
  const used = row(page, GUITAR_ID).locator(".my-model-used");
  await expect(used).toHaveText("Not used for any stem now.");
  await toSeparation(page);
  await select(page, "guitar").selectOption(GUITAR_ID);
  await expect.poll(() => state.log.settingsPosts).toContainEqual({ stem_models: { guitar: GUITAR_ID } });
  await page.locator('.settings-tab[data-tab="models"]').click();
  await expect(used).toHaveText("Used for: Guitar.");
});
