// Settings > Models (static/js/modelsSettings.js, modelsView.js): every
// built-in model by purpose, with its licence, its state on disk and what it
// is used for now, and the links from Separation and Transcription that open
// the tab. My models in the same tab is tests/e2e/my-models.spec.mjs.
//
// GET /api/models and the plan fields of /api/settings are answered here, so
// the e2e backend's own model folders (none downloaded) do not decide what is
// shown, and nothing is written to the shared backend. The words are pinned
// in tests/js/models-view.test.mjs; the route in tests/test_models_api.py.

import { test, expect } from "@playwright/test";
import { seedLibrary } from "./helpers.mjs";

const entry = (id, group, use, extra = {}) => ({
  id, group, use, name: id, stems: [], license: "MIT", state: "not_downloaded",
  size_bytes: null, download_bytes: null, available: true, ...extra,
});
const MODELS = {
  models: [
    entry("htdemucs_6s", "separation", "stems", { name: "HT Demucs 6 stems", stems: ["vocals", "drums", "bass", "guitar", "piano", "other"], state: "downloaded", size_bytes: 54_996_327, download_bytes: 54_996_327, source: { author: "Meta AI (Rouard, Massa, Défossez)", url: "https://github.com/facebookresearch/demucs" } }),
    entry("htdemucs_ft", "separation", "stems", { name: "HT Demucs fine-tuned", stems: ["vocals", "drums", "bass", "other"], state: "partial", size_bytes: 84_141_271, download_bytes: 336_565_084 }),
    entry("kim_mel_roformer", "separation", "stems", { name: "Mel-Band RoFormer (Kim)", stems: ["vocals"], download_bytes: 913_106_900, available: false }),
    entry("uvr_karaoke_2", "separation", "lead_backing", { name: "UVR-MDX-NET Karaoke 2", stems: ["lead_backing"] }),
    entry("basic_pitch", "transcription", "midi", { name: "Basic Pitch", stems: ["vocals", "bass", "guitar", "piano", "other"], license: "Apache-2.0", state: "built_in", size_bytes: 230_000 }),
    entry("piano_hr", "transcription", "midi_piano", { name: "ByteDance Piano Transcription", stems: ["piano"], license: "CC BY 4.0", download_bytes: 171_966_578, source: { author: "Qiuqiang Kong (ByteDance)", url: "https://zenodo.org/records/4034264" } }),
    entry("ymt3", "transcription", "midi_multi", { name: "YourMT3+", stems: ["drums", "bass", "guitar", "piano", "other"], license: "Apache-2.0", download_bytes: 561_544_628 }),
    entry("whisper_turbo", "analysis", "lyrics_gpu", { name: "Whisper turbo", download_bytes: 1_617_941_637 }),
    entry("whisper_small", "analysis", "lyrics_cpu", { name: "Whisper small", state: "downloaded", size_bytes: 483_617_219 }),
    entry("beat_this", "analysis", "beats", { name: "Beat This! (final0)", license: null, state: "unknown" }),
  ],
  remove: false,
};
const STEMS5 = ["vocals", "drums", "bass", "guitar", "piano"];

async function stubServer(page, { models = MODELS } = {}) {
  const state = { plan: Object.fromEntries(STEMS5.map((s) => [s, "htdemucs_6s"])), midi: {
    transcribe_vocals: "auto", transcribe_drums: "auto", transcribe_bass: "off", transcribe_guitar: "auto",
    transcribe_piano: "auto", transcribe_other: "auto",
    transcribe_vocals_engine: "basic_pitch", transcribe_drums_engine: "ymt3", transcribe_bass_engine: "ymt3",
    transcribe_guitar_engine: "basic_pitch", transcribe_piano_engine: "ymt3", transcribe_other_engine: "ymt3",
  } };
  await page.route("**/api/models", (route) =>
    models ? route.fulfill({ json: models }) : route.fulfill({ status: 404, json: { detail: "Not Found" } }));
  await page.route(/\/api\/my-models(\/.*)?$/, (route) => route.fulfill({ status: 404, json: { detail: "Not Found" } }));
  await page.route("**/api/settings", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const res = await route.fetch();
    return route.fulfill({ response: res, json: { ...(await res.json()), stem_models_resolved: state.plan, ...state.midi } });
  });
  return state;
}

async function openModels(page) {
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  await page.locator('.settings-tab[data-tab="models"]').click();
  const pane = page.locator('.settings-pane[data-pane="models"]');
  await expect(pane).toBeVisible();
  await expect(pane.locator(".models-builtin-group")).toBeVisible();
  return pane;
}

const row = (page, id) => page.locator(`.models-row[data-model="${id}"]`);

