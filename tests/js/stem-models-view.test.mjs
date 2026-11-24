// Settings > Separation, "Model for each stem": the words and numbers the rows
// show and what a choice sends (static/js/stemModelsView.js). Pure, so it runs
// without a browser; tests/e2e/separation-settings.spec.mjs checks the drawing.
//
// Run:  node tests/js/stem-models-view.test.mjs

import { TRANSLATIONS } from "../../static/js/i18n.js";
import { ADD_MODEL_VALUE } from "../../static/js/myModelsView.js";
import {
  BEST_DEMUCS_FACTOR,
  CHOICE_STEMS,
  estimateLine,
  fixedLine,
  hasStemModels,
  hintLine,
  isUserModel,
  noteLine,
  passSeconds,
  planMinutes,
  postValue,
  resetPatch,
  selectedValue,
  stemOptions,
  stemRows,
  warningFor,
  whyLine,
} from "../../static/js/stemModelsView.js";

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
const near = (name, actual, expected, tol = 0.05) =>
  check(name, typeof actual === "number" && Math.abs(actual - expected) <= tol, `got ${actual}, want ${expected} +/- ${tol}`);

// The English table, with {placeholders} filled, as i18n.t does.
const en = TRANSLATIONS.en;
const t = (key, vars = {}) => String(en[key] ?? key).replace(/\{(\w+)\}/g, (_, k) => (k in vars ? String(vars[k]) : `{${k}}`));

const MODELS = {
  htdemucs_6s: { name: "HTDemucs 6s", license: "MIT", source_url: "https://github.com/facebookresearch/demucs", download_bytes: 54_996_327, cpu_rtf: 0.5, gpu_rtf: 0.068, available: true, downloaded: true, note_key: "settings.stemModels.note.htdemucs_6s" },
  htdemucs_ft: { name: "HTDemucs FT", license: "MIT", source_url: "https://github.com/facebookresearch/demucs", download_bytes: 336_565_084, cpu_rtf: 1.84, gpu_rtf: 0.145, available: true, downloaded: false, note_key: "settings.stemModels.note.htdemucs_ft" },
  kim_mel_roformer: { name: "Kim Mel-RoFormer", license: "MIT", source_url: "https://huggingface.co/KimberleyJSN/melbandroformer", download_bytes: 913_106_900, cpu_rtf: 3.4, gpu_rtf: 0.105, available: true, downloaded: false, note_key: "settings.stemModels.note.kim_mel_roformer" },
};
const STEMS = [
  { stem: "vocals", choices: ["htdemucs_6s", "kim_mel_roformer"] },
  { stem: "lead_backing", fixed: "uvr_karaoke_2", available: true },
  { stem: "drums", choices: ["htdemucs_6s", "htdemucs_ft"] },
  { stem: "bass", choices: ["htdemucs_6s", "htdemucs_ft"] },
  { stem: "guitar", choices: ["htdemucs_6s"] },
  { stem: "piano", choices: ["htdemucs_6s"] },
  { stem: "other", fixed: "residual" },
];
const CATALOG_PINNED = [
  { stem: "vocals", fixed: "mdx_extra" },
  { stem: "lead_backing", fixed: "uvr_karaoke_2", available: true },
  { stem: "drums", fixed: "mdx_extra" },
  { stem: "bass", fixed: "mdx_extra" },
  { stem: "guitar", fixed: "mdx_extra" },
  { stem: "piano", fixed: "mdx_extra" },
  { stem: "other", fixed: "residual" },
];
const auto5 = () => Object.fromEntries(CHOICE_STEMS.map((s) => [s, "auto"]));
const all6s = () => Object.fromEntries(CHOICE_STEMS.map((s) => [s, "htdemucs_6s"]));

