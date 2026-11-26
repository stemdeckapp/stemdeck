// MIDI from a song's lanes (Advanced mode), the pure part: which lanes get
// a MIDI chip and when it shows, what its row says and offers in each state,
// what a refusal or a failure says, the .mid file's name and URL, the
// Settings > Transcription rules and the Export menu's Include MIDI. No DOM,
// so tests/js/lane-midi-view.test.mjs runs it in node. laneMidiRows.js draws
// the chip and the row, transcriptionSettings.js the Settings tab.
//
// Server side (docs/api.md, the transcription routes):
//   GET  /api/transcribe/status         { available, reason, engines, stem_engines,
//                                         default_engines, stems, max_seconds, details }
//   POST /api/jobs/{id}/midi            { stem, detail? }  202 { state: "running" }
//   POST /api/jobs/{id}/midi/{stem}/cancel
//   DELETE /api/jobs/{id}/midi/{stem}
//   GET  /api/jobs/{id}/stems/{stem}.mid
//   GET  /api/jobs/{id}/stems/all.zip?midi=1   the zip carries the .mid files
// A run is followed by reading GET /api/jobs/{id}, whose `midi` is
// { <stem>?: M }, M = { state: running | done | error | cancelled, pct 0..100,
// phase (running only): download | prepare | transcribe, notes, engine,
// detail, tempo_source: song | default | null, at, error_code, has_file }.
// The song's event stream closes at once for a finished song, so it carries
// no progress here. A refusal is { detail, code }.
//
// Settings (GET/POST /api/settings), for each of the six stems:
// transcribe_<stem> is "auto" | "off" (default "auto") and
// transcribe_<stem>_engine the engine that makes its notes, one of the
// status's stem_engines[<stem>] (default its default_engines[<stem>]).
// transcribe_detail is "balanced" | "detailed" | "clean" (default
// "balanced"). The status's engines list each engine with { id, label,
// version, licence, model_licence, stems, available, reason, source, model:
// built_in | downloaded | partial | not_downloaded, download_bytes,
// disk_bytes, device: cpu | cuda, seconds_per_minute, uses_detail }.

/** The lanes with a MIDI chip: every stem, in the order Settings lists them. */
export const MIDI_LANES = Object.freeze(["vocals", "drums", "bass", "guitar", "piano", "other"]);

/** The Settings field that turns each lane's MIDI on or off. */
export const SETTING_FIELDS = Object.freeze(Object.fromEntries(MIDI_LANES.map((l) => [l, `transcribe_${l}`])));
/** The Settings field that picks each lane's engine. */
export const ENGINE_FIELDS = Object.freeze(Object.fromEntries(MIDI_LANES.map((l) => [l, `transcribe_${l}_engine`])));
export const DETAIL_FIELD = "transcribe_detail";
/** Every Settings field the Transcription tab owns. */
export const TRANSCRIPTION_FIELDS = Object.freeze([
  ...Object.values(SETTING_FIELDS), DETAIL_FIELD, ...Object.values(ENGINE_FIELDS),
]);

export const LANE_CHOICES = Object.freeze(["auto", "off"]);
export const DETAILS = Object.freeze(["balanced", "detailed", "clean"]);
/** The engines built in, in the order Settings lists them. */
export const ENGINES = Object.freeze(["basic_pitch", "piano_hr", "ymt3"]);
/**
 * What each lane can be transcribed with, and its engine until the person
 * picks another, when the status does not say (an older server, a failed
 * read): config.MIDI_ENGINE_STEMS and MIDI_DEFAULT_ENGINES of this release.
 */
export const LANE_ENGINES = Object.freeze({
  vocals: ["basic_pitch"],
  drums: ["ymt3"],
  bass: ["basic_pitch", "ymt3"],
  guitar: ["basic_pitch", "ymt3"],
  piano: ["basic_pitch", "piano_hr", "ymt3"],
  other: ["basic_pitch", "ymt3"],
});
export const DEFAULT_ENGINES = Object.freeze({
  vocals: "basic_pitch", drums: "ymt3", bass: "ymt3", guitar: "basic_pitch", piano: "ymt3", other: "ymt3",
});
export const DEFAULT_LANE_CHOICE = "auto";
export const DEFAULT_DETAIL = "balanced";
// An engine id as the server writes one (a person's own engine included).
const ENGINE_ID_RE = /^[a-z][a-z0-9_]{0,31}$/;