test("the tab sits after Transcription, with its icon and title", async ({ page }) => {
  await stubServer(page);
  await openModels(page);
  const tabs = await page.locator(".settings-tab").evaluateAll((els) => els.map((e) => e.dataset.tab));
  expect(tabs.indexOf("models")).toBe(tabs.indexOf("transcription") + 1);
  await expect(page.locator('.settings-tab[data-tab="models"]')).toHaveText("Models");
  await expect(page.locator('.settings-tab[data-tab="models"] svg.settings-tab-icon')).toHaveCount(1);
  await expect(page.locator(".settings-main-title")).toHaveText("Models");
});

test("the built-in models are grouped by purpose, each with its state and size", async ({ page }) => {
  await stubServer(page);
  const pane = await openModels(page);
  await expect(pane.locator(".models-group-title")).toHaveText(["Separation", "Transcription", "Lyrics and beat grid"]);
  await expect(pane.locator('.models-section[data-group="separation"] .models-row')).toHaveCount(4);
  await expect(row(page, "htdemucs_6s").locator(".models-state")).toHaveText("Downloaded, 55 MB");
  await expect(row(page, "htdemucs_ft").locator(".models-state")).toHaveText("Partly downloaded, 84 MB");
  await expect(row(page, "kim_mel_roformer").locator(".models-state")).toHaveText("Cannot run here");
  await expect(row(page, "uvr_karaoke_2").locator(".models-state")).toHaveText("Not downloaded");
  await expect(row(page, "basic_pitch").locator(".models-state")).toHaveText("Built in");
  await expect(row(page, "piano_hr").locator(".models-state")).toHaveText("Not downloaded, 172 MB to download");
  await expect(row(page, "piano_hr").locator(".models-purpose")).toHaveText("Makes MIDI notes from the Piano lane when ByteDance Piano Transcription is its engine.");
  await expect(row(page, "ymt3").locator(".models-state")).toHaveText("Not downloaded, 562 MB to download");
  await expect(row(page, "ymt3").locator(".models-purpose")).toHaveText(
    "Makes MIDI notes from the Drums, Bass, Guitar, Piano and Other lanes when YourMT3+ is their engine, each keeping only its own instrument.",
  );
  await expect(row(page, "ymt3").locator(".models-licence")).toHaveText("Licence: Apache-2.0.");
  await expect(row(page, "piano_hr").locator(".models-licence")).toHaveText("Licence: CC BY 4.0.");
  await expect(row(page, "whisper_turbo").locator(".models-state")).toHaveText("Not downloaded, 1,618 MB to download");
  await expect(row(page, "beat_this").locator(".models-state")).toHaveText("Not known");
  await expect(row(page, "htdemucs_6s").locator(".models-purpose")).toHaveText("Separates Vocals, Drums, Bass, Guitar, Piano, and Other.");
  await expect(row(page, "basic_pitch").locator(".models-licence")).toHaveText("Licence: Apache-2.0.");
  await expect(row(page, "beat_this").locator(".models-licence")).toBeHidden();
  await expect(pane.locator(".models-remove-note")).toContainText("cannot be removed here");
  // Read-only: no button in a built-in row.
  await expect(pane.locator(".models-row button")).toHaveCount(0);
});

test("each row names its source, linked to the author's own page", async ({ page }) => {
  await stubServer(page);
  await openModels(page);
  const six = row(page, "htdemucs_6s").locator(".models-source");
  await expect(six).toHaveText("Source: Meta AI (Rouard, Massa, Défossez), github.com/facebookresearch/demucs ↗");
  const link = six.locator("a.models-source-link");
  await expect(link).toHaveAttribute("href", "https://github.com/facebookresearch/demucs");
  // target="_blank" is what main.js hands to the desktop app's open_url.
  await expect(link).toHaveAttribute("target", "_blank");
  await expect(link).toHaveAttribute("rel", "noopener noreferrer");
  await expect(row(page, "piano_hr").locator(".models-source a")).toHaveAttribute("href", "https://zenodo.org/records/4034264");
  // An entry with no verified source says so, with no link.
  await expect(row(page, "uvr_karaoke_2").locator(".models-source")).toHaveText("Source: not verified");
  await expect(row(page, "uvr_karaoke_2").locator(".models-source a")).toHaveCount(0);
});

test("Used for follows the per-stem choices and the MIDI lanes", async ({ page }) => {
  await stubServer(page);
  await openModels(page);
  await expect(row(page, "htdemucs_6s").locator(".models-used")).toHaveText("Used for: Vocals, Drums, Bass, Guitar, and Piano.");
  await expect(row(page, "htdemucs_ft").locator(".models-used")).toHaveText("Not used for any stem now.");
  await expect(row(page, "uvr_karaoke_2").locator(".models-used")).toHaveText("Used for: Lead + Backing.");
  // Each lane's engine: Basic Pitch for the vocals and the guitar, YourMT3+
  // for the rest but the bass, which is off; ByteDance's is no lane's.
  await expect(row(page, "basic_pitch").locator(".models-used")).toHaveText("Used for: Vocals and Guitar.");
  await expect(row(page, "ymt3").locator(".models-used")).toHaveText("Used for: Drums, Piano, and Other.");
  await expect(row(page, "piano_hr").locator(".models-used")).toHaveText("Not used for any stem now.");
  await expect(row(page, "whisper_turbo").locator(".models-used")).toBeHidden();
});

