// Settings > Transcription (static/js/transcriptionSettings.js): MIDI from
// every lane, each with its own engine.
//
// Driven in a real browser because the controls are the risk: each lane's
// two selects have to show what the server holds (Auto and the lane's
// default engine), each engine select has to list only the engines that can
// take its lane, a change has to reach the server, a refused save has to show
// the setting still in force, the reason the engine cannot run here has to be
// in words with the selects off, and an engine that downloads its model has
// to say so, with its size and how slow it is on this computer's processor,
// before it ever runs. GET /api/transcribe/status and the Transcription fields
// of /api/settings are answered here, so nothing writes to the shared backend.

import { test, expect } from "@playwright/test";
import { seedLibrary } from "./helpers.mjs";

const LANES = ["vocals", "drums", "bass", "guitar", "piano", "other"];
const NAMES = { vocals: "Vocals", drums: "Drums", bass: "Bass", guitar: "Guitar", piano: "Piano", other: "Other" };
const BP = {
  id: "basic_pitch", label: "Basic Pitch", version: "0.4.0", licence: "Apache-2.0", model_licence: "Apache-2.0",
  stems: ["vocals", "bass", "guitar", "piano", "other"], available: true, reason: null, source: "built_in",
  model: "built_in", download_bytes: null, disk_bytes: null, device: "cpu", seconds_per_minute: null, uses_detail: true,
};
const HR = {
  id: "piano_hr", label: "ByteDance Piano Transcription", version: "0.0.6", licence: "MIT", model_licence: "CC BY 4.0",
  stems: ["piano"], available: true, reason: null, source: "built_in", model: "not_downloaded",
  download_bytes: 171_966_578, disk_bytes: 171_966_578, device: "cpu", seconds_per_minute: null, uses_detail: true,
};
const YM = {
  id: "ymt3", label: "YourMT3+", version: "5e66c1e", licence: "Apache-2.0", model_licence: "Apache-2.0",
  stems: ["drums", "bass", "guitar", "piano", "other"], available: true, reason: null, source: "built_in",
  model: "not_downloaded", download_bytes: 561_544_628, disk_bytes: 188_412_704, device: "cpu",
  seconds_per_minute: { cuda: 12, cpu: 69 }, uses_detail: false,
};
const STEM_ENGINES = {
  vocals: ["basic_pitch"], drums: ["ymt3"], bass: ["basic_pitch", "ymt3"], guitar: ["basic_pitch", "ymt3"],
  piano: ["basic_pitch", "piano_hr", "ymt3"], other: ["basic_pitch", "ymt3"],
};
const DEFAULTS = { vocals: "basic_pitch", drums: "ymt3", bass: "ymt3", guitar: "basic_pitch", piano: "ymt3", other: "ymt3" };
const STATUS = {
  available: true, reason: null, stems: LANES, max_seconds: 1200, details: ["balanced", "detailed", "clean"],
  engines: [BP, HR, YM], stem_engines: STEM_ENGINES, default_engines: DEFAULTS,
};
const withYm = (over) => ({ ...STATUS, engines: [BP, HR, { ...YM, ...over }] });