function settings({ device = "cuda", reason = "gpu", quality = "standard", saved = auto5(), models = MODELS } = {}) {
  const recommended = { ...all6s(), ...(reason === "gpu" ? { vocals: "kim_mel_roformer" } : {}) };
  const resolved = Object.fromEntries(CHOICE_STEMS.map((s) => {
    const v = saved[s];
    return [s, v && v !== "auto" && models[v]?.available !== false ? v : recommended[s]];
  }));
  return {
    demucs_device_resolved: device,
    separation_quality: quality,
    stem_models: saved,
    stem_models_resolved: resolved,
    stem_models_recommended: recommended,
    stem_models_reason: reason,
    stem_model_catalog: { stems: STEMS, models, min_vram_mb: 4096 },
  };
}
const gpu = settings();
const cpu = settings({ device: "cpu", reason: "cpu" });

// An older server: no group at all.
check("an answer with the fields has the group", hasStemModels(gpu));
check("an older server's answer has no group", !hasStemModels({ demucs_device: "auto", separation_quality: "standard" }));
check("no answer has no group", !hasStemModels(null));
check("a catalog without models has no group", !hasStemModels({ ...gpu, stem_model_catalog: { stems: STEMS } }));
eq("an older server draws no rows", stemRows({ demucs_device: "auto" }), []);

// Rows: the order, and select or line.
eq("rows in the pane's order", stemRows(gpu).map((r) => r.stem), ["vocals", "lead_backing", "drums", "bass", "guitar", "piano", "other"]);
// Guitar and Piano are selects too, though they have one built-in model: a
// model the user adds, and "Add a model...", need somewhere to show.
eq("all five stems with a model are selects", stemRows(gpu).filter((r) => r.kind === "select").map((r) => r.stem), ["vocals", "drums", "bass", "guitar", "piano"]);
eq("guitar is a select of its one model", stemRows(gpu).find((r) => r.stem === "guitar"), { stem: "guitar", kind: "select", choices: ["htdemucs_6s"] });
eq("lead and backing is the karaoke line", stemRows(gpu).find((r) => r.stem === "lead_backing"), { stem: "lead_backing", kind: "fixed", fixed: "karaoke", available: true });
eq("other is the residual line", stemRows(gpu).find((r) => r.stem === "other"), { stem: "other", kind: "fixed", fixed: "residual" });
eq("the fixed lines read as written", stemRows(gpu).filter((r) => r.kind === "fixed").map((r) => fixedLine(gpu, r, t)), [
  en["settings.stemModels.fixed.karaoke"],
  en["settings.stemModels.fixed.residual"],
]);
eq("guitar's one option is its model, recommended", stemOptions(gpu, "guitar", ["htdemucs_6s"], t).map((o) => o.label), ["HTDemucs 6s (Recommended)"]);

// Option labels and the Recommended tag.
const vocalsGpu = stemOptions(gpu, "vocals", ["htdemucs_6s", "kim_mel_roformer"], t);
eq("on a GPU, Kim carries the Recommended tag", vocalsGpu.map((o) => o.label), ["HTDemucs 6s", "Kim Mel-RoFormer (Recommended)"]);
eq("only the recommended option is marked", vocalsGpu.map((o) => o.recommended), [false, true]);
const vocalsCpu = stemOptions(cpu, "vocals", ["htdemucs_6s", "kim_mel_roformer"], t);
eq("on the processor, 6s carries the tag", vocalsCpu.map((o) => o.label), ["HTDemucs 6s (Recommended)", "Kim Mel-RoFormer"]);
eq("drums on a GPU: 6s recommended", stemOptions(gpu, "drums", ["htdemucs_6s", "htdemucs_ft"], t).map((o) => o.label), ["HTDemucs 6s (Recommended)", "HTDemucs FT"]);