/** How often the page reads the song while a transcription runs, ms. */
export const POLL_MS = 1000;
/** Reads in a row that may fail before the page stops and says so. */
export const POLL_FAIL_LIMIT = 5;
/** How long Remove waits for its second press, ms (the track Delete tool's 3 s). */
export const REMOVE_ARM_MS = 3000;
/** The Export menu's Include MIDI, per browser (off unless it says "1"). */
export const EXPORT_MIDI_STORE_KEY = "stemdeck.exportMidi";

const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);

/** Whether a lane gets a MIDI chip. */
export function laneHasMidi(lane) {
  return MIDI_LANES.includes(lane);
}

/** The lane's name in the Transcription tab and the chips' sentences (the lane's own name). */
export function laneNameKey(lane) {
  return `stem.${lane}`;
}

// ─── Settings ───

/** The lane's setting from a settings answer: "auto" or "off". */
export function laneChoice(settings, lane) {
  const v = isObj(settings) ? settings[SETTING_FIELDS[lane]] : undefined;
  return LANE_CHOICES.includes(v) ? v : DEFAULT_LANE_CHOICE;
}

/** The Detail setting from a settings answer. */
export function detailChoice(settings) {
  const v = isObj(settings) ? settings[DETAIL_FIELD] : undefined;
  return DETAILS.includes(v) ? v : DEFAULT_DETAIL;
}

/** The engines a lane can be set to: the status's list, else this release's. */
export function laneEngines(status, lane) {
  const listed = isStatus(status) && isObj(status.stem_engines) ? status.stem_engines[lane] : null;
  if (Array.isArray(listed)) {
    const ids = listed.filter((id) => typeof id === "string" && ENGINE_ID_RE.test(id));
    if (ids.length) return ids;
  }
  return LANE_ENGINES[lane] ? [...LANE_ENGINES[lane]] : [];
}

/** The lane's engine until the person picks one: the status's, else this release's. */
export function defaultEngine(status, lane) {
  const said = isStatus(status) && isObj(status.default_engines) ? status.default_engines[lane] : null;
  if (typeof said === "string" && laneEngines(status, lane).includes(said)) return said;
  return DEFAULT_ENGINES[lane] || laneEngines(status, lane)[0] || "";
}

/** The engine that makes a lane's notes: its setting when the lane can take it, else its default. */
export function laneEngine(settings, lane, status = null) {
  const v = isObj(settings) ? settings[ENGINE_FIELDS[lane]] : undefined;
  return typeof v === "string" && laneEngines(status, lane).includes(v) ? v : defaultEngine(status, lane);
}

/** Whether a settings answer says anything about transcription. */
export function hasTranscriptionFields(settings) {
  return isObj(settings) && TRANSCRIPTION_FIELDS.some((f) => f in settings);
}

/** Only the Transcription fields of a settings answer. */
export function transcriptionFields(settings) {
  const out = {};
  if (!isObj(settings)) return out;
  for (const f of TRANSCRIPTION_FIELDS) if (f in settings) out[f] = settings[f];
  return out;
}

/**
 * The POST /api/settings body for one change in the tab, or null for a
 * field or value the tab does not offer (never posted).
 */
export function settingsPatch(field, value, status = null) {
  if (field === DETAIL_FIELD) return DETAILS.includes(value) ? { [field]: value } : null;
  const engineLane = MIDI_LANES.find((l) => ENGINE_FIELDS[l] === field);
  if (engineLane) return laneEngines(status, engineLane).includes(value) ? { [field]: value } : null;
  if (Object.values(SETTING_FIELDS).includes(field)) {
    return LANE_CHOICES.includes(value) ? { [field]: value } : null;
  }
  return null;
}

/** The i18n key of each Detail choice's name and of its one sentence. */
export function detailNameKey(detail) {
  return `settings.midi.detail.${DETAILS.includes(detail) ? detail : DEFAULT_DETAIL}`;
}
export function detailDescKey(detail) {
  return `${detailNameKey(detail)}Desc`;
}

// ─── The engine ───