/** The fake: `fields` are the Transcription settings, `posts` every save, `failSave` refuses them. */
async function stubServer(page, { status = STATUS, fields = {}, failSave = false } = {}) {
  const defaults = { transcribe_detail: "balanced" };
  for (const lane of LANES) {
    defaults[`transcribe_${lane}`] = "auto";
    defaults[`transcribe_${lane}_engine`] = DEFAULTS[lane];
  }
  const state = { fields: { ...defaults, ...fields }, posts: [] };
  await page.route("**/api/transcribe/status", (route) =>
    status ? route.fulfill({ json: status }) : route.fulfill({ status: 404, json: { detail: "Not Found" } }));
  await page.route("**/api/settings", async (route) => {
    const req = route.request();
    if (req.method() === "POST") {
      const body = JSON.parse(req.postData() || "{}");
      if (!Object.keys(body).some((k) => k.startsWith("transcribe_") && k !== "transcribe_lyrics")) return route.continue();
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

const laneSel = (pane, lane) => pane.locator(`select[data-field="transcribe_${lane}"]`);
const engineSel = (pane, lane) => pane.locator(`select[data-field="transcribe_${lane}_engine"]`);
const note = (pane, lane) => pane.locator(`.transcription-engine-note[data-lane="${lane}"]`);
const shownEngines = (sel) => sel.locator("option").evaluateAll((els) => els.filter((o) => !o.hidden).map((o) => o.textContent));

test("the tab sits after Separation, with its notice, its engines and a row per lane", async ({ page }) => {
  await stubServer(page);
  const pane = await openTab(page);
  const tabs = await page.locator(".settings-tab").evaluateAll((els) => els.map((e) => e.dataset.tab));
  expect(tabs.indexOf("transcription")).toBe(tabs.indexOf("separation") + 1);
  await expect(page.locator('.settings-tab[data-tab="transcription"]')).toHaveText("Transcription");
  await expect(page.locator(".settings-main-title")).toHaveText("Transcription");
  await expect(pane).toContainText("Transcription runs on this computer and sends nothing anywhere.");
  await expect(pane).toContainText("Each lane has its own engine.");
  await expect(pane).not.toContainText(/\bwe\b/i);
  await expect(pane.locator(".transcription-engine")).toHaveText(
    "Engine: Basic Pitch 0.4.0, licence Apache-2.0. Engine: ByteDance Piano Transcription 0.0.6, licence MIT, model licence CC BY 4.0. Engine: YourMT3+ 5e66c1e, licence Apache-2.0.",
  );
  await expect(pane.locator(".transcription-unavailable")).toBeHidden();
  await expect(pane.locator(".transcription-lane .settings-row-title")).toHaveText(LANES.map((l) => NAMES[l]));
  for (const lane of LANES) {
    await expect(laneSel(pane, lane)).toHaveValue("auto");
    await expect(laneSel(pane, lane).locator("option")).toHaveText(["Auto", "Off"]);
    await expect(engineSel(pane, lane)).toHaveValue(DEFAULTS[lane]);
    await expect(engineSel(pane, lane)).toHaveAttribute("aria-label", `${NAMES[lane]} engine`);
    await expect(pane.locator(`.transcription-lane-desc[data-lane="${lane}"]`)).toHaveText(
      `The MIDI button on the ${NAMES[lane]} lane, in Advanced mode, and the engine that writes its notes. Off hides the button.`,
    );
    await expect(laneSel(pane, lane)).toBeEnabled();
  }
  // Only the engines that can take each lane.
  expect(await shownEngines(engineSel(pane, "vocals"))).toEqual(["Basic Pitch"]);
  expect(await shownEngines(engineSel(pane, "drums"))).toEqual(["YourMT3+"]);
  expect(await shownEngines(engineSel(pane, "piano"))).toEqual(["Basic Pitch", "ByteDance Piano Transcription", "YourMT3+"]);
  expect(await shownEngines(engineSel(pane, "guitar"))).toEqual(["Basic Pitch", "YourMT3+"]);
  await expect(pane.locator(".set-transcribe-detail")).toHaveValue("balanced");
  await expect(pane.locator(".set-transcribe-detail option")).toHaveText(["Balanced", "Detailed", "Clean"]);
  await expect(pane.locator('.transcription-detail-desc[data-detail="balanced"]')).toHaveClass(/is-current/);
  // YourMT3+ is in use and Detail does not change its notes: said under Detail.
  await expect(pane.locator(".transcription-detail-not-for")).toHaveText(
    "YourMT3+ has no Detail setting: its notes are the same at every choice.",
  );
});

test("before a first run, a downloading engine says its size and its speed on the processor", async ({ page }) => {
  await stubServer(page);
  const pane = await openTab(page);
  // Basic Pitch is built in: nothing to say.
  await expect(note(pane, "vocals")).toBeHidden();
  await expect(note(pane, "drums")).toHaveText(
    "The first time, YourMT3+ downloads its model (562 MB) and then keeps it on this computer. "
    + "On this computer it runs on the processor, which is slow: about 69 seconds of work per minute of song, so a 4 minute song takes about 5 minutes.",
  );
});

test("a downloaded model on a graphics card says what it keeps and how fast it is", async ({ page }) => {
  await stubServer(page, { status: withYm({ model: "downloaded", device: "cuda" }) });
  const pane = await openTab(page);
  await expect(note(pane, "piano")).toHaveText(
    "The model of YourMT3+ is on this computer (188 MB). On this computer it runs on the graphics card: about 12 seconds of work per minute of song.",
  );
});

test("a lane's engine is saved, and its note follows", async ({ page }) => {
  const server = await stubServer(page);
  const pane = await openTab(page);
  await engineSel(pane, "guitar").selectOption("ymt3");
  await expect.poll(() => server.posts.at(-1)).toEqual({ transcribe_guitar_engine: "ymt3" });
  await expect(note(pane, "guitar")).toContainText("downloads its model (562 MB)");
  await engineSel(pane, "piano").selectOption("piano_hr");
  await expect.poll(() => server.posts.at(-1)).toEqual({ transcribe_piano_engine: "piano_hr" });
  await expect(note(pane, "piano")).toHaveText(
    "The first time, ByteDance Piano Transcription downloads its model (172 MB) and then keeps it on this computer.",
  );
  await engineSel(pane, "guitar").selectOption("basic_pitch");
  await expect.poll(() => server.posts.at(-1)).toEqual({ transcribe_guitar_engine: "basic_pitch" });
  await expect(note(pane, "guitar")).toBeHidden();
});

test("an engine that cannot run here says so, and transcription itself still runs", async ({ page }) => {
  await stubServer(page, { status: withYm({ available: false, reason: "no_runtime" }) });
  const pane = await openTab(page);
  await expect(note(pane, "drums")).toHaveText("YourMT3+ cannot run on this computer.");
  await expect(pane.locator(".transcription-unavailable")).toBeHidden();
  await expect(engineSel(pane, "bass")).toBeEnabled();
});

test("a change is saved to the server and shown", async ({ page }) => {
  const server = await stubServer(page, { fields: { transcribe_bass: "off" } });
  const pane = await openTab(page);
  await expect(laneSel(pane, "bass")).toHaveValue("off");
  await laneSel(pane, "drums").selectOption("off");
  await expect.poll(() => server.posts.at(-1)).toEqual({ transcribe_drums: "off" });
  await pane.locator(".set-transcribe-detail").selectOption("clean");
  await expect.poll(() => server.posts.at(-1)).toEqual({ transcribe_detail: "clean" });
  await expect(pane.locator('.transcription-detail-desc[data-detail="clean"]')).toHaveClass(/is-current/);
  expect(server.fields.transcribe_drums).toBe("off");
  // Opened again, the dialog shows what the server holds.
  await page.keyboard.press("Escape");
  await page.locator("#settingsBtn").click();
  await page.locator('.settings-tab[data-tab="transcription"]').click();
  await expect(laneSel(page, "drums")).toHaveValue("off");
  await expect(page.locator(".set-transcribe-detail")).toHaveValue("clean");
});

test("a refused save shows the setting still in force, and says so", async ({ page }) => {
  await stubServer(page, { failSave: true });
  const pane = await openTab(page);
  await laneSel(pane, "bass").selectOption("off");
  await expect(pane.locator(".transcription-msg")).toHaveText("Could not save the setting. The list shows the setting still in use.");
  await expect(laneSel(pane, "bass")).toHaveValue("auto");
});

test("an Intel Mac: the reason in words, and the selects off", async ({ page }) => {
  await stubServer(page, { status: { ...STATUS, available: false, reason: "unsupported_platform" } });
  const pane = await openTab(page);
  await expect(pane.locator(".transcription-unavailable")).toHaveText("Transcription is not available on this computer (Intel Mac).");
  for (const lane of LANES) {
    await expect(laneSel(pane, lane)).toBeDisabled();
    await expect(engineSel(pane, lane)).toBeDisabled();
  }
  await expect(pane.locator(".set-transcribe-detail")).toBeDisabled();
});

test("an engine that cannot start says so", async ({ page }) => {
  await stubServer(page, { status: { ...STATUS, available: false, reason: "no_runtime" } });
  const pane = await openTab(page);
  await expect(pane.locator(".transcription-unavailable")).toHaveText("The transcription engine could not start.");
  await expect(laneSel(pane, "piano")).toBeDisabled();
});

test("a server without transcription says it could not check, and leaves the choices", async ({ page }) => {
  await stubServer(page, { status: null });
  const pane = await openTab(page);
  await expect(pane.locator(".transcription-unavailable")).toHaveText("StemDeck could not check whether transcription can run on this computer.");
  await expect(pane.locator(".transcription-engine")).toBeHidden();
  await expect(laneSel(pane, "piano")).toBeEnabled();
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
