// The Model tab on a lane and its row (Advanced mode): which lanes get one,
// what the tab says, what the select lists and starts at, and what a run's
// line says (static/js/laneModelView.js). Pure, so it runs without a browser;
// tests/e2e/lane-model.spec.mjs checks the drawing.
//
// Run:  node tests/js/lane-model-view.test.mjs

import { TRANSLATIONS } from "../../static/js/i18n.js";
import { CHOICE_STEMS } from "../../static/js/stemModelsView.js";
import {
  BASE_MODEL,
  BLOCK_RUNNING,
  MODEL_LANES,
  REASON_CODES,
  differsFromSettings,
  hasUndo,
  initialChoice,
  laneHasModelTab,
  laneOptions,
  modelTabFace,
  reasonKey,
  reextractBlock,
  refusalCode,
  runLine,
  runPct,
  runRunning,
  settingsStemModel,
  songStemModel,
  splitWasReset,
  stickyRefusal,
  tabLabel,
  UNDO_REASON_CODES,
  undoModel,
} from "../../static/js/laneModelView.js";

let pass = 0,
  fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) {
    pass++;
    console.log(`PASS  ${name}`);
  } else {
    fail++;
    console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`);
  }
};
const eq = (name, actual, expected) =>
  check(name, JSON.stringify(actual) === JSON.stringify(expected), `got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)}`);

const en = TRANSLATIONS.en;
const t = (key, vars = {}) => String(en[key] ?? key).replace(/\{(\w+)\}/g, (_, k) => (k in vars ? String(vars[k]) : `{${k}}`));

const USER = "user_0123456789ab";
const MODELS = {
  htdemucs_6s: { name: "HTDemucs 6s", available: true },
  htdemucs_ft: { name: "HTDemucs FT", available: true },
  kim_mel_roformer: { name: "Kim Mel-RoFormer", available: true },
  [USER]: { name: "My Guitar", user: true, available: true },
};
function settings({ saved = {}, models = MODELS } = {}) {
  const auto = Object.fromEntries(CHOICE_STEMS.map((s) => [s, "auto"]));
  const recommended = { vocals: "kim_mel_roformer", drums: "htdemucs_6s", bass: "htdemucs_6s", guitar: "htdemucs_6s", piano: "htdemucs_6s" };
  const stem_models = { ...auto, ...saved };
  const resolved = Object.fromEntries(CHOICE_STEMS.map((s) => [s, stem_models[s] === "auto" ? recommended[s] : stem_models[s]]));
  return {
    stem_models,
    stem_models_resolved: resolved,
    stem_models_recommended: recommended,
    stem_models_reason: "gpu",
    stem_model_catalog: {
      stems: [
        { stem: "vocals", choices: ["htdemucs_6s", "kim_mel_roformer"] },
        { stem: "lead_backing", fixed: "uvr_karaoke_2", available: true },
        { stem: "drums", choices: ["htdemucs_6s", "htdemucs_ft"] },
        { stem: "bass", choices: ["htdemucs_6s", "htdemucs_ft"] },
        { stem: "guitar", choices: ["htdemucs_6s", USER] },
        { stem: "piano", fixed: "htdemucs_6s" },
        { stem: "other", fixed: "residual" },
      ],
      models,
    },
  };
}
const job = (over = {}) => ({ job_id: "abc123", stem_models: { vocals: "kim_mel_roformer", drums: "htdemucs_6s", bass: "htdemucs_6s", guitar: "htdemucs_6s", piano: "htdemucs_6s" }, stem_undo: {}, stem_reextract: null, ...over });

// ─── The tab ───

{
  const face = modelTabFace();
  check("the tab's face is one SVG", face.startsWith("<svg") && face.endsWith("</svg>"));
  check("drawn like the rail icons: viewBox 24, stroke 1.6, round caps, currentColor",
    face.includes('viewBox="0 0 24 24"') && face.includes('stroke-width="1.6"') && face.includes('stroke-linecap="round"') && face.includes('stroke="currentColor"'));
  check("a body with pins on two sides", face.includes("<rect") && /M4 [\d.]+h3/.test(face) && /M17 [\d.]+h3/.test(face));
  check("hidden from screen readers (the button carries the name)", face.includes('aria-hidden="true"'));
}

eq("the lanes with a tab are the stems Settings chooses a model for", [...MODEL_LANES], ["vocals", "drums", "bass", "guitar", "piano"]);
for (const lane of ["other", "lead_vocals", "backing_vocals", "original", "__master", "take_1"]) {
  check(`no tab on ${lane}`, !laneHasModelTab(lane));
}

eq("tab label names the song's model", tabLabel(job(), settings(), "vocals", t), "Model: Kim Mel-RoFormer");
eq("tab label of a model the user added reads (yours)", tabLabel(job({ stem_models: { guitar: USER } }), settings(), "guitar", t), "Model: My Guitar (yours)");
eq("tab label before the settings answer is the id", tabLabel(job(), null, "drums", t), "Model: htdemucs_6s");

// ─── The song's model and the Settings default ───

eq("the song's own model", songStemModel(job(), "vocals"), "kim_mel_roformer");
eq("a song from before per-stem models: the base model", songStemModel({ stem_models: null }, "vocals"), BASE_MODEL);
eq("a stem the record does not name: the base model", songStemModel({ stem_models: { vocals: "kim_mel_roformer" } }, "bass"), BASE_MODEL);
eq("Settings default on auto is what auto resolves to", settingsStemModel(settings(), "vocals"), "kim_mel_roformer");
eq("Settings default saved explicitly", settingsStemModel(settings({ saved: { drums: "htdemucs_ft" } }), "drums"), "htdemucs_ft");
eq("no settings answer: no default", settingsStemModel(null, "vocals"), "");

check("no dot when the song matches Settings", !differsFromSettings(job(), settings(), "vocals"));
check("a dot when Settings now uses another model", differsFromSettings(job(), settings({ saved: { drums: "htdemucs_ft" } }), "drums"));
check("a dot when the song used another model than Settings now", differsFromSettings(job({ stem_models: { vocals: "htdemucs_6s" } }), settings(), "vocals"));
check("no dot while the settings answer is not there", !differsFromSettings(job(), null, "drums"));

// ─── The select ───

{
  const opts = laneOptions(settings(), job(), "vocals", t);
  eq("vocals lists the Settings choices, Recommended tagged", opts.map((o) => o.label), ["HTDemucs 6s", "Kim Mel-RoFormer (Recommended)"]);
  check("no Add a model option in a lane", opts.every((o) => !o.add));
  eq("it starts at the song's own model", initialChoice(opts, job(), "vocals"), "kim_mel_roformer");
}
{
  const opts = laneOptions(settings(), job(), "guitar", t);
  eq("a model the user added reads (yours), never Recommended", opts.map((o) => o.label), ["HTDemucs 6s (Recommended)", "My Guitar (yours)"]);
}
{
  const opts = laneOptions(settings(), job(), "piano", t);
  eq("a stem with one model lists it", opts.map((o) => o.value), ["htdemucs_6s"]);
}
{
  // A song made with a model since removed: the select still says it, unusable.
  const gone = "user_ffffffffffff";
  const j = job({ stem_models: { guitar: gone } });
  const opts = laneOptions(settings(), j, "guitar", t);
  eq("a removed model is listed first, disabled", [opts[0].value, opts[0].disabled], [gone, true]);
  eq("it starts at the song's model even then", initialChoice(opts, j, "guitar"), gone);
}
{
  const s = settings({ models: { ...MODELS, kim_mel_roformer: { name: "Kim Mel-RoFormer", available: false } } });
  const opts = laneOptions(s, job({ stem_models: { vocals: "htdemucs_6s" } }), "vocals", t);
  check("a model that cannot run here is disabled", opts.find((o) => o.value === "kim_mel_roformer")?.disabled === true);
}
eq("no settings answer: no options", laneOptions(null, job(), "vocals", t), []);
eq("a lane with no options starts at nothing", initialChoice([], job(), "vocals"), "");

// ─── Re-extract on or off ───

{
  const opts = laneOptions(settings(), job(), "vocals", t);
  eq("on with a usable choice", reextractBlock({ job: job(), options: opts, choice: "htdemucs_6s" }), "");
  eq("on for the model the stem already has", reextractBlock({ job: job(), options: opts, choice: "kim_mel_roformer" }), "");
  eq("off while a run of the song goes", reextractBlock({ job: job({ stem_reextract: { stem: "drums", state: "running" } }), options: opts, choice: "htdemucs_6s" }), BLOCK_RUNNING);
  eq("off with a reason the page found", reextractBlock({ job: job(), options: opts, choice: "htdemucs_6s", blockedKey: "laneModel.reason.no_source" }), "laneModel.reason.no_source");
  eq("off with no list", reextractBlock({ job: job(), options: [], choice: "" }), "laneModel.noChoices");
  const gone = laneOptions(settings(), job({ stem_models: { guitar: "user_ffffffffffff" } }), "guitar", t);
  eq("off on an unusable choice", reextractBlock({ job: job(), options: gone, choice: "user_ffffffffffff" }), "laneModel.reason.model_unavailable");
}

// ─── Refusals ───

for (const code of REASON_CODES) {
  check(`reason ${code} has its sentence`, typeof en[reasonKey(code)] === "string" && en[reasonKey(code)].length > 0);
}
eq("an unknown code gets the general sentence", reasonKey("something_new"), "laneModel.reason.unknown");
check("the general sentence exists", typeof en["laneModel.reason.unknown"] === "string");
for (const code of ["not_ready", "stem_not_selectable", "stems_missing", "model_not_allowed", "model_removed", "model_unavailable", "no_source", "busy", "no_undo", "failed", "source_changed", "interrupted"]) {
  check(`the server's code ${code} is known`, REASON_CODES.includes(code));
}
eq("the body's code wins", refusalCode(409, { detail: "x", code: "busy" }), "busy");
eq("a 404 without a code is a song that is gone", refusalCode(404, { detail: "job not found" }), "not_found");
eq("a 422 list without a code is unknown", refusalCode(422, { detail: [{ msg: "x" }] }), "unknown");
check("no source is about the song", stickyRefusal("no_source"));
check("busy is about this one try", !stickyRefusal("busy"));