/** Whether a status answer is one (the route exists and answered). */
export function isStatus(status) {
  return isObj(status) && typeof status.available === "boolean";
}

/** Whether transcription can run on this computer. */
export function engineAvailable(status) {
  return isStatus(status) && status.available === true;
}

/**
 * Why transcription cannot run, as an i18n key, or "" when it can.
 * No answer at all (an older server, a failed read) has its own sentence.
 */
export function unavailableKey(status) {
  if (!isStatus(status)) return "settings.midi.reason.unknown";
  if (status.available) return "";
  if (status.reason === "unsupported_platform") return "settings.midi.reason.unsupported_platform";
  return "settings.midi.reason.engine";
}

/** The status's entry for one engine, or null. */
export function engineOf(status, id) {
  if (!isStatus(status) || !Array.isArray(status.engines)) return null;
  return status.engines.find((e) => isObj(e) && e.id === id) || null;
}

/**
 * Whether the engine can run here. An older server lists one engine with no
 * "available" of its own: Basic Pitch, as available as transcription is.
 */
export function engineReady(status, id) {
  if (!engineAvailable(status)) return false;
  const e = engineOf(status, id);
  if (!e) return false;
  return e.available !== false;
}

/**
 * The engine lines, one per engine the status lists ("Engine: Basic Pitch
 * 0.4.0, licence Apache-2.0."), with the model's licence when it differs
 * from the code's, joined by a space; "" when there is none.
 */
export function engineLine(status, t) {
  const engines = isStatus(status) && Array.isArray(status.engines) ? status.engines.filter(isObj) : [];
  const lines = [];
  for (const e of engines) {
    const name = String(e.label || e.id || "").trim();
    if (!name) continue;
    const vars = {
      name,
      version: String(e.version || "").trim() || "?",
      licence: String(e.licence || "").trim() || "?",
    };
    const model = String(e.model_licence || "").trim();
    lines.push(model && model !== vars.licence
      ? t("settings.midi.engineModel", { ...vars, model })
      : t("settings.midi.engine", vars));
  }
  return lines.join(" ");
}

/** The i18n key of an engine's name (a built-in one), or "" for any other. */
export function engineNameKey(id) {
  return ENGINES.includes(id) ? `settings.midi.engineName.${id}` : "";
}

/** An engine's name: its i18n name when it is built in, else the status's label, else its id. */
export function engineName(status, id, t) {
  const key = engineNameKey(id);
  if (key) return t(key);
  const e = engineOf(status, id);
  return String(e?.label || id || "").trim();
}

/** Whether an engine's notes change with Detail (an older server: every engine's did). */
export function usesDetail(status, id) {
  const e = engineOf(status, id);
  return !e || e.uses_detail !== false;
}

/** Seconds of work per minute of song on `device`, a whole number, or null. */
function speedOf(e, device) {
  const v = Number(isObj(e?.seconds_per_minute) ? e.seconds_per_minute[device] : NaN);
  return Number.isFinite(v) && v > 0 ? Math.round(v) : null;
}

/**
 * What a lane's engine needs said before it runs, as a list of { key, vars }
 * ([] when nothing): that it cannot run here, or the model it downloads the
 * first time (its size) or keeps (its size on disk), and how long it takes
 * on this computer (the device a run would use). Under the engine's choice in
 * Settings and in the lane's row before a first run.
 */
export function engineNotes(status, settings, lane, t, sizeText = (b) => `${Math.round(b / 1e6)} MB`) {
  if (!isStatus(status)) return [];
  const id = laneEngine(settings, lane, status);
  const e = engineOf(status, id);
  const engine = engineName(status, id, t);
  if (!e) return [];
  if (!engineReady(status, id)) return [{ key: "settings.midi.engineUnavailable", vars: { engine } }];
  const notes = [];
  const download = Number(e.download_bytes);
  const disk = Number(e.disk_bytes);
  if (e.model === "downloaded" && Number.isFinite(disk) && disk > 0) {
    notes.push({ key: "settings.midi.engineModel.downloaded", vars: { engine, size: sizeText(disk) } });
  } else if (e.model !== "built_in" && Number.isFinite(download) && download > 0) {
    notes.push({ key: "settings.midi.engineModel.notDownloaded", vars: { engine, size: sizeText(download) } });
  }
  const device = e.device === "cuda" ? "cuda" : "cpu";
  const seconds = speedOf(e, device);
  if (seconds !== null) {
    const minutes = Math.max(1, Math.round((4 * seconds) / 60));
    notes.push({ key: `settings.midi.engineSpeed.${device}`, vars: { engine, seconds, minutes } });
  }
  return notes;
}

