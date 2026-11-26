// Settings > Models: what the built-in list says. Pure functions with `t`
// (i18n.t) passed in, so tests/js/models-view.test.mjs runs them without a
// browser. modelsSettings.js draws the list from what these return.
//
// Server: GET /api/models (app/core/model_inventory.py)
//   { models: [{ id, group, use, name, stems, license, state, size_bytes,
//     download_bytes, available }], remove: false }
// group is separation, transcription or analysis; use is stems,
// lead_backing, midi (Basic Pitch), midi_piano (ByteDance Piano
// Transcription), midi_multi (YourMT3+), lyrics, lyrics_gpu, lyrics_cpu or
// beats; state is downloaded, partial, not_downloaded, built_in or unknown.
//
// "Used for" comes from a settings answer the page already has, never from
// this route: stem_models_resolved (what the next job runs for each stem) for
// a separation model, including one the person added, and transcribe_<lane>
// with transcribe_<lane>_engine for the MIDI engines. So it follows a choice
// the moment the Separation or Transcription tab saves one.

import { formatSize, listText } from "./myModelsView.js";
import { isHttpsLink, linkedSentence } from "./sourceLink.js";
import { STEM_LABEL } from "./stemModelsView.js";
import { ENGINE_FIELDS, MIDI_LANES, SETTING_FIELDS, laneChoice, laneEngine, laneNameKey } from "./laneMidiView.js";

/** The groups, in the order the tab lists them, each with its title key. */
export const GROUPS = [
  { id: "separation", title: "settings.models.group.separation" },
  { id: "transcription", title: "settings.models.group.transcription" },
  { id: "analysis", title: "settings.models.group.analysis" },
];

/** The stems a separation model can be chosen for, in Settings' order. */
export const PLAN_STEMS = ["vocals", "drums", "bass", "guitar", "piano"];
/** The stems MIDI is made from (laneMidiView.MIDI_LANES). */
export { MIDI_LANES };

const STATES = ["downloaded", "partial", "not_downloaded", "built_in", "unknown"];
const USES = {
  stems: "settings.models.purpose.stems",
  lead_backing: "settings.models.purpose.leadBacking",
  midi: "settings.models.purpose.midi",
  midi_piano: "settings.models.purpose.midiPiano",
  midi_multi: "settings.models.purpose.midiMulti",
  lyrics: "settings.models.purpose.lyrics",
  lyrics_gpu: "settings.models.purpose.lyricsGpu",
  lyrics_cpu: "settings.models.purpose.lyricsCpu",
  beats: "settings.models.purpose.beats",
};

const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);

/** Whether a GET /api/models answer is one (an older server has no route). */
export function isModelsAnswer(d) {
  return isObj(d) && Array.isArray(d.models);
}

/** The entries of an answer worth a row: an id, a name, a known group. */
export function modelEntries(d) {
  if (!isModelsAnswer(d)) return [];
  return d.models.filter(
    (m) => isObj(m) && typeof m.id === "string" && m.id && typeof m.name === "string" && m.name
      && GROUPS.some((g) => g.id === m.group),
  );
}

/** The entries by purpose, in GROUPS order, each group in the server's
 *  order; a group with no entry is left out. [{ id, title, models }] */
export function groupByPurpose(d) {
  const entries = modelEntries(d);
  return GROUPS.map((g) => ({ ...g, models: entries.filter((m) => m.group === g.id) })).filter((g) => g.models.length);
}

const stemLabel = (stem, t) => t(STEM_LABEL[stem] || `stem.${stem}`);

/** The stems the next job separates with `modelId`, from a settings answer's
 *  stem_models_resolved, in Settings' order. */
export function usedForStems(settings, modelId) {
  const plan = isObj(settings) ? settings.stem_models_resolved : null;
  if (!isObj(plan) || !modelId) return [];
  return PLAN_STEMS.filter((s) => plan[s] === modelId);
}

/** The engine id each MIDI model entry is ("midi" is Basic Pitch). */
const MIDI_ENGINE_OF_USE = { midi: "basic_pitch", midi_piano: "piano_hr", midi_multi: "ymt3" };

/** The lanes an engine makes MIDI for: the ones not turned Off whose engine
 *  it is (Basic Pitch when `engine` is left out). */
export function usedForLanes(settings, engine = "basic_pitch") {
  return MIDI_LANES.filter((lane) => laneChoice(settings, lane) !== "off" && laneEngine(settings, lane) === engine);
}