// ─── Undo copies ───

{
  const j = job({ stem_undo: { guitar: { model: "htdemucs_6s", saved_at: 1764000000 } } });
  check("an undo copy shows Undo", hasUndo(j, "guitar"));
  check("no copy, no Undo", !hasUndo(j, "bass"));
  check("no stem_undo at all, no Undo", !hasUndo({ stem_undo: null }, "guitar"));
  eq("the copy's model", undoModel(j, "guitar"), "htdemucs_6s");
  eq("no copy, no model", undoModel(j, "bass"), "");
}

// ─── A run's line ───

const name = "Guitar";
const stemName = (s) => ({ vocals: "Vocals", drums: "Drums", guitar: "Guitar" })[s] || s;
const run = (over) => job({ stem_reextract: { stem: "guitar", model: USER, action: "reextract", state: "running", phase: "separating", progress: 0.42, code: null, used_model: null, fallback: null, gain_db: null, ...over } });

eq("no run, no line", runLine(job(), settings(), "guitar", name, t), null);
check("running is running", runRunning(run({})));
eq("progress as a whole percent", runPct({ progress: 0.426 }), 43);
eq("progress clamped", [runPct({ progress: 1.7 }), runPct({ progress: -1 }), runPct({})], [100, 0, 0]);
eq("separating names the stem and the model", runLine(run({}), settings(), "guitar", name, t), { kind: "running", text: "Separating Guitar again with My Guitar (yours)..." });
eq("waiting", runLine(run({ phase: "waiting" }), settings(), "guitar", name, t).text, "Waiting for the separation to be free...");
eq("the fetch phase reads as processing the stem", runLine(run({ phase: "downloading", model: "kim_mel_roformer", stem: "guitar" }), settings(), "guitar", name, t).text, "Processing Guitar...");
eq("finishing", runLine(run({ phase: "finishing" }), settings(), "guitar", name, t).text, "Writing the new Guitar stem...");
eq("an unknown phase reads as separating", runLine(run({ phase: null }), settings(), "guitar", name, t).kind, "running");
eq("an undo running", runLine(run({ action: "undo" }), settings(), "guitar", name, t).text, "Putting back the previous Guitar...");
eq("another stem running", runLine(run({ stem: "drums" }), settings(), "guitar", name, t, stemName), { kind: "other", text: "Drums of this song is being separated again. Wait until it ends." });
eq("another stem's ended run says nothing here", runLine(run({ stem: "drums", state: "done" }), settings(), "guitar", name, t), null);
eq("cancelled", runLine(run({ state: "cancelled" }), settings(), "guitar", name, t), { kind: "cancelled", text: "Stopped. Guitar is as it was." });
eq("an error says its reason", runLine(run({ state: "error", code: "source_changed" }), settings(), "guitar", name, t).text, en["laneModel.reason.source_changed"]);
eq("online sources off says where to turn them on", runLine(run({ state: "error", code: "online_sources_off" }), settings(), "guitar", name, t).text, en["onlineSources.off"]);
eq("an unknown error code", runLine(run({ state: "error", code: "zzz" }), settings(), "guitar", name, t).text, en["laneModel.reason.unknown"]);
{
  const done = run({ state: "done", used_model: USER });
  done.stem_models.guitar = USER;
  eq("done names the model that made it", runLine(done, settings(), "guitar", name, t), { kind: "done", text: "Guitar is now made with My Guitar (yours)." });
}
{
  const done = run({ state: "done", used_model: "htdemucs_6s", fallback: { model: USER, name: "My Guitar", code: "load_failed" } });
  eq("a fallback says what stood in and why", runLine(done, settings(), "guitar", name, t).text, `My Guitar could not run, so HTDemucs 6s made Guitar. ${en["settings.myModels.reason.load_failed"]}`);
}
{
  const done = run({ state: "done", used_model: USER, gain_db: -1.54 });
  eq("a gain change is said as a positive number", runLine(done, settings(), "guitar", name, t).text, "Guitar is now made with My Guitar (yours). Every stem was turned down by 1.5 dB so the song does not clip.");
  const louder = run({ state: "done", used_model: USER, gain_db: 0 });
  check("no gain line for 0 dB", !runLine(louder, settings(), "guitar", name, t).text.includes("dB"));
}
{
  const undone = run({ state: "done", action: "undo" });
  undone.stem_models.guitar = "htdemucs_6s";
  eq("an undo done names the model it is back to", runLine(undone, settings(), "guitar", name, t), { kind: "undone", text: "Guitar is back as it was, made with HTDemucs 6s." });
}