/** engineNotes as one string, each sentence translated, joined by a space. */
export function engineNoteText(status, settings, lane, t, sizeText) {
  return engineNotes(status, settings, lane, t, sizeText).map((n) => t(n.key, n.vars)).join(" ");
}

/**
 * What the lane's row says before a run, after its idle sentence: the model
 * the engine downloads the first time (its size), and how long it takes when
 * this computer runs it on the processor. "" when there is nothing to warn of.
 */
export function rowNoteText(status, settings, lane, t, sizeText) {
  const said = ["settings.midi.engineModel.notDownloaded", "settings.midi.engineSpeed.cpu"];
  return engineNotes(status, settings, lane, t, sizeText)
    .filter((n) => said.includes(n.key))
    .map((n) => t(n.key, n.vars))
    .join(" ");
}

/** Whether the lane's engine still has to fetch its model (its first run). */
export function needsDownload(status, settings, lane) {
  const e = engineOf(status, laneEngine(settings, lane, status));
  return !!e && e.model !== "built_in" && e.model !== "downloaded" && Number(e.download_bytes) > 0;
}

/** The longest song the engine takes, in whole minutes (20 when unknown). */
export function maxMinutes(status) {
  const s = Number(isStatus(status) ? status.max_seconds : NaN);
  return Number.isFinite(s) && s > 0 ? Math.max(1, Math.floor(s / 60)) : 20;
}

// ─── A lane's MIDI ───

export const STATES = Object.freeze(["running", "done", "error", "cancelled"]);

/** The lane's MIDI state from a job state, normalised, or null when it has none. */
export function midiOf(job, lane) {
  const m = isObj(job?.midi) ? job.midi[lane] : null;
  if (!isObj(m) || !STATES.includes(m.state)) return null;
  const notes = Number(m.notes);
  return {
    state: m.state,
    pct: midiPct(m),
    phase: m.state === "running" ? (["download", "prepare"].includes(m.phase) ? m.phase : "transcribe") : null,
    notes: Number.isFinite(notes) && notes >= 0 ? Math.round(notes) : null,
    engine: typeof m.engine === "string" ? m.engine : "",
    detail: DETAILS.includes(m.detail) ? m.detail : "",
    tempo_source: m.tempo_source === "song" || m.tempo_source === "default" ? m.tempo_source : null,
    at: typeof m.at === "string" ? m.at : "",
    error_code: typeof m.error_code === "string" && m.error_code ? m.error_code : null,
    has_file: m.has_file === true,
  };
}

/** A run's progress, a whole percent from 0 to 100. */
export function midiPct(m) {
  const p = Number(m?.pct);
  return Number.isFinite(p) ? Math.max(0, Math.min(100, Math.round(p))) : 0;
}

/** The progress as the row and the chip show it: "42%". */
export function progressText(m) {
  return `${midiPct(m)}%`;
}

/** Whether a transcription of any lane of the song is running. */
export function anyRunning(job) {
  return MIDI_LANES.some((lane) => midiOf(job, lane)?.state === "running");
}

/** Whether the song has at least one MIDI file (the Export menu's switch shows). */
export function hasFinishedMidi(job) {
  return MIDI_LANES.some((lane) => midiOf(job, lane)?.has_file === true);
}

/** Whether the song is finished (only a finished song can be transcribed). */
export function songDone(job) {
  return isObj(job) && job.status === "done";
}

/**
 * Whether the lane's chip shows, and whether it can be pressed:
 *   hidden  the lane has none, its setting is Off, the song is not finished,
 *           or the server has no transcription at all (no status answer)
 *   off     shown and not pressable: the lane's engine cannot run here, the
 *           reason (`reasonKey`) is its tooltip
 * A lane that has a stem is assumed (the lanes are the song's stems).
 */
