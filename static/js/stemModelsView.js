// Settings > Separation, "Model for each stem": what the rows say and what a
// choice sends. Pure functions over the GET /api/settings answer, with `t`
// (i18n.t) passed in, so tests/js/stem-models-view.test.mjs runs them without
// a browser. separationSettings.js draws the rows from what these return.
//
// The answer carries five fields (the server side is app/core/stem_models.py):
//   stem_models              the saved choice per stem, "auto" or a model id
//   stem_models_resolved     what the next job uses on this machine
//   stem_models_recommended  the recommendation per stem on this machine
//   stem_models_reason       why: "gpu" | "cpu" | "mps" | "low-vram" | "unavailable"
//   stem_model_catalog       { stems: [...], models: { id: {...} }, min_vram_mb }
// An older server sends none of them, and the group is then left out.
//
// A model the user added (Settings, My models) is a catalog entry with
// `user: true` and an id "user_<12 hex>". It is listed in the choices of the
// stems it declares, labelled "{name} (yours)", and never recommended.
// Every stem with a choice list is a select, Guitar and Piano too, so the last
// option "Add a model..." (ADD_MODEL_VALUE, never posted) has somewhere to be.

import { ADD_MODEL_VALUE, USER_MODEL_ID_RE } from "./myModelsView.js";

/** The rows, in the order the pane shows them. */
export const ROW_ORDER = ["vocals", "lead_backing", "drums", "bass", "guitar", "piano", "other"];
/** The stems a choice is saved for; "Use recommended" sends all of them. */
export const CHOICE_STEMS = ["vocals", "drums", "bass", "guitar", "piano"];
/** The stem key each row's name comes from (Lead + Backing is a vocal mode). */
export const STEM_LABEL = {
  vocals: "stem.vocals",
  lead_backing: "vocalMode.split",
  drums: "stem.drums",
  bass: "stem.bass",
  guitar: "stem.guitar",
  piano: "stem.piano",
  other: "stem.other",
};

/** The song length every estimate is for: 4 minutes. */
export const SONG_SECONDS = 240;
/** A pass slower than the song itself is slow here. */
export const SLOW_RTF = 1;
/** Best quality runs Demucs with shifts: 1.62 times the Standard time
 *  (measured; the engine weights its progress by the same number). It does
 *  not change a model that is not Demucs. */
export const BEST_DEMUCS_FACTOR = 1.62;

const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);

/** Whether the answer has the per-stem model fields (an older server has none). */
export function hasStemModels(d) {
  return isObj(d)
    && isObj(d.stem_models)
    && isObj(d.stem_models_resolved)
    && isObj(d.stem_models_recommended)
    && isObj(d.stem_model_catalog)
    && Array.isArray(d.stem_model_catalog.stems)
    && isObj(d.stem_model_catalog.models);
}

const models = (d) => d?.stem_model_catalog?.models || {};

/** A model's display name, or its id when the catalog does not list it. */
export function modelName(d, id) {
  return models(d)[id]?.name || String(id ?? "");
}

/** Whether a model is one the user added. */
export function isUserModel(d, id) {
  return models(d)[id]?.user === true || USER_MODEL_ID_RE.test(String(id ?? ""));
}

/** Whether a model runs through Demucs, which Best quality slows down. The
 *  catalog's `engine` when it sends one, otherwise read from the id. */
export function isDemucs(d, id) {
  const m = models(d)[id];
  if (typeof m?.engine === "string") return m.engine === "demucs";
  return /demucs/i.test(String(id ?? ""));
}

/**
 * The rows to draw, in ROW_ORDER, only for stems the catalog lists:
 *   { stem, kind: "select", choices }            a list of models, even of one
 *   { stem, kind: "fixed", fixed: "only", model } a deployment pins this model
 *   { stem, kind: "fixed", fixed: "karaoke", available }  Lead + Backing
 *   { stem, kind: "fixed", fixed: "residual" }    Other
 */