// ─── Every code the server can give has its sentence (contract v1.1) ───

// Refusals of POST .../reextract, .../undo and .../keep, and the codes a
// failed run leaves in stem_reextract.code (app/api/jobs.py
// _REEXTRACT_REFUSALS, app/pipeline/stem_reextract.py).
const SERVER_CODES = [
  "not_ready", "stem_not_selectable", "stems_missing", "model_not_allowed", "model_removed",
  "model_unavailable", "no_source", "busy", "no_undo", "failed", "source_changed", "interrupted",
];
for (const code of SERVER_CODES) {
  for (const action of ["reextract", "undo", "keep"]) {
    const key = reasonKey(code, action);
    check(`${action} ${code} has its own sentence`, key !== "laneModel.reason.unknown" && typeof en[key] === "string" && en[key].length > 0, key);
  }
}
eq("an undo says missing stems in its own words", reasonKey("stems_missing", "undo"), "laneModel.reason.undo.stems_missing");
eq("a re-extract says missing stems as before", reasonKey("stems_missing", "reextract"), "laneModel.reason.stems_missing");
eq("an undo's other codes are the shared sentences", reasonKey("busy", "undo"), "laneModel.reason.busy");
for (const code of UNDO_REASON_CODES) check(`undo ${code} sentence exists`, typeof en[`laneModel.reason.undo.${code}`] === "string");
eq("a failed undo run with missing stems says the undo sentence",
  runLine(job({ stem_reextract: { stem: "guitar", action: "undo", state: "error", code: "stems_missing" } }), settings(), "guitar", "Guitar", t).text,
  en["laneModel.reason.undo.stems_missing"]);