export function chipState({ lane, settings, status, job }) {
  if (!laneHasMidi(lane) || laneChoice(settings, lane) === "off" || !songDone(job) || !isStatus(status)) {
    return { show: false, off: false, reasonKey: "" };
  }
  let reasonKey = unavailableKey(status);
  const engine = laneEngine(settings, lane, status);
  // The lane's engine is not one this computer can run (or the server does
  // not list it): the chip is off, and its tooltip names the engine.
  if (!reasonKey && !engineReady(status, engine)) reasonKey = "laneMidi.engineUnavailable";
  return { show: true, off: Boolean(reasonKey), reasonKey, engine };
}

/**
 * The chip's mark: "" (nothing), "run" (the percent under MIDI), "done" (a
 * dot: a file is there), "error" (a red dot: the last run failed).
 */
export function chipMark(m) {
  if (!m) return "";
  if (m.state === "running") return "run";
  if (m.state === "error") return "error";
  return m.has_file ? "done" : "";
}

/** The chip's tooltip and accessible name. `name` is the lane's display name. */
export function chipLabel(m, { name, off = false, reasonKey = "", engine = "" }, t) {
  if (off) {
    if (!reasonKey) return t("laneMidi.tab.idle", { name });
    const key = engineNameKey(engine);
    return t(reasonKey, { name, engine: key ? t(key) : engine });
  }
  if (!m) return t("laneMidi.tab.idle", { name });
  if (m.state === "running") return t("laneMidi.tab.running", { name, pct: midiPct(m) });
  if (m.state === "error") return t("laneMidi.tab.error", { name });
  if (m.has_file) return t("laneMidi.tab.done", { name });
  return t("laneMidi.tab.idle", { name });
}

/**
 * What the row offers and says for a lane's state. `name` the lane's name,
 * `sending` a request of this page is on its way, `otherRunning` another
 * lane's transcription of the song runs, `removeArmed` Remove waits for its
 * second press, `maxMin` the longest song in minutes, `t` the translator
 * and `plural` i18n's plural() for the note count.
 *   buttons: transcribe | rerun | retry | cancel | download | remove, each
 *            { show, disabled }
 *   run:     whether the progress and its percent show
 *   status:  { kind, text }, kind idle | running | done | error | cancelled
 *   hint:    the engine's detail and tempo for a finished file, or ""
 */
export function rowView(m, { name, sending = false, otherRunning = false, removeArmed = false, maxMin = 20, firstRun = "", detail = true }, t, plural = null) {
  const state = m?.state || "idle";
  const running = state === "running";
  const hasFile = m?.has_file === true;
  const busy = sending || otherRunning;
  const b = (show, disabled = false) => ({ show, disabled: show && disabled });
  const buttons = {
    // A first run (or one after a stop with no file kept).
    transcribe: b(!running && !hasFile && state !== "error", busy),
    // A file is there: make it again; the old one stays until the new is done.
    rerun: b(!running && hasFile && state !== "error", busy),
    retry: b(state === "error", busy),
    cancel: b(running, sending),
    download: b(hasFile, false),
    remove: b(hasFile && !running, busy),
  };
  let status;
  if (running) {
    const doing = m.phase === "download"
      ? t("laneMidi.downloadingModel")
      : m.phase === "prepare" ? t("laneMidi.preparingModel") : t("laneMidi.running", { name });
    const text = [doing, hasFile ? t("laneMidi.keptWhileRunning") : ""].filter(Boolean).join(" ");
    status = { kind: "running", text };
  } else if (state === "error") {
    status = { kind: "error", text: t(errorKey(m.error_code), { minutes: maxMin, name }) };
  } else if (state === "cancelled") {
    status = { kind: "cancelled", text: t(hasFile ? "laneMidi.cancelledKept" : "laneMidi.cancelled") };
  } else if (hasFile) {
    status = { kind: "done", text: m.notes === null ? t("laneMidi.doneNoCount", { name }) : notesText(m.notes, name, t, plural) };
  } else {
    // Before a first run that downloads a model: its size, and how long the
    // engine takes on this computer, said before the person presses.
    status = { kind: "idle", text: [t("laneMidi.idle", { name }), firstRun].filter(Boolean).join(" ") };
  }
  if (otherRunning && !running) status = { kind: "other", text: t("laneMidi.otherRunning") };
  if (removeArmed && buttons.remove.show) status = { kind: "error", text: t("laneMidi.removeAgain") };
  return { buttons, run: running, pct: running ? midiPct(m) : 0, status, hint: hintLine(m, t, detail) };
}