// Unavailable: disabled, says so, and the warning says so.
const noKim = { ...MODELS, kim_mel_roformer: { ...MODELS.kim_mel_roformer, available: false } };
const unavail = settings({ reason: "unavailable", models: noKim });
const vocalsUnavail = stemOptions(unavail, "vocals", ["htdemucs_6s", "kim_mel_roformer"], t);
eq("an unavailable model is disabled", vocalsUnavail.map((o) => o.disabled), [false, true]);
eq("its label says why", vocalsUnavail[1].label, "Kim Mel-RoFormer (Not available on this install)");
eq("an available model is never disabled", vocalsGpu.map((o) => o.disabled), [false, false]);
eq("a saved unavailable choice warns as unavailable", warningFor(settings({ reason: "unavailable", models: noKim, saved: { ...auto5(), vocals: "kim_mel_roformer" } }), "kim_mel_roformer", t)?.kind, "unavailable");
eq("a model with no available field counts as available", stemOptions(gpu, "vocals", ["htdemucs_6s"], t)[0].disabled, false);

// What the select shows.
eq("auto shows what it resolves to", selectedValue(gpu, "vocals"), "kim_mel_roformer");
eq("auto on the processor shows 6s", selectedValue(cpu, "vocals"), "htdemucs_6s");
eq("a saved model shows itself", selectedValue(settings({ saved: { ...auto5(), bass: "htdemucs_ft" } }), "bass"), "htdemucs_ft");

// What a choice sends.
eq("picking the recommended model sends auto", postValue(gpu, "vocals", "kim_mel_roformer"), "auto");
eq("picking another model sends its id", postValue(gpu, "vocals", "htdemucs_6s"), "htdemucs_6s");
eq("on the processor, 6s for vocals sends auto", postValue(cpu, "vocals", "htdemucs_6s"), "auto");
eq("on the processor, Kim sends its id", postValue(cpu, "vocals", "kim_mel_roformer"), "kim_mel_roformer");
eq("ft for drums sends its id", postValue(gpu, "drums", "htdemucs_ft"), "htdemucs_ft");
eq("Use recommended sends all five as auto", resetPatch(), { stem_models: { vocals: "auto", drums: "auto", bass: "auto", guitar: "auto", piano: "auto" } });

// The estimate: one pass per distinct model, for a 4 minute song.
near("CPU, Kim plus 6s: about 15.6 minutes", planMinutes(cpu, { ...all6s(), vocals: "kim_mel_roformer" }), 15.6);
near("CPU, all 6s: 2 minutes", planMinutes(cpu, all6s()), 2.0);
near("CPU, Kim plus ft plus 6s: three passes", planMinutes(cpu, { ...all6s(), vocals: "kim_mel_roformer", drums: "htdemucs_ft", bass: "htdemucs_ft" }), (3.4 + 1.84 + 0.5) * 4);
near("GPU, Kim plus 6s: under a minute", planMinutes(gpu, gpu.stem_models_resolved), (0.105 + 0.068) * 4, 0.01);
near("Best slows Demucs only", planMinutes(settings({ device: "cpu", reason: "cpu", quality: "best" }), { ...all6s(), vocals: "kim_mel_roformer" }), (3.4 + 0.5 * BEST_DEMUCS_FACTOR) * 4);
check("an unknown model has no estimate", planMinutes(gpu, { ...all6s(), vocals: "nope" }) === null);
eq("the estimate line, CPU Kim plus 6s", estimateLine({ ...cpu, stem_models_resolved: { ...all6s(), vocals: "kim_mel_roformer" } }, t, "en"),
  "With these models, a 4-minute song takes about 15.6 min to separate on this computer.");
eq("the estimate uses the reader's decimal comma", estimateLine({ ...cpu, stem_models_resolved: { ...all6s(), vocals: "kim_mel_roformer" } }, t, "de"),
  "With these models, a 4-minute song takes about 15,6 min to separate on this computer.");
near("a pass on CPU, Kim: 13.6 minutes", passSeconds(cpu, "kim_mel_roformer", "cpu", "standard") / 60, 13.6);

