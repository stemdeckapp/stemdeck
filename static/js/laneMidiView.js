// MIDI from the Piano and Bass lanes (Advanced mode), the pure part: which
// lanes get a MIDI chip and when it shows, what its row says and offers in
// each state, what a refusal or a failure says, the .mid file's name and URL,
// the Settings > Transcription rules and the Export menu's Include MIDI. No
// DOM, so tests/js/lane-midi-view.test.mjs runs it in node. laneMidiRows.js
// draws the chip and the row, transcriptionSettings.js the Settings tab.
//
// Server side (docs/api.md, the transcription routes):
//   GET  /api/transcribe/status         { available, reason, engines, stems, max_seconds, details }
//   POST /api/jobs/{id}/midi            { stem, detail? }  202 { state: "running" }
//   POST /api/jobs/{id}/midi/{stem}/cancel
//   DELETE /api/jobs/{id}/midi/{stem}
//   GET  /api/jobs/{id}/stems/{stem}.mid
//   GET  /api/jobs/{id}/stems/all.zip?midi=1   the zip carries the .mid files
// A run is followed by reading GET /api/jobs/{id}, whose `midi` is
// { piano?: M, bass?: M }, M = { state: running | done | error | cancelled,
// pct 0..100, notes, engine, detail, tempo_source: song | default | null, at,
// error_code, has_file }. The song's event stream closes at once for a
// finished song, so it carries no progress here. A refusal is { detail, code }.
//
// Settings (GET/POST /api/settings): transcribe_piano and transcribe_bass are
// "auto" | "off" (default "auto"), transcribe_detail is "balanced" |
// "detailed" | "clean" (default "balanced"), transcribe_piano_engine is
// "basic_pitch" | "piano_hr" (default "basic_pitch"): what makes the Piano
// lane's notes. The Bass lane's are always Basic Pitch's. The status's
// engines list each engine with { id, label, version, licence, model_licence,
// stems, available, reason, model: built_in | downloaded | partial |
// not_downloaded, download_bytes }. A running M has phase "download" while
// piano_hr's model downloads, then "transcribe".

/** The lanes with a MIDI chip, in the order Settings lists them. */
export const MIDI_LANES = Object.freeze(["piano", "bass"]);

/** The Settings field that turns each lane's MIDI on or off. */
export const SETTING_FIELDS = Object.freeze({ piano: "transcribe_piano", bass: "transcribe_bass" });
export const DETAIL_FIELD = "transcribe_detail";
export const PIANO_ENGINE_FIELD = "transcribe_piano_engine";
/** Every Settings field the Transcription tab owns. */
export const TRANSCRIPTION_FIELDS = Object.freeze([SETTING_FIELDS.piano, SETTING_FIELDS.bass, DETAIL_FIELD, PIANO_ENGINE_FIELD]);

export const LANE_CHOICES = Object.freeze(["auto", "off"]);
export const DETAILS = Object.freeze(["balanced", "detailed", "clean"]);
export const PIANO_ENGINES = Object.freeze(["basic_pitch", "piano_hr"]);
export const DEFAULT_LANE_CHOICE = "auto";
export const DEFAULT_DETAIL = "balanced";
export const DEFAULT_PIANO_ENGINE = "basic_pitch";

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

/** The Keys engine setting from a settings answer. */
export function pianoEngineChoice(settings) {
  const v = isObj(settings) ? settings[PIANO_ENGINE_FIELD] : undefined;
  return PIANO_ENGINES.includes(v) ? v : DEFAULT_PIANO_ENGINE;
}