/** "312 notes from Bass." with the plural the language needs (i18n's plural()). */
export function notesText(count, name, t, plural = null) {
  return plural ? plural("laneMidi.notes", count, { name }) : t("laneMidi.notes.other", { count, name });
}

/**
 * The line under the status for a file: the detail it was made with (unless
 * its engine has none, `detail` false), and its tempo.
 */
export function hintLine(m, t, detail = true) {
  if (!m?.has_file) return "";
  const parts = [];
  if (m.detail && detail) parts.push(t("laneMidi.detailLine", { detail: t(detailNameKey(m.detail)) }));
  if (m.tempo_source === "song") parts.push(t("laneMidi.tempo.song"));
  else if (m.tempo_source === "default") parts.push(t("laneMidi.tempo.default"));
  return parts.join(" ");
}

// ─── Failures and refusals ───

/** The failure codes a run can end with, each with its sentence. */
export const ERROR_CODES = Object.freeze([
  "too_long", "disk", "io", "interrupted", "engine_unavailable", "worker_failed", "no_audio", "engine", "timeout", "stalled",
  "download", "prepare",
]);

/** The i18n key for a run's error_code; one this page does not know gets the general one. */
export function errorKey(code) {
  return ERROR_CODES.includes(code) ? `laneMidi.error.${code}` : "laneMidi.error.unknown";
}

/** The refusal codes POST /api/jobs/{id}/midi (and cancel, remove) answer with. */
export const REFUSAL_CODES = Object.freeze([
  "job_not_found", "stem_not_supported", "bad_detail", "disabled", "busy", "no_stem", "too_long", "engine_unavailable",
]);

/** The code of a refused request: the body's own, else read from the status. */
export function refusalCode(status, body) {
  if (isObj(body) && typeof body.code === "string" && body.code) return body.code;
  if (status === 0) return "offline";
  if (status === 404) return "job_not_found";
  return "unknown";
}

/** The i18n key of a refusal's sentence. */
export function refusalKey(code) {
  if (code === "too_long" || code === "engine_unavailable") return errorKey(code);
  if (code === "offline") return "laneMidi.lost";
  // The song-wide "a job runs on this song" refusal (errorReason.js JOB_BUSY)
  // says the same as the MIDI route's own busy.
  if (code === "job_busy") return "laneMidi.refusal.busy";
  return REFUSAL_CODES.includes(code) ? `laneMidi.refusal.${code}` : "laneMidi.refusal.unknown";
}

/** A refused request as the row says it: { kind: "error", text, code }. */
export function refusalNote(status, body, { name, maxMin = 20 }, t) {
  const code = refusalCode(status, body);
  return { kind: "error", text: t(refusalKey(code), { name, minutes: maxMin }), code };
}

// ─── The file ───

/** The same safe name the stems zip uses for the song (player.js). */
export function safeTitle(title) {
  return String(title || "")
    .replace(/[^a-zA-Z0-9]+/g, "_")
    .replace(/_{2,}/g, "_")
    .slice(0, 80)
    .replace(/^_+|_+$/g, "");
}

/** The .mid file's name: "<song>_<lane>.mid", or "<lane>.mid". */
export function midiFilename(title, lane) {
  const safe = safeTitle(title);
  return safe ? `${safe}_${lane}.mid` : `${lane}.mid`;
}

/**
 * The .mid file's URL. A run replaces the file at the same path, so its time
 * goes in the query: a browser never hands back the file of an earlier run.
 */
export function midiUrl(jobId, lane, m) {
  const base = `/api/jobs/${encodeURIComponent(jobId)}/stems/${encodeURIComponent(lane)}.mid`;
  return m?.at ? `${base}?v=${encodeURIComponent(m.at)}` : base;
}

// ─── Export ───

/** The stored Include MIDI choice: on only when it says "1". */
export function parseExportMidi(raw) {
  return raw === "1";
}

/** Whether the stems zip asks for the MIDI files: the box is ticked and the song has one. */
export function zipWantsMidi(checked, job) {
  return checked === true && hasFinishedMidi(job);
}