/** Whether a settings answer carries what "Used for" is worked out from. */
export function hasPlan(settings) {
  return isObj(settings) && (isObj(settings.stem_models_resolved)
    || MIDI_LANES.some((l) => SETTING_FIELDS[l] in settings || ENGINE_FIELDS[l] in settings));
}

/**
 * The "Used for" line of an entry, or "" when there is nothing to say (a
 * model whose use does not depend on a choice, or no settings yet).
 * A separation model: the stems it separates for the next job, or "not used".
 * The Karaoke split: always, for Lead + Backing. A MIDI engine: the lanes on
 * that it makes the notes of.
 */
export function usedForLine(entry, settings, t, locale) {
  if (!isObj(entry)) return "";
  if (entry.use === "lead_backing") return t("settings.models.usedFor", { stems: stemLabel("lead_backing", t) });
  if (entry.use in MIDI_ENGINE_OF_USE) {
    if (!isObj(settings)) return "";
    const lanes = usedForLanes(settings, MIDI_ENGINE_OF_USE[entry.use]);
    if (!lanes.length) {
      const allOff = MIDI_LANES.every((lane) => laneChoice(settings, lane) === "off");
      return t(allOff ? "settings.models.usedForNoneMidi" : "settings.models.usedForNone");
    }
    return t("settings.models.usedFor", { stems: listText(lanes.map((l) => t(laneNameKey(l))), locale) });
  }
  if (entry.use !== "stems") return "";
  if (!isObj(settings) || !isObj(settings.stem_models_resolved)) return "";
  const stems = usedForStems(settings, entry.id);
  if (!stems.length) return t("settings.models.usedForNone");
  return t("settings.models.usedFor", { stems: listText(stems.map((s) => stemLabel(s, t)), locale) });
}

/** What the model does, in one sentence. */
export function purposeLine(entry, t, locale) {
  const key = USES[entry?.use];
  if (!key) return "";
  if (entry.use === "stems") {
    const stems = (Array.isArray(entry.stems) ? entry.stems : []).map((s) => stemLabel(s, t));
    return t(key, { stems: listText(stems, locale) });
  }
  return t(key);
}

/** "Licence: MIT.", or "" when the code knows none. */
export function licenceLine(entry, t) {
  const l = typeof entry?.license === "string" ? entry.license.trim() : "";
  return l ? t("settings.models.licence", { licence: l }) : "";
}

/**
 * A built-in model's "Source" line: who made it and the page they publish
 * it on, linked; "Source: not verified" with no link when the server has
 * no verified source for it (a model a deployment swapped in).
 */
export function sourceView(entry, t) {
  const src = isObj(entry?.source) ? entry.source : null;
  const author = typeof src?.author === "string" ? src.author.trim() : "";
  if (!author || !isHttpsLink(src.url)) return { before: t("settings.models.sourceNotVerified"), link: null, after: "" };
  return linkedSentence("settings.models.source", "settings.models.sourceNotVerified", { author }, src.url, t);
}

/** Bytes as the My models list writes them ("55 MB", "1,6 GB" in some
 *  languages), or null for no size. */
export function sizeText(bytes, t, locale) {
  const b = Number(bytes);
  if (!Number.isFinite(b) || b <= 0) return null;
  return formatSize(b, t, locale);
}

/**
 * What the badge on a row says, and its state for the colour:
 *   { state, text }
 * state is the entry's own, or "unavailable" for a model this install
 * cannot run and does not have.
 */
export function stateView(entry, t, locale) {
  const state = STATES.includes(entry?.state) ? entry.state : "unknown";
  const size = sizeText(entry?.size_bytes, t, locale);
  const full = sizeText(entry?.download_bytes, t, locale);
  if (state === "built_in") return { state, text: t("settings.models.state.builtIn") };
  if (state === "downloaded") {
    return { state, text: size ? t("settings.models.state.downloaded", { size }) : t("settings.models.state.downloadedNoSize") };
  }
  if (state === "partial") {
    return { state, text: t("settings.models.state.partial", { size: size || "?" }) };
  }
  if (state === "unknown") return { state, text: t("settings.models.state.unknown") };
  if (entry?.available === false) return { state: "unavailable", text: t("settings.models.state.unavailable") };
  return {
    state,
    text: full ? t("settings.models.state.notDownloaded", { size: full }) : t("settings.models.state.notDownloadedNoSize"),
  };
}