export function stemRows(d) {
  if (!hasStemModels(d)) return [];
  const byStem = new Map();
  for (const entry of d.stem_model_catalog.stems) {
    if (isObj(entry) && typeof entry.stem === "string") byStem.set(entry.stem, entry);
  }
  const rows = [];
  for (const stem of ROW_ORDER) {
    const entry = byStem.get(stem);
    if (!entry) continue;
    if (Array.isArray(entry.choices) && entry.choices.length > 0) {
      rows.push({ stem, kind: "select", choices: entry.choices.map(String) });
    } else if (stem === "lead_backing") {
      rows.push({ stem, kind: "fixed", fixed: "karaoke", available: entry.available !== false });
    } else if (stem === "other" || entry.fixed === "residual") {
      rows.push({ stem, kind: "fixed", fixed: "residual" });
    } else if (typeof entry.fixed === "string") {
      rows.push({ stem, kind: "fixed", fixed: "only", model: entry.fixed });
    }
  }
  return rows;
}

/** The model a select shows: the saved one, or the one "auto" resolves to. */
export function selectedValue(d, stem) {
  const saved = d?.stem_models?.[stem];
  if (!saved || saved === "auto") return d?.stem_models_resolved?.[stem] ?? d?.stem_models_recommended?.[stem] ?? "";
  return saved;
}

/** Whether a model can run on this install. Only an explicit false says no. */
export function modelAvailable(d, id) {
  return models(d)[id]?.available !== false;
}

/**
 * The options of one stem's select: value, label, disabled, recommended, add.
 * Labels read "HTDemucs 6s (Recommended)" or "Kim (Not available on this
 * install)": the model name is not translated, the tags are. A model the user
 * added reads "{name} (yours)" and is never tagged Recommended. With
 * `addOption`, the last option is "Add a model...", which opens My models.
 */
export function stemOptions(d, stem, choices, t, { addOption = false } = {}) {
  const rec = d?.stem_models_recommended?.[stem];
  const out = choices.map((id) => {
    const user = isUserModel(d, id);
    const recommended = !user && id === rec;
    const disabled = !modelAvailable(d, id);
    const tags = [];
    if (recommended) tags.push(t("settings.stemModels.recommendedTag"));
    if (disabled) tags.push(t("settings.stemModels.unavailable"));
    const name = user ? t("settings.myModels.yours", { name: modelName(d, id) }) : modelName(d, id);
    return { value: id, label: tags.length ? `${name} (${tags.join(", ")})` : name, disabled, recommended, add: false };
  });
  if (addOption) out.push({ value: ADD_MODEL_VALUE, label: t("settings.myModels.addOption"), disabled: false, recommended: false, add: true });
  return out;
}

/** What a choice sends: "auto" for the recommended model, so a later change
 *  of machine (a new GPU) keeps following the recommendation. Null for
 *  "Add a model...", which is never sent. */
export function postValue(d, stem, id) {
  if (id === ADD_MODEL_VALUE) return null;
  return id === d?.stem_models_recommended?.[stem] ? "auto" : id;
}

/** The body of "Use recommended": every stem back to "auto". */
export function resetPatch() {
  return { stem_models: Object.fromEntries(CHOICE_STEMS.map((s) => [s, "auto"])) };
}

/**
 * This machine's speed for a model on a device from the last speed test
 * (`speed_test` in the settings answer), or null. Only a result for this
 * hardware counts: a stale one is about another machine. Its estimated rows
 * count too: they scale the registry number by what was measured here, which
 * is closer to this machine than the registry number alone.
 */
export function measuredRtf(d, id, device) {
  const st = d?.speed_test;
  if (!isObj(st) || st.stale === true || !isObj(st.devices)) return null;
  const rtf = Number(st.devices[device]?.[id]?.rtf);
  return Number.isFinite(rtf) && rtf > 0 ? rtf : null;
}

