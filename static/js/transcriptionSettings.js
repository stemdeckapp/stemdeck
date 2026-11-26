// Settings > Transcription: MIDI from the Piano and Bass lanes. A row for
// Keys (Auto or Off), the Keys engine (Basic Pitch, or ByteDance Piano
// Transcription with the state of its model: downloaded, or downloaded the
// first time it is used), one for Bass (Auto, which uses Basic Pitch, or Off),
// the Detail of the notes with one sentence per choice, the fixed notice (it
// runs on this computer and sends nothing; the person is responsible for the
// rights), and each engine's name, version and licence. When transcription
// cannot run here the reason shows in plain words and the selects are off.
// The rules are in the pure laneMidiView.js; this file draws them and saves a
// choice.
//
// Server: GET /api/settings (transcribe_piano, transcribe_bass,
// transcribe_detail, transcribe_piano_engine), POST /api/settings with one of
// them, and
// GET /api/transcribe/status. Every answer this tab saves is announced on the
// overlay as "stemdeck:settings-applied" with only these fields, so the lanes'
// MIDI chips follow at once (laneMidiRows.js) and no other tab repaints.

import { t, getLanguage } from "./i18n.js";
import { SETTINGS_APPLIED_EVENT } from "./separationSettings.js";
import { formatSize } from "./myModelsView.js";
import {
  DETAIL_FIELD, PIANO_ENGINE_FIELD, SETTING_FIELDS, detailChoice, engineAvailable, engineLine,
  hasTranscriptionFields, isStatus, laneChoice, pianoEngineChoice, pianoEngineNote, settingsPatch,
  transcriptionFields, unavailableKey,
} from "./laneMidiView.js";

export async function wireTranscriptionSettings(overlay) {
  const pane = overlay.querySelector('.settings-pane[data-pane="transcription"]');
  if (!pane) return;
  const selects = [...pane.querySelectorAll("select[data-field]")];
  const engineEl = pane.querySelector(".transcription-engine");
  const whyEl = pane.querySelector(".transcription-unavailable");
  const engineNoteEl = pane.querySelector(".transcription-piano-engine-note");
  const msg = pane.querySelector(".transcription-msg");
  const detailLines = [...pane.querySelectorAll(".transcription-detail-desc[data-detail]")];

  let data = {};
  let status = null;
  let statusRead = false;
  // Only the answer to the latest save is painted: a slower earlier one must
  // not put back a choice that was changed again since.
  let latest = 0;

  const say = (key, isError = false) => {
    if (!msg) return;
    msg.textContent = key ? t(key) : "";
    msg.classList.toggle("error", Boolean(isError));
  };

  const paint = () => {
    for (const sel of selects) {
      const field = sel.dataset.field;
      if (field === DETAIL_FIELD) sel.value = detailChoice(data);
      else if (field === SETTING_FIELDS.piano) sel.value = laneChoice(data, "piano");
      else if (field === SETTING_FIELDS.bass) sel.value = laneChoice(data, "bass");
      else if (field === PIANO_ENGINE_FIELD) sel.value = pianoEngineChoice(data);
    }
    const detail = detailChoice(data);
    for (const line of detailLines) line.classList.toggle("is-current", line.dataset.detail === detail);
    paintEngineNote();
  };

  // Under the Keys engine: its model's state, or that it cannot run here.
  // Nothing until the status is read, and nothing for Basic Pitch.
  function paintEngineNote() {
    if (!engineNoteEl) return;
    const locale = getLanguage();
    const note = statusRead ? pianoEngineNote(status, data, (b) => formatSize(b, t, locale)) : null;
    engineNoteEl.textContent = note ? t(note.key, note.vars) : "";
    engineNoteEl.hidden = !note;
  }

  const paintStatus = () => {
    if (engineEl) {
      const text = engineLine(status, t);
      engineEl.textContent = text;
      engineEl.hidden = !text;
    }
    // Unknown until the read ends: nothing is said, nothing is turned off.
    const key = statusRead ? unavailableKey(status) : "";
    if (whyEl) {
      whyEl.textContent = key ? t(key) : "";
      whyEl.hidden = !key;
    }
    const off = isStatus(status) && !engineAvailable(status);
    for (const sel of selects) sel.disabled = off;
    paintEngineNote();
  };

  const take = (answer) => {
    if (!hasTranscriptionFields(answer)) return;
    data = { ...data, ...transcriptionFields(answer) };
    paint();
  };

  const save = async (sel) => {
    const patch = settingsPatch(sel.dataset.field, sel.value);
    if (!patch) {
      paint();
      return;
    }
    const seq = ++latest;
    say(null);
    try {
      const r = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const fields = transcriptionFields(await r.json());
      if (seq !== latest) return;
      take(fields);
      // The lanes' MIDI chips follow (laneMidiRows.js listens on the document).
      overlay.dispatchEvent(new CustomEvent(SETTINGS_APPLIED_EVENT, { detail: fields }));
    } catch (e) {
      console.warn("[transcription] the setting was not saved:", e);
      if (seq !== latest) return;
      // Not saved: show what is still in force, and say so.
      paint();
      say("settings.midi.saveFailed", true);
    }
  };

  for (const sel of selects) sel.addEventListener("change", () => save(sel));

  // Any settings answer the dialog applied (its own first read included).
  overlay.addEventListener(SETTINGS_APPLIED_EVENT, (e) => take(e.detail));

  paint();
  paintStatus();
  const [settingsRes, statusRes] = await Promise.allSettled([
    fetch("/api/settings", { cache: "no-store" }).then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))),
    fetch("/api/transcribe/status", { cache: "no-store" }).then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))),
  ]);
  if (settingsRes.status === "fulfilled") take(settingsRes.value);
  else console.warn("[transcription] reading the settings failed:", settingsRes.reason);
  if (statusRes.status === "fulfilled" && isStatus(statusRes.value)) status = statusRes.value;
  else console.warn("[transcription] reading whether transcription can run failed:", statusRes.reason ?? statusRes.value);
  statusRead = true;
  paintStatus();
}