// The slow warning: slower than the song itself.
eq("Kim on the processor is slow", warningFor(cpu, "kim_mel_roformer", t, "en"), { kind: "slow", text: "Slow on this computer: about 13.6 min for a 4-minute song." });
eq("ft on the processor is slow", warningFor(cpu, "htdemucs_ft", t, "en")?.kind, "slow");
eq("6s on the processor is not slow", warningFor(cpu, "htdemucs_6s", t, "en"), null);
eq("6s with Best on the processor is not slow (0.81 of the song)", warningFor(settings({ device: "cpu", reason: "cpu", quality: "best" }), "htdemucs_6s", t, "en"), null);
eq("Kim on a GPU is not slow", warningFor(gpu, "kim_mel_roformer", t, "en"), null);
const mps = settings({ device: "mps", reason: "mps" });
eq("Kim on Apple Silicon is untested", warningFor(mps, "kim_mel_roformer", t, "en")?.kind, "untestedMps");
eq("6s on Apple Silicon has no warning", warningFor(mps, "htdemucs_6s", t, "en"), null);

// The reason line.
// The GPU reason gives Kim's own pass time here and, until it is downloaded, its size.
eq("gpu reason, with Kim's pass time and its download", whyLine(gpu, t, "en"),
  "Separation runs on a graphics card, so the cleaner vocal model is recommended for Vocals. Its pass adds about 25 s to a 4-minute song here. The first time, it downloads 913 MB.");
const kimHere = { ...MODELS, kim_mel_roformer: { ...MODELS.kim_mel_roformer, downloaded: true } };
eq("gpu reason once Kim is downloaded: no download sentence", whyLine(settings({ models: kimHere }), t, "en"),
  "Separation runs on a graphics card, so the cleaner vocal model is recommended for Vocals. Its pass adds about 25 s to a 4-minute song here.");
eq("a gpu reason the Recommended tag contradicts says nothing",
  whyLine({ ...gpu, stem_models_recommended: all6s() }, t, "en"), null);
// over-budget: a speed test on this cuda machine made 6s the vocals recommendation.
const overBudget = {
  ...settings({ reason: "over-budget" }),
  speed_test: { stale: false, devices: { cuda: { htdemucs_6s: { rtf: 0.2, measured: true }, kim_mel_roformer: { rtf: 0.9, measured: false, estimated_from: "htdemucs_6s" } } } },
};
eq("over-budget: 6s is the vocals recommendation", stemOptions(overBudget, "vocals", ["htdemucs_6s", "kim_mel_roformer"], t)[0].recommended, true);
eq("over-budget reason gives Kim's measured plan time", whyLine(overBudget, t, "en"),
  "The benchmark found the cleaner vocal model too slow for the goal on this computer, so the fast model is recommended for Vocals. The cleaner model would take about 4.4 min for a 4-minute song.");

// Measured speed beats the registry, a stale result does not count, and on
// Apple Silicon there is no number until the speed test has run there.
const measuredCpu = { ...cpu, speed_test: { stale: false, devices: { cpu: { htdemucs_6s: { rtf: 0.25, measured: true }, kim_mel_roformer: { rtf: 1.7, measured: false } } } } };
near("measured CPU speed is used: Kim plus 6s", planMinutes(measuredCpu, { ...all6s(), vocals: "kim_mel_roformer" }), (1.7 + 0.25) * 4);
eq("measured speed changes the slow warning", warningFor(measuredCpu, "kim_mel_roformer", t, "en")?.text, "Slow on this computer: about 6.8 min for a 4-minute song.");
eq("a measured 6s under the song's length is not slow", warningFor(measuredCpu, "htdemucs_6s", t, "en"), null);
near("a stale speed test is ignored", planMinutes({ ...measuredCpu, speed_test: { ...measuredCpu.speed_test, stale: true } }, all6s()), 2.0);
near("another device's speed is not used", planMinutes({ ...gpu, speed_test: measuredCpu.speed_test }, gpu.stem_models_resolved), (0.105 + 0.068) * 4, 0.01);
const mpsNoTest = settings({ device: "mps", reason: "mps" });
eq("on Apple Silicon without a speed test: no estimate", estimateLine(mpsNoTest, t, "en"), null);
eq("nor a slow warning", warningFor(mpsNoTest, "htdemucs_6s", t, "en"), null);
const mpsTested = { ...mpsNoTest, speed_test: { stale: false, devices: { mps: { htdemucs_6s: { rtf: 0.15, measured: true } } } } };
eq("on Apple Silicon with a speed test: the measured estimate", estimateLine(mpsTested, t, "en"),
  "With these models, a 4-minute song takes about 0.6 min to separate on this computer.");