/**
 * The real-time factor of a model on a device: seconds per second of audio.
 * The speed test's number when there is one, else the registry's. The
 * registry's gpu_rtf is a CUDA measurement, so on Apple Silicon there is no
 * number until the speed test has run there.
 */
export function rtfFor(d, id, device) {
  const m = models(d)[id];
  if (!m) return null;
  const measured = measuredRtf(d, id, device);
  if (measured !== null) return measured;
  const cpu = Number.isFinite(m.cpu_rtf) ? m.cpu_rtf : null;
  const gpu = Number.isFinite(m.gpu_rtf) ? m.gpu_rtf : null;
  if (device === "mps") return null;
  if (device === "cuda") return gpu ?? cpu;
  return cpu;
}

/** Seconds one model's pass takes for `seconds` of audio, or null when unknown. */
export function passSeconds(d, id, device, quality, seconds = SONG_SECONDS) {
  const rtf = rtfFor(d, id, device);
  if (rtf === null) return null;
  const factor = quality === "best" && isDemucs(d, id) ? BEST_DEMUCS_FACTOR : 1;
  return rtf * factor * seconds;
}

/** The machine's device and quality, as the answer states them. */
const deviceOf = (d) => d?.demucs_device_resolved || "cpu";
const qualityOf = (d) => d?.separation_quality || "standard";

/**
 * Minutes a 4 minute song takes with a plan ({ stem: model id }): one pass
 * per distinct model. Null when a model's speed is not known.
 */
export function planMinutes(d, plan, { device = deviceOf(d), quality = qualityOf(d), seconds = SONG_SECONDS } = {}) {
  const ids = [...new Set(CHOICE_STEMS.map((s) => plan?.[s]).filter(Boolean))];
  if (!ids.length) return null;
  let total = 0;
  for (const id of ids) {
    const s = passSeconds(d, id, device, quality, seconds);
    if (s === null) return null;
    total += s;
  }
  return total / 60;
}

/** A number with at most `digits` decimals, in the reader's format ("15.6", "15,6"). */
export function formatNumber(value, locale, digits = 1) {
  const step = 10 ** digits;
  const rounded = Math.round(value * step) / step;
  try {
    return new Intl.NumberFormat(locale || undefined, { maximumFractionDigits: digits }).format(rounded);
  } catch {
    return String(rounded);
  }
}

/** Minutes, one decimal, never shown as 0. */
export function formatMinutes(minutes, locale) {
  return formatNumber(Math.max(0.1, minutes), locale, 1);
}

/** The estimate line for the plan the next job uses, or null. */
export function estimateLine(d, t, locale) {
  const minutes = planMinutes(d, d?.stem_models_resolved);
  if (minutes === null) return null;
  return t("settings.stemModels.estimate", { minutes: formatMinutes(minutes, locale) });
}

/** The vocals plan the processor reason compares against: the vocal model
 *  that is not recommended, with every other stem as it resolves. Null when
 *  this install has no other vocal model to offer. */
function alternativeVocalsPlan(d) {
  const entry = d.stem_model_catalog.stems.find((e) => e?.stem === "vocals");
  const rec = d.stem_models_recommended?.vocals;
  // A model the user added is not the "cleaner vocal model" this line is about.
  const other = (entry?.choices || []).find((id) => id !== rec && !isUserModel(d, id) && modelAvailable(d, id));
  return other ? { ...d.stem_models_resolved, vocals: other } : null;
}

/**
 * Why the recommendation is what it is, or null for a reason this page does
 * not know. It follows stem_models_reason, which the server works out from the
 * plan it recommends (a speed test included), and it never says something the
 * Recommended tags contradict.
 */