/** The engine that makes a lane's notes: the Keys engine for the piano, Basic Pitch for the bass. */
export function laneEngine(settings, lane) {
  return lane === "piano" ? pianoEngineChoice(settings) : DEFAULT_PIANO_ENGINE;
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
export function settingsPatch(field, value) {
  if (field === DETAIL_FIELD) return DETAILS.includes(value) ? { [field]: value } : null;
  if (field === PIANO_ENGINE_FIELD) return PIANO_ENGINES.includes(value) ? { [field]: value } : null;
  if (field === SETTING_FIELDS.piano || field === SETTING_FIELDS.bass) {
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

/**
 * What the Keys engine row says under its choice, as { key, vars }, or null:
 * that ByteDance Piano Transcription cannot run here, or the state of its model
 * (built in, downloaded, or downloaded the first time it is used).
 */
export function pianoEngineNote(status, settings, sizeText = (b) => `${Math.round(b / 1e6)} MB`) {
  if (pianoEngineChoice(settings) !== "piano_hr" || !isStatus(status)) return null;
  const e = engineOf(status, "piano_hr");
  if (!e || !engineReady(status, "piano_hr")) return { key: "settings.midi.pianoEngine.unavailable", vars: {} };
  const bytes = Number(e.download_bytes);
  const size = Number.isFinite(bytes) && bytes > 0 ? sizeText(bytes) : "?";
  if (e.model === "downloaded") return { key: "settings.midi.pianoEngine.model.downloaded", vars: { size } };
  return { key: "settings.midi.pianoEngine.model.notDownloaded", vars: { size } };
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
    phase: m.state === "running" && m.phase === "download" ? "download" : m.state === "running" ? "transcribe" : null,
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
  const engine = laneEngine(settings, lane);
  // An older server lists no second engine: the Keys engine is then Basic Pitch.
  if (!reasonKey && engine !== DEFAULT_PIANO_ENGINE && engineOf(status, engine) && !engineReady(status, engine)) {
    reasonKey = "settings.midi.pianoEngine.unavailable";
  }
  return { show: true, off: Boolean(reasonKey), reasonKey };
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
export function chipLabel(m, { name, off = false, reasonKey = "" }, t) {
  if (off) return reasonKey ? t(reasonKey) : t("laneMidi.tab.idle", { name });
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
export function rowView(m, { name, sending = false, otherRunning = false, removeArmed = false, maxMin = 20 }, t, plural = null) {
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
    const doing = m.phase === "download" ? t("laneMidi.downloadingModel") : t("laneMidi.running", { name });
    const text = [doing, hasFile ? t("laneMidi.keptWhileRunning") : ""].filter(Boolean).join(" ");
    status = { kind: "running", text };
  } else if (state === "error") {
    status = { kind: "error", text: t(errorKey(m.error_code), { minutes: maxMin, name }) };
  } else if (state === "cancelled") {
    status = { kind: "cancelled", text: t(hasFile ? "laneMidi.cancelledKept" : "laneMidi.cancelled") };
  } else if (hasFile) {
    status = { kind: "done", text: m.notes === null ? t("laneMidi.doneNoCount", { name }) : notesText(m.notes, name, t, plural) };
  } else {
    status = { kind: "idle", text: t("laneMidi.idle", { name }) };
  }
  if (otherRunning && !running) status = { kind: "other", text: t("laneMidi.otherRunning") };
  if (removeArmed && buttons.remove.show) status = { kind: "error", text: t("laneMidi.removeAgain") };
  return { buttons, run: running, pct: running ? midiPct(m) : 0, status, hint: hintLine(m, t) };
}

/** "312 notes from Bass." with the plural the language needs (i18n's plural()). */
export function notesText(count, name, t, plural = null) {
  return plural ? plural("laneMidi.notes", count, { name }) : t("laneMidi.notes.other", { count, name });
}

/** The line under the status for a file: the detail it was made with, and its tempo. */
export function hintLine(m, t) {
  if (!m?.has_file) return "";
  const parts = [];
  if (m.detail) parts.push(t("laneMidi.detailLine", { detail: t(detailNameKey(m.detail)) }));
  if (m.tempo_source === "song") parts.push(t("laneMidi.tempo.song"));
  else if (m.tempo_source === "default") parts.push(t("laneMidi.tempo.default"));
  return parts.join(" ");
}

// ─── Failures and refusals ───

/** The failure codes a run can end with, each with its sentence. */
export const ERROR_CODES = Object.freeze([
  "too_long", "disk", "io", "interrupted", "engine_unavailable", "worker_failed", "no_audio", "engine", "timeout", "stalled",
  "download",
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