eq("online sources off at run time says where to turn them on",
  runLine(job({ stem_reextract: { stem: "guitar", action: "reextract", state: "error", code: "online_sources_off" } }), settings(), "guitar", "Guitar", t).text,
  en["onlineSources.off"]);

// ─── A Vocals re-extract sets the Lead + Backing pair aside ───

{
  const done = { stem: "vocals", action: "reextract", state: "done" };
  check("pair before, none after: reset", splitWasReset(done, "done", { vocal_split: "none" }));
  check("no pair before: nothing to say", !splitWasReset(done, "none", { vocal_split: "none" }));
  check("another stem: no reset", !splitWasReset({ ...done, stem: "drums" }, "done", { vocal_split: "none" }));
  check("an undo: no reset", !splitWasReset({ ...done, action: "undo" }, "done", { vocal_split: "done" }));
  check("a failed run: no reset", !splitWasReset({ ...done, state: "error" }, "done", { vocal_split: "done" }));
  check("pair still there: no reset", !splitWasReset(done, "done", { vocal_split: "done" }));
}

// ─── Every key the row uses exists in English ───

const KEYS = [
  "laneModel.tab.title", "laneModel.panel.title", "laneModel.panel.regionAria", "laneModel.select.aria",
  "laneModel.reextract", "laneModel.reextractTitle", "laneModel.undo", "laneModel.undoTitle",
  "laneModel.progressAria", "laneModel.cancel", "laneModel.cancelTitle", "laneModel.madeWith",
  "laneModel.differs", "laneModel.lost", "laneModel.noChoices",
  "laneModel.keep", "laneModel.keepTitle", "laneModel.keepAgain", "laneModel.kept", "laneModel.splitReset",
  "help.modelTab", "help.modelSelect", "help.modelReextract", "help.modelUndo", "help.modelKeep", "help.modelCancel",
];
for (const key of KEYS) check(`${key} is in the English table`, typeof en[key] === "string");
check("no em dash in the row's English", Object.entries(en).filter(([k]) => k.startsWith("laneModel.") || k.startsWith("help.model")).every(([, v]) => !v.includes("\u2014")));

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