test("a server without the route shows no built-in group and no error", async ({ page }) => {
  const errors = [];
  page.on("pageerror", (err) => errors.push(String(err)));
  await stubServer(page, { models: null });
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  await page.locator('.settings-tab[data-tab="models"]').click();
  const group = page.locator(".models-builtin-group");
  await expect(group).toHaveCount(1);
  await expect(group).toBeHidden();
  expect(errors).toEqual([]);
});

test("the links in Separation and Transcription open the Models tab", async ({ page }) => {
  await stubServer(page);
  await openModels(page);
  for (const [tab, text] of [["separation", "Add or remove models in Models"], ["transcription", "See the transcription models in Models"]]) {
    await page.locator(`.settings-tab[data-tab="${tab}"]`).click();
    const link = page.locator(`.settings-pane[data-pane="${tab}"] .models-open`);
    await expect(link).toHaveText(text);
    await link.click();
    await expect(page.locator(".settings-tab.active")).toHaveAttribute("data-tab", "models");
    await expect(page.locator('.settings-tab[data-tab="models"]')).toBeFocused();
  }
});

test("search finds a built-in model and opens the Models tab at it", async ({ page }) => {
  await stubServer(page);
  await openModels(page);
  await page.locator('.settings-tab[data-tab="general"]').click();
  await page.locator(".settings-search-input").fill("Whisper small");
  const result = page.locator(".settings-result", { hasText: "Whisper small" });
  await expect(result).toHaveCount(1);
  await result.click();
  await expect(page.locator(".settings-tab.active")).toHaveAttribute("data-tab", "models");
  await expect(row(page, "whisper_small")).toBeInViewport();
});

test("in European Portuguese the rows use its own words", async ({ page }) => {
  await stubServer(page);
  await page.addInitScript(() => localStorage.setItem("stemdeck.language", JSON.stringify("pt-PT")));
  await openModels(page);
  await expect(row(page, "htdemucs_6s").locator(".models-state")).toHaveText("Transferido, 55 MB");
  await expect(page.locator(".models-group-title").first()).toHaveText("Separação");
});

test("at phone width the rows stack and nothing runs off the side", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await stubServer(page);
  const pane = await openModels(page);
  const overflow = await pane.evaluate((el) => el.scrollWidth - el.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  for (const id of ["htdemucs_6s", "whisper_turbo"]) {
    const box = await row(page, id).locator(".models-state").boundingBox();
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(375);
  }
});

// ── The Download button (app/core/model_get.py behind it) ──
//
// The server's run is played here: POST /api/models/{id}/download starts it,
// GET /api/models/download moves it on one step per poll, Cancel stops it,
// and the list read again at the end has the model downloaded. Nothing is
// fetched and nothing reaches the shared backend.

async function downloadServer(page, { steps, refuse = null } = {}) {
  const models = structuredClone(MODELS);
  for (const m of models.models) if (["kim_mel_roformer", "piano_hr", "htdemucs_ft"].includes(m.id)) m.downloadable = true;
  models.models.find((m) => m.id === "kim_mel_roformer").available = true;
  const server = { models, run: { state: "idle", model: null, phase: null, bytes: 0, total: null, error_code: null }, posts: [], cancelled: false, polls: 0 };
  await page.route("**/api/models", (route) => route.fulfill({ json: { ...server.models, download: server.run } }));
  await page.route("**/api/models/download", (route) => {
    server.polls += 1;
    if (server.run.state === "running" && !server.cancelled) {
      const next = steps.shift();
      if (next) Object.assign(server.run, next);
      if (server.run.state === "done") {
        const m = server.models.models.find((e) => e.id === server.run.model);
        Object.assign(m, { state: "downloaded", size_bytes: m.download_bytes });
      }
    }
    return route.fulfill({ json: server.run });
  });
  await page.route(/\/api\/models\/[a-z0-9_]+\/download$/, (route) => {
    const id = route.request().url().split("/").slice(-2)[0];
    server.posts.push(id);
    if (refuse) return route.fulfill({ status: refuse.status, json: { detail: "x", code: refuse.code } });
    const m = server.models.models.find((e) => e.id === id);
    server.run = { state: "running", model: id, phase: "download", bytes: 0, total: m.download_bytes, error_code: null };
    return route.fulfill({ status: 202, json: { state: "running" } });
  });
  await page.route("**/api/models/download/cancel", (route) => {
    server.cancelled = true;
    server.run = { ...server.run, state: "cancelled", phase: null };
    return route.fulfill({ json: server.run });
  });
  return server;
}

