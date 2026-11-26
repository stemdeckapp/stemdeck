// Settings > Transcription: MIDI from every lane. For each of the six lanes a
// row with Auto or Off (the lane's MIDI button) and the engine that writes
// its notes, listing only the engines that can take that lane; under it what
// the engine needs said before it runs (it cannot run here, or the model it
// downloads the first time and its size, and how long it takes on this
// computer). Then the Detail of the notes with one sentence per choice (and
// which engines it does not change), the fixed notice (it runs on this
// computer and sends nothing; the person is responsible for the rights), and
// each engine's name, version and licence. When transcription cannot run
// here the reason shows in plain words and the selects are off. The rules are
// in the pure laneMidiView.js; this file draws them and saves a choice.
//
// Server: GET /api/settings (transcribe_<lane>, transcribe_<lane>_engine,
// transcribe_detail), POST /api/settings with one of them, and
// GET /api/transcribe/status. Every answer this tab saves is announced on the
// overlay as "stemdeck:settings-applied" with only these fields, so the lanes'
// MIDI chips follow at once (laneMidiRows.js) and no other tab repaints.

import { t, getLanguage, onLanguageChange } from "./i18n.js";
import { SETTINGS_APPLIED_EVENT } from "./separationSettings.js";
import { formatSize, listText } from "./myModelsView.js";
import {
  DETAIL_FIELD, ENGINE_FIELDS, MIDI_LANES, SETTING_FIELDS, detailChoice, engineAvailable, engineLine, engineName,
  engineNoteText, hasTranscriptionFields, isStatus, laneChoice, laneEngine, laneEngines, laneNameKey, settingsPatch,
  transcriptionFields, unavailableKey, usesDetail,
} from "./laneMidiView.js";

export async function wireTranscriptionSettings(overlay) {
  const pane = overlay.querySelector('.settings-pane[data-pane="transcription"]');
  if (!pane) return;
  const selects = [...pane.querySelectorAll("select[data-field]")];
  const engineEl = pane.querySelector(".transcription-engine");
  const whyEl = pane.querySelector(".transcription-unavailable");
  const msg = pane.querySelector(".transcription-msg");
  const detailLines = [...pane.querySelectorAll(".transcription-detail-desc[data-detail]")];
  const detailNotFor = pane.querySelector(".transcription-detail-not-for");

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

  const laneOfEngineField = (field) => MIDI_LANES.find((l) => ENGINE_FIELDS[l] === field);
  const laneOfSettingField = (field) => MIDI_LANES.find((l) => SETTING_FIELDS[l] === field);

  // Each lane's own words: its sentence, the engine select's name, and the
  // engines it lists (only those that can take the lane).
  const paintLanes = () => {
    for (const lane of MIDI_LANES) {
      const name = t(laneNameKey(lane));
      const desc = pane.querySelector(`.transcription-lane-desc[data-lane="${lane}"]`);
      if (desc) desc.textContent = t("settings.midi.laneDesc", { name });
      const engineSel = pane.querySelector(`select[data-field="${ENGINE_FIELDS[lane]}"]`);
      if (!engineSel) continue;
      engineSel.setAttribute("aria-label", t("settings.midi.engineAria", { name }));
      const allowed = laneEngines(status, lane);
      for (const opt of [...engineSel.options]) {
        const ok = allowed.includes(opt.value);
        opt.hidden = !ok;
        opt.disabled = !ok;
        opt.textContent = engineName(status, opt.value, t);
      }
      // An engine the status names that this page has no option for (one the
      // person added): an option of its own, so the choice can show it.
      for (const id of allowed) {
        if ([...engineSel.options].some((o) => o.value === id)) continue;
        const opt = document.createElement("option");
        opt.value = id;
        opt.textContent = engineName(status, id, t);
        engineSel.append(opt);
      }
    }
  };

  const paint = () => {
    for (const sel of selects) {
      const field = sel.dataset.field;
      if (field === DETAIL_FIELD) sel.value = detailChoice(data);
      else if (laneOfSettingField(field)) sel.value = laneChoice(data, laneOfSettingField(field));
      else if (laneOfEngineField(field)) sel.value = laneEngine(data, laneOfEngineField(field), status);
    }
    const detail = detailChoice(data);
    for (const line of detailLines) line.classList.toggle("is-current", line.dataset.detail === detail);
    paintEngineNotes();
  };

  // Under each lane's engine: what it needs said before it runs. Nothing
  // until the status is read.
  function paintEngineNotes() {
    const locale = getLanguage();
    for (const lane of MIDI_LANES) {
      const el = pane.querySelector(`.transcription-engine-note[data-lane="${lane}"]`);
      if (!el) continue;
      const text = statusRead ? engineNoteText(status, data, lane, t, (b) => formatSize(b, t, locale)) : "";
      el.textContent = text;
      el.hidden = !text;
    }
    if (detailNotFor) {
      // The engines in use whose notes Detail does not change.
      const ids = [...new Set(MIDI_LANES.filter((l) => laneChoice(data, l) !== "off")
        .map((l) => laneEngine(data, l, status)))].filter((id) => !usesDetail(status, id));
      const text = statusRead && ids.length
        ? t("settings.midi.detail.notFor", { engines: listText(ids.map((id) => engineName(status, id, t)), locale) })
        : "";
      detailNotFor.textContent = text;
      detailNotFor.hidden = !text;
    }
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
    paintLanes();
    paint();
  };

  const take = (answer) => {
    if (!hasTranscriptionFields(answer)) return;
    data = { ...data, ...transcriptionFields(answer) };
    paint();
  };

  const save = async (sel) => {
    const patch = settingsPatch(sel.dataset.field, sel.value, status);
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
  // The sentences painted here carry a lane's or an engine's name.
  onLanguageChange(() => paintStatus());

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
