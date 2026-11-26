// Settings > Transcription (static/js/transcriptionSettings.js): MIDI from the
// Piano and Bass lanes.
//
// Driven in a real browser because the controls are the risk: each select has
// to show what the server holds (Auto, Auto, Balanced by default), a change
// has to reach the server, a refused save has to show the setting still in
// force, the reason the engine cannot run here has to be in words with the
// selects off, and the Keys engine has to say where its model is. GET /api/transcribe/status and the Transcription fields of
// /api/settings are answered here, so nothing writes to the shared backend.

import { test, expect } from "@playwright/test";
import { seedLibrary } from "./helpers.mjs";

const STATUS = {
  available: true, reason: null, stems: ["piano", "bass"], max_seconds: 1200, details: ["balanced", "detailed", "clean"],
  engines: [
    { id: "basic_pitch", label: "Basic Pitch", version: "0.4.0", licence: "Apache-2.0", model_licence: "Apache-2.0",
      stems: ["piano", "bass"], available: true, reason: null, model: "built_in", download_bytes: null },
    { id: "piano_hr", label: "ByteDance Piano Transcription", version: "0.0.6", licence: "MIT", model_licence: "CC BY 4.0",
      stems: ["piano"], available: true, reason: null, model: "not_downloaded", download_bytes: 171_966_578 },
  ],
};
const withHr = (over) => ({ ...STATUS, engines: [STATUS.engines[0], { ...STATUS.engines[1], ...over }] });

/** The fake: `fields` are the Transcription settings, `posts` every save, `failSave` refuses them. */
async function stubServer(page, { status = STATUS, fields = {}, failSave = false } = {}) {
  const state = {
    fields: {
      transcribe_piano: "auto", transcribe_bass: "auto", transcribe_detail: "balanced",
      transcribe_piano_engine: "basic_pitch", ...fields,
    },
    posts: [],
  };
  await page.route("**/api/transcribe/status", (route) =>
    status ? route.fulfill({ json: status }) : route.fulfill({ status: 404, json: { detail: "Not Found" } }));
  await page.route("**/api/settings", async (route) => {
    const req = route.request();
    if (req.method() === "POST") {
      const body = JSON.parse(req.postData() || "{}");
      if (!Object.keys(body).some((k) => /^transcribe_(piano|bass|detail|piano_engine)$/.test(k))) return route.continue();
      state.posts.push(body);
      if (failSave) return route.fulfill({ status: 500, json: { detail: "no" } });
      Object.assign(state.fields, body);
      const res = await page.request.get("/api/settings");
      return route.fulfill({ json: { ...(await res.json()), ...state.fields } });
    }
    const res = await route.fetch();
    return route.fulfill({ response: res, json: { ...(await res.json()), ...state.fields } });
  });
  return state;
}

async function openTab(page) {
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  await page.locator('.settings-tab[data-tab="transcription"]').click();
  const pane = page.locator('.settings-pane[data-pane="transcription"]');
  await expect(pane).toBeVisible();
  return pane;
}

test("the tab sits after Separation, with its notice, its engine and its three choices", async ({ page }) => {
  await stubServer(page);
  const pane = await openTab(page);
  const tabs = await page.locator(".settings-tab").evaluateAll((els) => els.map((e) => e.dataset.tab));
  expect(tabs.indexOf("transcription")).toBe(tabs.indexOf("separation") + 1);
  await expect(page.locator('.settings-tab[data-tab="transcription"]')).toHaveText("Transcription");
  await expect(page.locator('.settings-tab[data-tab="transcription"] svg.settings-tab-icon')).toHaveCount(1);
  await expect(page.locator(".settings-main-title")).toHaveText("Transcription");
  await expect(pane).toContainText("Transcription runs on this computer and sends nothing anywhere.");
  await expect(pane).toContainText("You alone are responsible for having the rights to what you transcribe.");
  await expect(pane).not.toContainText(/\bwe\b/i);
  await expect(pane.locator(".transcription-engine")).toHaveText(
    "Engine: Basic Pitch 0.4.0, licence Apache-2.0. Engine: ByteDance Piano Transcription 0.0.6, licence MIT, model licence CC BY 4.0.",
  );
  await expect(pane.locator(".transcription-unavailable")).toBeHidden();
  await expect(pane.locator(".set-transcribe-piano")).toHaveValue("auto");
  await expect(pane.locator(".set-transcribe-bass")).toHaveValue("auto");
  await expect(pane.locator(".set-transcribe-detail")).toHaveValue("balanced");
  await expect(pane.locator(".set-transcribe-piano option")).toHaveText(["Auto", "Off"]);
  await expect(pane.locator(".set-transcribe-bass option")).toHaveText(["Auto (Basic Pitch)", "Off"]);
  await expect(pane.locator(".set-transcribe-piano-engine")).toHaveValue("basic_pitch");
  await expect(pane.locator(".set-transcribe-piano-engine option")).toHaveText(["Basic Pitch", "ByteDance Piano Transcription"]);
  await expect(pane).toContainText("ByteDance Piano Transcription is best for solo or clearly audible piano.");
  // Basic Pitch is built in: nothing to say about a model.
  await expect(pane.locator(".transcription-piano-engine-note")).toBeHidden();
  await expect(pane.locator(".set-transcribe-detail option")).toHaveText(["Balanced", "Detailed", "Clean"]);
  await expect(pane.locator(".transcription-detail-desc")).toHaveText([
    "Balanced: the notes most songs need, with few stray ones.",
    "Detailed: quiet and short notes too, with more stray ones to delete.",
    "Clean: only clear notes, so some quiet ones are left out.",
  ]);
  await expect(pane.locator('.transcription-detail-desc[data-detail="balanced"]')).toHaveClass(/is-current/);
  for (const sel of [".set-transcribe-piano", ".set-transcribe-piano-engine", ".set-transcribe-bass", ".set-transcribe-detail"]) {
    await expect(pane.locator(sel)).toBeEnabled();
  }
});