test("Download beside Not downloaded: progress, then Downloaded and no button", async ({ page }) => {
  await stubServer(page);
  const server = await downloadServer(page, {
    steps: [
      { bytes: 120_400_000 },
      { bytes: 913_106_900 },
      { state: "done", phase: null, bytes: 913_106_900 },
    ],
  });
  await openModels(page);
  const kim = row(page, "kim_mel_roformer");
  const get = kim.locator("button.models-download");
  await expect(get).toHaveText("Download");
  await expect(get).toHaveAttribute("aria-label", "Download Mel-Band RoFormer (Kim)");
  // A real button, beside the label, not the label itself.
  await expect(kim.locator(".models-state")).toHaveText("Not downloaded, 913 MB to download");
  // Partly downloaded has one too; downloaded, built-in and not downloadable rows do not.
  await expect(row(page, "htdemucs_ft").locator("button.models-download")).toHaveCount(1);
  for (const id of ["htdemucs_6s", "basic_pitch", "whisper_turbo", "beat_this"]) {
    await expect(row(page, id).locator("button")).toHaveCount(0);
  }
  await get.click();
  await expect(kim.locator(".models-state")).toHaveText("Downloading 120 of 913 MB");
  const cancel = kim.locator("button.models-cancel");
  await expect(cancel).toHaveText("Cancel");
  // One at a time: the other Download waits.
  await expect(row(page, "piano_hr").locator("button.models-download")).toBeDisabled();
  await expect(kim.locator(".models-state")).toHaveText("Downloaded, 913 MB");
  await expect(kim.locator("button")).toHaveCount(0);
  await expect(row(page, "piano_hr").locator("button.models-download")).toBeEnabled();
  expect(server.posts).toEqual(["kim_mel_roformer"]);
});

test("Cancel stops the download and Download comes back", async ({ page }) => {
  await stubServer(page);
  const server = await downloadServer(page, { steps: [{ bytes: 50_000_000 }, { bytes: 60_000_000 }, { bytes: 70_000_000 }, { bytes: 80_000_000 }] });
  await openModels(page);
  const hr = row(page, "piano_hr");
  await hr.locator("button.models-download").click();
  await expect(hr.locator(".models-state")).toHaveText(/^Downloading \d+ of 172 MB$/);
  await hr.locator("button.models-cancel").click();
  expect(server.cancelled).toBe(true);
  await expect(hr.locator(".models-state")).toHaveText("Not downloaded, 172 MB to download");
  await expect(hr.locator("button.models-download")).toBeEnabled();
  await expect(hr.locator(".models-download-msg")).toBeHidden();
});

test("a failed download says why in the page's words, never the server's", async ({ page }) => {
  await stubServer(page);
  await downloadServer(page, { steps: [{ state: "failed", phase: null, error_code: "network" }] });
  await openModels(page);
  const kim = row(page, "kim_mel_roformer");
  await kim.locator("button.models-download").click();
  await expect(kim.locator(".models-download-msg")).toHaveText(
    "StemDeck could not reach the download server. Check the internet connection and try again.",
  );
  await expect(kim.locator("button.models-download")).toBeEnabled();
});

test("a refused start says it is busy, on its own row", async ({ page }) => {
  await stubServer(page);
  await downloadServer(page, { steps: [], refuse: { status: 409, code: "busy" } });
  await openModels(page);
  const kim = row(page, "kim_mel_roformer");
  await kim.locator("button.models-download").click();
  await expect(kim.locator(".models-download-msg")).toHaveText(
    "StemDeck is downloading another model, separating a song or making MIDI. Try again when it has finished.",
  );
  await expect(row(page, "piano_hr").locator(".models-download-msg")).toBeHidden();
});

test("in European Portuguese the button is Transferir", async ({ page }) => {
  await stubServer(page);
  await downloadServer(page, { steps: [{ bytes: 120_400_000 }, { bytes: 130_000_000 }] });
  await page.addInitScript(() => localStorage.setItem("stemdeck.language", JSON.stringify("pt-PT")));
  await openModels(page);
  const kim = row(page, "kim_mel_roformer");
  await expect(kim.locator("button.models-download")).toHaveText("Transferir");
  await kim.locator("button.models-download").click();
  await expect(kim.locator(".models-state")).toHaveText(/^A transferir \d+ de 913 MB$/);
});

test("at phone width a row's state and Download stay on the screen", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await stubServer(page);
  await downloadServer(page, { steps: [] });
  const pane = await openModels(page);
  const overflow = await pane.evaluate((el) => el.scrollWidth - el.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  const box = await row(page, "kim_mel_roformer").locator("button.models-download").boundingBox();
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(375);
});