eq("cpu reason names Kim's time here", whyLine(cpu, t, "en"),
  "Separation runs on the processor, so the fast model is recommended for every stem. The cleaner vocal model would take about 15.6 min for a 4-minute song here.");
eq("low VRAM reason names the floor", whyLine(settings({ reason: "low-vram" }), t, "en"),
  "This graphics card has less than 4 GB of memory, so the fast model is recommended for Vocals.");
eq("on the processor without Kim, the reason says it is not available, not a time",
  whyLine(settings({ device: "cpu", reason: "cpu", models: noKim }), t, "en"), en["settings.stemModels.why.unavailable"]);
eq("mps reason", whyLine(mps, t, "en"), en["settings.stemModels.why.mps"]);
eq("unavailable reason", whyLine(unavail, t, "en"), en["settings.stemModels.why.unavailable"]);
eq("an unknown reason shows nothing", whyLine({ ...gpu, stem_models_reason: "deployment" }, t, "en"), null);

// A deployment that pins its own model: every stem is a line naming it, no reason.
const pinned = {
  ...gpu,
  stem_models_resolved: Object.fromEntries(CHOICE_STEMS.map((s) => [s, "mdx_extra"])),
  stem_model_catalog: {
    ...gpu.stem_model_catalog,
    deployment: "mdx_extra",
    stems: CATALOG_PINNED,
  },
};
eq("a pinned deployment has no selects", stemRows(pinned).filter((r) => r.kind === "select").length, 0);
eq("its vocals line names the pinned model", fixedLine(pinned, stemRows(pinned)[0], t), "mdx_extra, the only model for this stem.");
eq("a pinned deployment has no reason line", whyLine(pinned, t, "en"), null);
eq("nor an estimate, its speed being unknown", estimateLine(pinned, t, "en"), null);

// Hint and note.
eq("not downloaded: size and licence", hintLine(gpu, "kim_mel_roformer", t, "en"), "Downloads 913 MB the first time it is used. License: MIT.");
eq("downloaded: the licence only", hintLine(gpu, "htdemucs_6s", t, "en"), "Downloaded. License: MIT.");
eq("an unknown model has no hint", hintLine(gpu, "nope", t, "en"), "");
eq("the note is the model's own", noteLine(gpu, "htdemucs_ft", t), en["settings.stemModels.note.htdemucs_ft"]);