export function whyLine(d, t, locale) {
  // A deployment that pins its own base model sets every stem: nothing is
  // recommended, so there is nothing to explain.
  if (d?.stem_model_catalog?.deployment) return null;
  const recVocals = d?.stem_models_recommended?.vocals;
  switch (d?.stem_models_reason) {
    case "gpu": {
      // "The cleaner model is recommended" is only true when it is.
      if (!recVocals || isDemucs(d, recVocals)) return null;
      const s = passSeconds(d, recVocals, deviceOf(d), qualityOf(d));
      if (s === null) return null;
      const line = t("settings.stemModels.why.gpu", { seconds: formatNumber(s, locale, 0) });
      const m = models(d)[recVocals];
      if (m?.downloaded === true) return line;
      const mb = Math.max(1, Math.round((Number(m?.download_bytes) || 0) / 1e6));
      return `${line} ${t("settings.stemModels.why.gpuDownload", { size: formatNumber(mb, locale, 0) })}`;
    }
    case "over-budget": {
      const plan = alternativeVocalsPlan(d);
      const minutes = plan ? planMinutes(d, plan) : null;
      if (minutes === null) return null;
      return t("settings.stemModels.why.overBudget", { minutes: formatMinutes(minutes, locale) });
    }
    case "mps": return t("settings.stemModels.why.mps");
    case "unavailable": return t("settings.stemModels.why.unavailable");
    case "low-vram": {
      const mb = Number(d.stem_model_catalog?.min_vram_mb) || 4096;
      return t("settings.stemModels.why.lowVram", { gb: formatNumber(mb / 1024, locale, 1) });
    }
    case "cpu": {
      // No other vocal model here: a time for it would be about nothing.
      const plan = alternativeVocalsPlan(d);
      if (!plan) return t("settings.stemModels.why.unavailable");
      const minutes = planMinutes(d, plan);
      if (minutes === null) return null;
      return t("settings.stemModels.why.cpu", { minutes: formatMinutes(minutes, locale) });
    }
    default: return null;
  }
}

/** The size and licence under a select, for the model it shows. A model the
 *  user added says the licence its source declared, which StemDeck never checks. */
export function hintLine(d, id, t, locale) {
  const m = models(d)[id];
  if (!m) return "";
  if (isUserModel(d, id)) {
    return typeof m.license === "string" && m.license
      ? t("settings.myModels.hint", { license: m.license })
      : t("settings.myModels.hintNoLicence");
  }
  const license = m.license || "?";
  if (m.downloaded === true) return t("settings.stemModels.hintDownloaded", { license });
  const mb = Math.max(1, Math.round((Number(m.download_bytes) || 0) / 1e6));
  return t("settings.stemModels.hint", { size: formatNumber(mb, locale, 0), license });
}

/** The model's own note ("settings.stemModels.note.<id>"), or "". */
export function noteLine(d, id, t) {
  const key = models(d)[id]?.note_key;
  return typeof key === "string" && key.startsWith("settings.stemModels.note.") ? t(key) : "";
}

/**
 * The warning under a select for the model it shows, or null:
 *   unavailable  the model cannot run on this install (the next job uses the recommendation)
 *   untestedMps  a model that is not Demucs, on Apple Silicon
 *   slow         slower than the song itself on this machine, with minutes
 */
export function warningFor(d, id, t, locale) {
  if (!models(d)[id]) return null;
  if (!modelAvailable(d, id)) return { kind: "unavailable", text: t("settings.stemModels.unavailable") };
  const device = deviceOf(d);
  if (device === "mps" && !isDemucs(d, id)) return { kind: "untestedMps", text: t("settings.stemModels.untestedMps") };
  const s = passSeconds(d, id, device, qualityOf(d));
  if (s !== null && s / SONG_SECONDS > SLOW_RTF) {
    return { kind: "slow", text: t("settings.stemModels.slow", { minutes: formatMinutes(s / 60, locale) }) };
  }
  return null;
}

/** The read-only line of a row without a choice. */
export function fixedLine(d, row, t) {
  if (row.fixed === "karaoke") return t("settings.stemModels.fixed.karaoke");
  if (row.fixed === "residual") return t("settings.stemModels.fixed.residual");
  return t("settings.stemModels.fixed.only", { name: modelName(d, row.model) });
}