test("the Keys engine is saved, and says where its model is", async ({ page }) => {
  const server = await stubServer(page);
  const pane = await openTab(page);
  const note = pane.locator(".transcription-piano-engine-note");
  await pane.locator(".set-transcribe-piano-engine").selectOption("piano_hr");
  await expect.poll(() => server.posts.at(-1)).toEqual({ transcribe_piano_engine: "piano_hr" });
  await expect(note).toBeVisible();
  await expect(note).toHaveText("Its model (172 MB) downloads the first time you transcribe the Piano lane, then stays on this computer.");
  await pane.locator(".set-transcribe-piano-engine").selectOption("basic_pitch");
  await expect.poll(() => server.posts.at(-1)).toEqual({ transcribe_piano_engine: "basic_pitch" });
  await expect(note).toBeHidden();
});

test("a downloaded model shows its size", async ({ page }) => {
  await stubServer(page, { status: withHr({ model: "downloaded" }), fields: { transcribe_piano_engine: "piano_hr" } });
  const pane = await openTab(page);
  await expect(pane.locator(".set-transcribe-piano-engine")).toHaveValue("piano_hr");
  await expect(pane.locator(".transcription-piano-engine-note")).toHaveText("Its model is on this computer (172 MB).");
});

test("a Keys engine that cannot run here says so and can be changed back", async ({ page }) => {
  await stubServer(page, { status: withHr({ available: false, reason: "no_runtime" }), fields: { transcribe_piano_engine: "piano_hr" } });
  const pane = await openTab(page);
  await expect(pane.locator(".transcription-piano-engine-note")).toHaveText(
    "ByteDance Piano Transcription cannot run on this computer. Choose Basic Pitch to transcribe the Piano lane.",
  );
  // Transcription itself runs: nothing else is turned off.
  await expect(pane.locator(".transcription-unavailable")).toBeHidden();
  await expect(pane.locator(".set-transcribe-piano-engine")).toBeEnabled();
});

test("a change is saved to the server and shown", async ({ page }) => {
  const server = await stubServer(page, { fields: { transcribe_bass: "off" } });
  const pane = await openTab(page);
  await expect(pane.locator(".set-transcribe-bass")).toHaveValue("off");
  await pane.locator(".set-transcribe-piano").selectOption("off");
  await expect.poll(() => server.posts.at(-1)).toEqual({ transcribe_piano: "off" });
  await pane.locator(".set-transcribe-detail").selectOption("clean");
  await expect.poll(() => server.posts.at(-1)).toEqual({ transcribe_detail: "clean" });
  await expect(pane.locator('.transcription-detail-desc[data-detail="clean"]')).toHaveClass(/is-current/);
  await expect(pane.locator('.transcription-detail-desc[data-detail="balanced"]')).not.toHaveClass(/is-current/);
  expect(server.fields).toEqual({ transcribe_piano: "off", transcribe_bass: "off", transcribe_detail: "clean" });
  // Opened again, the dialog shows what the server holds.
  await page.keyboard.press("Escape");
  await page.locator("#settingsBtn").click();
  await page.locator('.settings-tab[data-tab="transcription"]').click();
  await expect(page.locator(".set-transcribe-piano")).toHaveValue("off");
  await expect(page.locator(".set-transcribe-detail")).toHaveValue("clean");
});

test("a refused save shows the setting still in force, and says so", async ({ page }) => {
  await stubServer(page, { failSave: true });
  const pane = await openTab(page);
  await pane.locator(".set-transcribe-bass").selectOption("off");
  await expect(pane.locator(".transcription-msg")).toHaveText("Could not save the setting. The list shows the setting still in use.");
  await expect(pane.locator(".set-transcribe-bass")).toHaveValue("auto");
});

test("an Intel Mac: the reason in words, and the selects off", async ({ page }) => {
  await stubServer(page, { status: { ...STATUS, available: false, reason: "unsupported_platform" } });
  const pane = await openTab(page);
  await expect(pane.locator(".transcription-unavailable")).toHaveText("Transcription is not available on this computer (Intel Mac).");
  for (const sel of [".set-transcribe-piano", ".set-transcribe-bass", ".set-transcribe-detail"]) {
    await expect(pane.locator(sel)).toBeDisabled();
  }
});

test("an engine that cannot start says so", async ({ page }) => {
  await stubServer(page, { status: { ...STATUS, available: false, reason: "no_runtime" } });
  const pane = await openTab(page);
  await expect(pane.locator(".transcription-unavailable")).toHaveText("The transcription engine could not start.");
  await expect(pane.locator(".set-transcribe-piano")).toBeDisabled();
});

test("a server without transcription says it could not check, and leaves the choices", async ({ page }) => {
  await stubServer(page, { status: null });
  const pane = await openTab(page);
  await expect(pane.locator(".transcription-unavailable")).toHaveText("StemDeck could not check whether transcription can run on this computer.");
  await expect(pane.locator(".transcription-engine")).toBeHidden();
  await expect(pane.locator(".set-transcribe-piano")).toBeEnabled();
});

test("search finds the Detail setting in the Transcription tab", async ({ page }) => {
  await stubServer(page);
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  await page.locator(".settings-search-input").fill("stray");
  const result = page.locator(".settings-result").first();
  await expect(result).toContainText("Detail");
  await result.click();
  await expect(page.locator('.settings-pane[data-pane="transcription"]')).toBeVisible();
});