// A model the user added (Settings, My models): "(yours)", never
// recommended, posted as its id, and "Add a model..." last and never posted.
const USER_ID = "user_0123456789ab";
const USER_MODEL = { name: "Guitar RoFormer", user: true, engine: "user_roformer", stems: ["guitar"], license: "CC BY 4.0", source_url: null, download_bytes: 0, cpu_rtf: 2.5, gpu_rtf: 0.105, estimated: true, available: true, downloaded: true, note_key: null };
const withUser = (over = {}) => {
  const base = settings({ models: { ...MODELS, [USER_ID]: { ...USER_MODEL, ...over } } });
  return {
    ...base,
    stem_model_catalog: {
      ...base.stem_model_catalog,
      stems: base.stem_model_catalog.stems.map((e) => (e.stem === "guitar" ? { ...e, choices: [...e.choices, USER_ID] } : e)),
    },
  };
};
const user = withUser();
check("a user id is a user model", isUserModel(user, USER_ID) && !isUserModel(user, "htdemucs_6s"));
check("an id of that shape counts even before the catalog lists it", isUserModel(gpu, "user_aaaaaaaaaaaa"));
eq("guitar lists the user's model after the built-in one", stemRows(user).find((r) => r.stem === "guitar").choices, ["htdemucs_6s", USER_ID]);
const guitarOpts = stemOptions(user, "guitar", ["htdemucs_6s", USER_ID], t, { addOption: true });
eq("the user's model reads (yours), then Add a model...", guitarOpts.map((o) => o.label), ["HTDemucs 6s (Recommended)", "Guitar RoFormer (yours)", "Add a model..."]);
eq("only the last option is the Add option", guitarOpts.map((o) => o.add), [false, false, true]);
eq("the Add option's value", guitarOpts.at(-1).value, ADD_MODEL_VALUE);
eq("without the server's routes there is no Add option", stemOptions(user, "guitar", ["htdemucs_6s", USER_ID], t).map((o) => o.value), ["htdemucs_6s", USER_ID]);
const recUser = { ...user, stem_models_recommended: { ...user.stem_models_recommended, guitar: USER_ID } };
eq("a user model is never tagged Recommended, even if the answer names it", stemOptions(recUser, "guitar", [USER_ID], t).map((o) => [o.label, o.recommended]), [["Guitar RoFormer (yours)", false]]);
eq("picking the user's model posts its id", postValue(user, "guitar", USER_ID), USER_ID);
eq("Add a model... is never posted", postValue(user, "guitar", ADD_MODEL_VALUE), null);
const userOff = withUser({ available: false });
eq("a user model this install cannot run is disabled and says so", stemOptions(userOff, "guitar", ["htdemucs_6s", USER_ID], t)[1], { value: USER_ID, label: "Guitar RoFormer (yours) (Not available on this install)", disabled: true, recommended: false, add: false });
eq("the user model's hint names the declared licence", hintLine(user, USER_ID, t, "en"), "Added by you. Licence declared by its source: CC BY 4.0.");
eq("no declared licence says so", hintLine(withUser({ license: null }), USER_ID, t, "en"), "Added by you. Its source declared no licence.");
const userCpu = { ...user, demucs_device_resolved: "cpu" };
eq("a user model slower than the song warns with its measured speed", warningFor(userCpu, USER_ID, t, "en"), { kind: "slow", text: "Slow on this computer: about 10 min for a 4-minute song." });
eq("on a GPU its estimated speed is fast", warningFor(user, USER_ID, t, "en"), null);
eq("a user model is not Demucs: Best quality does not slow it", passSeconds({ ...userCpu, separation_quality: "best" }, USER_ID, "cpu", "best"), 2.5 * 240);
// The processor reason talks about the cleaner built-in vocal model, never one the user added.
const userVocalsCpu = (() => {
  const base = settings({ device: "cpu", reason: "cpu", models: { ...noKim, [USER_ID]: { ...USER_MODEL, stems: ["vocals"] } } });
  return { ...base, stem_model_catalog: { ...base.stem_model_catalog, stems: base.stem_model_catalog.stems.map((e) => (e.stem === "vocals" ? { ...e, choices: [...e.choices, USER_ID] } : e)) } };
})();
eq("the cpu reason ignores a vocal model the user added", whyLine(userVocalsCpu, t, "en"), en["settings.stemModels.why.unavailable"]);

// Every key this module reads is in every table.
const KEYS = Object.keys(en).filter((k) => k.startsWith("settings.stemModels.") || k.startsWith("help.set.stem"));
for (const [lang, table] of Object.entries(TRANSLATIONS)) {
  if (lang === "pt-PT") continue;
  const missing = KEYS.filter((k) => !(k in table));
  check(`${lang} has every stem model key`, missing.length === 0, missing.join(", "));
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
