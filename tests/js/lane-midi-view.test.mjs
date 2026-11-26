// MIDI from every lane (Advanced mode), the pure part
// (static/js/laneMidiView.js): which lanes get a chip and when it shows, what
// the row offers and says in each state, what a refusal or a failure says,
// the .mid file's name and URL, the Settings > Transcription rules and the
// Export menu's Include MIDI. Pure, so it runs without a browser;
// tests/e2e/lane-midi.spec.mjs and transcription-settings.spec.mjs check the
// drawing.
//
// Run:  node tests/js/lane-midi-view.test.mjs

import { TRANSLATIONS } from "../../static/js/i18n.js";
import {
  DETAILS,
  DETAIL_FIELD,
  ERROR_CODES,
  DEFAULT_ENGINES,
  ENGINES,
  ENGINE_FIELDS,
  LANE_ENGINES,
  MIDI_LANES,
  REFUSAL_CODES,
  SETTING_FIELDS,
  anyRunning,
  chipLabel,
  chipMark,
  chipState,
  detailChoice,
  detailDescKey,
  detailNameKey,
  defaultEngine,
  engineAvailable,
  engineLine,
  engineName,
  engineNotes,
  engineNoteText,
  engineOf,
  engineReady,
  errorKey,
  hasFinishedMidi,
  hasTranscriptionFields,
  hintLine,
  laneChoice,
  laneEngine,
  laneEngines,
  laneHasMidi,
  laneNameKey,
  maxMinutes,
  midiFilename,
  midiOf,
  midiPct,
  midiUrl,
  notesText,
  needsDownload,
  parseExportMidi,
  progressText,
  refusalCode,
  refusalKey,
  refusalNote,
  rowNoteText,
  rowView,
  safeTitle,
  settingsPatch,
  songDone,
  transcriptionFields,
  unavailableKey,
  usesDetail,
  zipWantsMidi,
} from "../../static/js/laneMidiView.js";

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
// i18n's plural() for English: one, else other.
const plural = (key, count, vars = {}) => t(`${key}.${count === 1 ? "one" : "other"}`, { count, ...vars });

const STATUS_OK = {
  available: true, reason: null, stems: ["piano", "bass"], max_seconds: 1200, details: ["balanced", "detailed", "clean"],
  engines: [{ id: "basic_pitch", label: "Basic Pitch", version: "0.4.0", licence: "Apache-2.0" }],
};
const status = (over) => ({ ...STATUS_OK, ...over });
const M = (over) => ({ state: "done", pct: 100, notes: 312, engine: "basic_pitch", detail: "balanced", tempo_source: "song", at: "2026-11-25T10:00:00Z", error_code: null, has_file: true, ...over });
const job = (midi, over = {}) => ({ job_id: "abc123", status: "done", midi, ...over });

// ─── Lanes and settings ───

eq("the MIDI lanes are every stem, in Settings' order", [...MIDI_LANES], ["vocals", "drums", "bass", "guitar", "piano", "other"]);
check("every stem has a chip", MIDI_LANES.every(laneHasMidi));
check("the Lead and Backing split and a take have none", !["lead_backing", "take_1", ""].some(laneHasMidi));
eq("each lane is named by its stem", MIDI_LANES.map(laneNameKey), MIDI_LANES.map((l) => "stem." + l));

eq("a lane's setting defaults to auto", laneChoice({}, "piano"), "auto");
eq("no settings answer is auto", laneChoice(null, "bass"), "auto");
eq("off is read", laneChoice({ transcribe_bass: "off" }, "bass"), "off");
eq("an unknown value is auto", laneChoice({ transcribe_piano: "maybe" }, "piano"), "auto");
eq("the piano field is transcribe_piano", SETTING_FIELDS.piano, "transcribe_piano");
eq("the bass field is transcribe_bass", SETTING_FIELDS.bass, "transcribe_bass");
eq("each lane's field", MIDI_LANES.map((l) => SETTING_FIELDS[l]), MIDI_LANES.map((l) => "transcribe_" + l));
eq("each lane's engine field", MIDI_LANES.map((l) => ENGINE_FIELDS[l]), MIDI_LANES.map((l) => "transcribe_" + l + "_engine"));
eq("detail defaults to balanced", detailChoice({}), "balanced");
eq("detail reads clean", detailChoice({ transcribe_detail: "clean" }), "clean");
eq("an unknown detail is balanced", detailChoice({ transcribe_detail: "loud" }), "balanced");
eq("the details in order", [...DETAILS], ["balanced", "detailed", "clean"]);

check("an answer with a transcription field has them", hasTranscriptionFields({ transcribe_detail: "clean" }));
check("an answer without them has none", !hasTranscriptionFields({ demucs_device: "cpu" }));
check("null has none", !hasTranscriptionFields(null));
eq("only the transcription fields are kept", transcriptionFields({ transcribe_piano: "off", demucs_device: "cpu", transcribe_detail: "clean" }), { transcribe_piano: "off", transcribe_detail: "clean" });

eq("a lane patch posts its field", settingsPatch("transcribe_piano", "off"), { transcribe_piano: "off" });
eq("a detail patch posts its field", settingsPatch(DETAIL_FIELD, "detailed"), { transcribe_detail: "detailed" });
eq("a bad lane value is never posted", settingsPatch("transcribe_bass", "on"), null);
eq("a bad detail is never posted", settingsPatch("transcribe_detail", "max"), null);
eq("another field is never posted", settingsPatch("demucs_device", "cpu"), null);

eq("each detail has its name key", DETAILS.map(detailNameKey), ["settings.midi.detail.balanced", "settings.midi.detail.detailed", "settings.midi.detail.clean"]);
eq("and its sentence key", DETAILS.map(detailDescKey), ["settings.midi.detail.balancedDesc", "settings.midi.detail.detailedDesc", "settings.midi.detail.cleanDesc"]);
for (const d of DETAILS) check(`the ${d} sentence is one plain sentence`, /^[^.]+\.$/.test(en[detailDescKey(d)]), en[detailDescKey(d)]);

// ─── The engine ───

check("available is available", engineAvailable(STATUS_OK));
check("not available is not", !engineAvailable(status({ available: false, reason: "no_runtime" })));
eq("no reason when it runs", unavailableKey(STATUS_OK), "");
eq("an Intel Mac says so", t(unavailableKey(status({ available: false, reason: "unsupported_platform" }))), "Transcription is not available on this computer (Intel Mac).");
eq("no runtime: the engine could not start", t(unavailableKey(status({ available: false, reason: "no_runtime" }))), "The transcription engine could not start.");
eq("an engine error: the same sentence", t(unavailableKey(status({ available: false, reason: "engine_error" }))), "The transcription engine could not start.");
eq("an unknown reason: the same sentence", unavailableKey(status({ available: false, reason: "new_reason" })), "settings.midi.reason.engine");
eq("no answer has its own sentence", unavailableKey(null), "settings.midi.reason.unknown");
eq("an answer without available is no answer", unavailableKey({ engines: [] }), "settings.midi.reason.unknown");
eq("the engine line", engineLine(STATUS_OK, t), "Engine: Basic Pitch 0.4.0, licence Apache-2.0.");
eq("no engines, no line", engineLine(status({ engines: [] }), t), "");
eq("no status, no line", engineLine(null, t), "");
eq("an engine without a label uses its id", engineLine(status({ engines: [{ id: "basic_pitch", version: "1", licence: "MIT" }] }), t), "Engine: basic_pitch 1, licence MIT.");
eq("the longest song in minutes", maxMinutes(STATUS_OK), 20);
eq("an odd number of seconds rounds down", maxMinutes(status({ max_seconds: 1500 })), 25);
eq("no status is 20 minutes", maxMinutes(null), 20);

// ─── Each lane's engine ───

const HR = { id: "piano_hr", label: "ByteDance Piano Transcription", version: "0.0.6", licence: "MIT", model_licence: "CC BY 4.0", stems: ["piano"], available: true, reason: null, model: "not_downloaded", download_bytes: 171_966_578, disk_bytes: 171_966_578, device: "cpu", seconds_per_minute: null, uses_detail: true };
const BP = { id: "basic_pitch", label: "Basic Pitch", version: "0.4.0", licence: "Apache-2.0", model_licence: "Apache-2.0", stems: ["vocals", "bass", "guitar", "piano", "other"], available: true, reason: null, model: "built_in", download_bytes: null, disk_bytes: null, device: "cpu", seconds_per_minute: null, uses_detail: true };
const YM = { id: "ymt3", label: "YourMT3+", version: "5e66c1e", licence: "Apache-2.0", model_licence: "Apache-2.0", stems: ["drums", "bass", "guitar", "piano", "other"], available: true, reason: null, model: "not_downloaded", download_bytes: 561_544_628, disk_bytes: 188_412_704, device: "cpu", seconds_per_minute: { cuda: 12, cpu: 69 }, uses_detail: false };
const STEM_ENGINES = { vocals: ["basic_pitch"], drums: ["ymt3"], bass: ["basic_pitch", "ymt3"], guitar: ["basic_pitch", "ymt3"], piano: ["basic_pitch", "piano_hr", "ymt3"], other: ["basic_pitch", "ymt3"] };
const DEFAULTS = { vocals: "basic_pitch", drums: "ymt3", bass: "ymt3", guitar: "basic_pitch", piano: "ymt3", other: "ymt3" };
const ALL = status({ engines: [BP, HR, YM], stem_engines: STEM_ENGINES, default_engines: DEFAULTS });
const MB = (b) => `${Math.round(b / 1e6)} MB`;
const HR_SET = { transcribe_piano_engine: "piano_hr" };
eq("the engines in order", [...ENGINES], ["basic_pitch", "piano_hr", "ymt3"]);
eq("this release's lane engines match the server's", LANE_ENGINES, STEM_ENGINES);
eq("and its defaults", DEFAULT_ENGINES, DEFAULTS);
eq("a lane's engines come from the status", laneEngines(status({ stem_engines: { bass: ["ymt3", "mine_2"] } }), "bass"), ["ymt3", "mine_2"]);
eq("without a status, this release's", laneEngines(null, "drums"), ["ymt3"]);
eq("a status's bad ids are left out", laneEngines(status({ stem_engines: { bass: ["Bad Id", "ymt3"] } }), "bass"), ["ymt3"]);
eq("the default comes from the status", defaultEngine(status({ default_engines: { guitar: "ymt3" } }), "guitar"), "ymt3");
eq("a default the lane cannot take is this release's", defaultEngine(status({ default_engines: { vocals: "ymt3" } }), "vocals"), "basic_pitch");
eq("each lane's engine with nothing set", MIDI_LANES.map((l) => laneEngine({}, l, ALL)), MIDI_LANES.map((l) => DEFAULTS[l]));
eq("the piano's setting is read", laneEngine(HR_SET, "piano", ALL), "piano_hr");
eq("an engine the lane cannot take reads as its default", laneEngine({ transcribe_drums_engine: "basic_pitch" }, "drums", ALL), "ymt3");
eq("an engine patch posts its field", settingsPatch(ENGINE_FIELDS.piano, "piano_hr", ALL), HR_SET);
eq("an engine the lane cannot take is never posted", settingsPatch(ENGINE_FIELDS.bass, "piano_hr", ALL), null);
eq("nor one the status does not list", settingsPatch(ENGINE_FIELDS.vocals, "ymt3", ALL), null);
eq("an engine field is a transcription field", transcriptionFields({ ...HR_SET, port: 1 }), HR_SET);
eq("one engine by id", engineOf(ALL, "piano_hr").label, "ByteDance Piano Transcription");
eq("an engine's name", engineName(ALL, "ymt3", t), "YourMT3+");
eq("a name the page does not know is the status's label", engineName(status({ engines: [{ id: "mine", label: "My model" }] }), "mine", t), "My model");
check("an engine that can run is ready", engineReady(ALL, "ymt3"));
check("one that cannot is not", !engineReady(status({ engines: [BP, { ...YM, available: false }] }), "ymt3"));
check("nothing is ready when transcription cannot run", !engineReady(status({ available: false, engines: [BP, YM] }), "basic_pitch"));
check("an engine the status does not list is not ready", !engineReady(STATUS_OK, "ymt3"));
check("YourMT3+ has no Detail", !usesDetail(ALL, "ymt3") && usesDetail(ALL, "basic_pitch"));
check("an engine the status does not describe uses Detail", usesDetail(null, "basic_pitch"));
eq("every engine line, with the model's licence where it differs", engineLine(ALL, t),
  "Engine: Basic Pitch 0.4.0, licence Apache-2.0. Engine: ByteDance Piano Transcription 0.0.6, licence MIT, model licence CC BY 4.0. Engine: YourMT3+ 5e66c1e, licence Apache-2.0.");

const noteTexts = (st, settings, lane) => engineNotes(st, settings, lane, t, MB).map((n) => t(n.key, n.vars));
eq("Basic Pitch: nothing to say", noteTexts(ALL, {}, "vocals"), []);
eq("YourMT3+ before its first run on a computer without a GPU: its download and its speed", noteTexts(ALL, {}, "drums"), [
  "The first time, YourMT3+ downloads its model (562 MB) and then keeps it on this computer.",
  "On this computer it runs on the processor, which is slow: about 69 seconds of work per minute of song, so a 4 minute song takes about 5 minutes.",
]);
const onGpu = status({ ...ALL, engines: [BP, HR, { ...YM, device: "cuda", model: "downloaded" }] });
eq("downloaded, on a GPU: what it keeps and its speed", noteTexts(onGpu, {}, "piano"), [
  "The model of YourMT3+ is on this computer (188 MB).",
  "On this computer it runs on the graphics card: about 12 seconds of work per minute of song.",
]);
eq("a partial download reads as not downloaded", engineNotes(status({ ...ALL, engines: [BP, HR, { ...YM, model: "partial" }] }), {}, "bass", t, MB)[0].key, "settings.midi.engineModel.notDownloaded");
eq("piano_hr: its download, no speed measured", noteTexts(ALL, HR_SET, "piano"), ["The first time, ByteDance Piano Transcription downloads its model (172 MB) and then keeps it on this computer."]);
eq("cannot run here: says so, and nothing else", noteTexts(status({ ...ALL, engines: [BP, HR, { ...YM, available: false }] }), {}, "other"), ["YourMT3+ cannot run on this computer."]);
eq("no status yet: nothing", engineNotes(null, {}, "drums", t, MB), []);
eq("the notes as one line", engineNoteText(onGpu, {}, "piano", t, MB), "The model of YourMT3+ is on this computer (188 MB). On this computer it runs on the graphics card: about 12 seconds of work per minute of song.");
check("a first YourMT3+ run downloads", needsDownload(ALL, {}, "drums"));
check("Basic Pitch never does", !needsDownload(ALL, {}, "vocals"));
check("a downloaded model does not", !needsDownload(onGpu, {}, "piano"));
eq("the row warns of the download and the processor's speed before a first run", rowNoteText(ALL, {}, "drums", t, MB),
  "The first time, YourMT3+ downloads its model (562 MB) and then keeps it on this computer. On this computer it runs on the processor, which is slow: about 69 seconds of work per minute of song, so a 4 minute song takes about 5 minutes.");
eq("and says nothing once the model is here on a GPU", rowNoteText(onGpu, {}, "piano", t, MB), "");
eq("and nothing for Basic Pitch", rowNoteText(ALL, {}, "vocals", t, MB), "");

const ymOff = status({ ...ALL, engines: [BP, HR, { ...YM, available: false }] });
const offDrums = chipState({ lane: "drums", settings: {}, status: ymOff, job: job(null) });
eq("the drums chip is off when its engine cannot run", [offDrums.show, offDrums.off, offDrums.reasonKey, offDrums.engine], [true, true, "laneMidi.engineUnavailable", "ymt3"]);
eq("and its tooltip names the engine and the lane", chipLabel(null, { name: "Drums", off: true, reasonKey: offDrums.reasonKey, engine: offDrums.engine }, t),
  "YourMT3+ cannot run on this computer, so Drums cannot be transcribed with it.");
const vocalsOn = chipState({ lane: "vocals", settings: {}, status: ymOff, job: job(null) });
eq("the vocals chip, on Basic Pitch, is not", [vocalsOn.show, vocalsOn.off], [true, false]);
const unlisted = chipState({ lane: "drums", settings: {}, status: STATUS_OK, job: job(null) });
eq("an engine the server does not list: shown, off", [unlisted.show, unlisted.off], [true, true]);
const downloading = midiOf(job({ piano: M({ state: "running", pct: 40, phase: "download", has_file: false }) }), "piano");
eq("a running download is its phase", downloading.phase, "download");
eq("making the weights is its phase", midiOf(job({ piano: M({ state: "running", phase: "prepare", has_file: false }) }), "piano").phase, "prepare");
eq("a run without a phase transcribes", midiOf(job({ piano: M({ state: "running", has_file: false }) }), "piano").phase, "transcribe");
eq("a finished run has none", midiOf(job({ piano: M() }), "piano").phase, null);
const dlView = rowView(downloading, { name: "Piano" }, t, plural);
eq("the row says the model downloads, with its percent", [dlView.status.text, dlView.pct, dlView.run],
  ["Downloading the transcription model. This happens only the first time.", 40, true]);
const prepView = rowView(midiOf(job({ piano: M({ state: "running", phase: "prepare", has_file: false }) }), "piano"), { name: "Piano" }, t, plural);
eq("and that it is prepared", prepView.status.text, "Preparing the transcription model. This happens only the first time.");
eq("a failed download has its sentence", t(errorKey("download")), "The transcription model could not be downloaded. Check the internet connection and try again.");
eq("a failed preparation has its sentence", t(errorKey("prepare")), "The transcription model could not be prepared from its download. Try again.");
const first = rowView(null, { name: "Drums", firstRun: "The first time, YourMT3+ downloads its model (562 MB)." }, t, plural);
eq("an idle row says the first run's download after what it does", first.status.text, "Turns Drums into MIDI notes that a music program can open. The first time, YourMT3+ downloads its model (562 MB).");
eq("a YourMT3+ file's hint has no Detail", rowView(M({ engine: "ymt3" }), { name: "Piano", detail: false }, t, plural).hint, "Timed to the song's tempo.");

// ─── A lane's state ───

eq("no midi is null", midiOf(job(null), "piano"), null);
eq("a state the page does not know is null", midiOf(job({ piano: { state: "queued" } }), "piano"), null);
eq("the other lane has none", midiOf(job({ bass: M() }), "piano"), null);
const norm = midiOf(job({ bass: M({ notes: "40", tempo_source: "weird", detail: "loud", has_file: 1 }) }), "bass");
eq("notes are read as a number", norm.notes, 40);
eq("a strange tempo source is null", norm.tempo_source, null);
eq("a strange detail is empty", norm.detail, "");
eq("has_file is only true when true", norm.has_file, false);
eq("pct is clamped high", midiPct({ pct: 140 }), 100);
eq("pct is clamped low", midiPct({ pct: -5 }), 0);
eq("pct is rounded", midiPct({ pct: 41.6 }), 42);
eq("no pct is 0", midiPct({}), 0);
eq("the progress text", progressText({ pct: 42 }), "42%");
check("a running lane makes the song running", anyRunning(job({ piano: M({ state: "running", pct: 10 }) })));
check("no running lane", !anyRunning(job({ piano: M(), bass: M({ state: "error", has_file: false }) })));
check("a file makes the song have MIDI", hasFinishedMidi(job({ bass: M() })));
check("a running first run has no file yet", !hasFinishedMidi(job({ bass: M({ state: "running", has_file: false }) })));
check("a re-run keeps the earlier file", hasFinishedMidi(job({ bass: M({ state: "running", has_file: true }) })));
check("no midi has none", !hasFinishedMidi(job(null)));
check("a done song is done", songDone(job(null)));
check("a running song is not", !songDone(job(null, { status: "running" })));
check("no job is not done", !songDone(null));

// ─── When the chip shows ───

const chip = (over = {}) => chipState({ lane: "bass", settings: {}, status: STATUS_OK, job: job(null), ...over });
eq("auto, done song, engine here: shown", chip({ status: ALL }), { show: true, off: false, reasonKey: "", engine: "ymt3" });
eq("off in Settings: hidden", chip({ settings: { transcribe_bass: "off" } }).show, false);
eq("the other lane's off does not hide it", chip({ settings: { transcribe_piano: "off" } }).show, true);
eq("a song still separating: hidden", chip({ job: job(null, { status: "running" }) }).show, false);
eq("no song read yet: hidden", chip({ job: null }).show, false);
eq("no transcription on the server: hidden", chip({ status: null }).show, false);
eq("a lead and backing split never has one", chip({ lane: "lead_backing" }).show, false);
eq("the engine cannot run: shown, off, with the reason",
  chip({ status: status({ available: false, reason: "unsupported_platform" }) }),
  { show: true, off: true, reasonKey: "settings.midi.reason.unsupported_platform", engine: "ymt3" });

eq("no state, no mark", chipMark(null), "");
eq("running: the percent", chipMark(M({ state: "running", has_file: false })), "run");
eq("a file: a dot", chipMark(M()), "done");
eq("a failed run: a red dot", chipMark(M({ state: "error", has_file: true })), "error");
eq("stopped with no file: no mark", chipMark(M({ state: "cancelled", has_file: false })), "");
eq("stopped with the earlier file: a dot", chipMark(M({ state: "cancelled", has_file: true })), "done");

eq("the idle chip's name", chipLabel(null, { name: "Bass" }, t), "MIDI from Bass");
eq("the running chip's name", chipLabel(M({ state: "running", pct: 42 }), { name: "Bass" }, t), "MIDI from Bass: 42%");
eq("the done chip's name", chipLabel(M(), { name: "Piano" }, t), "MIDI from Piano: a file is ready");
eq("the failed chip's name", chipLabel(M({ state: "error" }), { name: "Piano" }, t), "MIDI from Piano: the last run did not work");
eq("an off chip says why", chipLabel(null, { name: "Bass", off: true, reasonKey: "settings.midi.reason.unsupported_platform" }, t), "Transcription is not available on this computer (Intel Mac).");

// ─── The row in each state ───

const view = (m, over = {}) => rowView(m, { name: "Bass", ...over }, t, plural);
const shown = (v) => Object.entries(v.buttons).filter(([, b]) => b.show).map(([k]) => k);

let v = view(null);
eq("idle offers Transcribe only", shown(v), ["transcribe"]);
eq("idle says what it does", v.status, { kind: "idle", text: "Turns Bass into MIDI notes that a music program can open." });
eq("with nothing to warn of, that alone", view(null, { firstRun: "" }).status.text, "Turns Bass into MIDI notes that a music program can open.");
eq("idle has no progress", v.run, false);
eq("idle has no hint", v.hint, "");

v = view(M({ state: "running", pct: 42, has_file: false, notes: null }));
eq("running offers Cancel", shown(v), ["cancel"]);
eq("running shows the progress", [v.run, v.pct], [true, 42]);
eq("running says so", v.status, { kind: "running", text: "Transcribing Bass on this computer..." });

v = view(M({ state: "running", pct: 10, has_file: true }));
eq("a re-run keeps Download for the earlier file", shown(v), ["cancel", "download"]);
eq("and says the earlier file stays", v.status.text, "Transcribing Bass on this computer... The earlier MIDI file stays until the new one is done.");

v = view(M());
eq("done offers Download, Re-run and Remove", shown(v), ["rerun", "download", "remove"]);
eq("done says the note count", v.status, { kind: "done", text: "312 notes from Bass." });
eq("one note is singular", view(M({ notes: 1 })).status.text, "1 note from Bass.");
eq("an unknown count still says it is ready", view(M({ notes: null })).status.text, "The MIDI file of Bass is ready.");
eq("done's hint: the detail and the song's tempo", v.hint, "Detail: Balanced. Timed to the song's tempo.");
eq("a default tempo says so", view(M({ tempo_source: "default", detail: "clean" })).hint, "Detail: Clean. The song's tempo is not known, so the file uses a standard tempo.");
eq("no tempo source, the detail alone", view(M({ tempo_source: null, detail: "detailed" })).hint, "Detail: Detailed.");

v = view(M({ state: "error", error_code: "disk", has_file: false }));
eq("an error offers Retry", shown(v), ["retry"]);
eq("an error says its reason", v.status, { kind: "error", text: "There is not enough free disk space to save the MIDI file." });
v = view(M({ state: "error", error_code: "worker_failed", has_file: true }));
eq("an error with the earlier file keeps Download and Remove", shown(v), ["retry", "download", "remove"]);
eq("too long names the limit", view(M({ state: "error", error_code: "too_long", has_file: false }), { maxMin: 20 }).status.text, "This song is too long to transcribe. The most is 20 minutes.");
eq("an unknown error has the general sentence", view(M({ state: "error", error_code: "gremlins", has_file: false })).status.text, "The transcription did not work. Try again.");

v = view(M({ state: "cancelled", has_file: false, notes: null }));
eq("stopped with no file offers Transcribe", shown(v), ["transcribe"]);
eq("and says nothing was made", v.status, { kind: "cancelled", text: "Stopped. No MIDI file was made." });
v = view(M({ state: "cancelled", has_file: true }));
eq("stopped with the earlier file offers Re-run, Download, Remove", shown(v), ["rerun", "download", "remove"]);
eq("and says it is kept", v.status.text, "Stopped. The earlier MIDI file is kept.");

v = view(null, { sending: true });
eq("while a request goes, Transcribe is off", v.buttons.transcribe.disabled, true);
v = view(M(), { otherRunning: true });
eq("another job on the song turns Re-run off", v.buttons.rerun.disabled, true);
eq("and Remove", v.buttons.remove.disabled, true);
eq("Download stays on", v.buttons.download.disabled, false);
eq("and the line says why", v.status, { kind: "other", text: "Another job is running on this song. Try again when it finishes." });
v = view(M(), { removeArmed: true });
eq("an armed Remove asks for the second press", v.status, { kind: "error", text: "Press again to delete the MIDI file" });
v = view(M({ state: "running", pct: 5, has_file: true }), { sending: true });
eq("a Cancel on its way turns Cancel off", v.buttons.cancel.disabled, true);
eq("a hidden button is never reported disabled", view(null, { sending: true }).buttons.cancel, { show: false, disabled: false });
eq("notes without plural() use other", notesText(3, "Piano", t), "3 notes from Piano.");

// ─── Failures and refusals ───

for (const code of ERROR_CODES) check(`error ${code} has its sentence`, typeof en[errorKey(code)] === "string" && errorKey(code) !== "laneMidi.error.unknown");
eq("an unknown error code is the general one", errorKey("nope"), "laneMidi.error.unknown");
eq("the backend's run codes are all known", ["no_audio", "engine", "timeout", "stalled"].every((c) => ERROR_CODES.includes(c)), true);
eq("no audio names the lane", view(M({ state: "error", error_code: "no_audio", has_file: false })).status.text, "The audio of Bass is missing or cannot be read, so it could not be transcribed.");
eq("an engine failure", view(M({ state: "error", error_code: "engine", has_file: false })).status.text, "The transcription engine failed during the run. Try again.");
eq("a timeout", view(M({ state: "error", error_code: "timeout", has_file: false })).status.text, "The transcription took longer than the time limit and was stopped. Try again.");
eq("a stalled run", view(M({ state: "error", error_code: "stalled", has_file: false })).status.text, "The transcription stopped making progress and was stopped. Try again.");
eq("a song-wide job_busy reads as busy", refusalKey("job_busy"), "laneMidi.refusal.busy");
eq("a refusal's own code", refusalCode(409, { detail: "x", code: "busy" }), "busy");
eq("a bare 404 is the song gone", refusalCode(404, null), "job_not_found");
eq("no answer at all is offline", refusalCode(0, null), "offline");
eq("anything else is unknown", refusalCode(500, { detail: "boom" }), "unknown");
for (const code of REFUSAL_CODES) check(`refusal ${code} has its sentence`, typeof en[refusalKey(code)] === "string" && refusalKey(code) !== "laneMidi.refusal.unknown");
eq("busy says what the brief asked for", t(refusalKey("busy")), "Another job is running on this song. Try again when it finishes.");
eq("too long reuses the run's sentence", refusalKey("too_long"), "laneMidi.error.too_long");
eq("engine unavailable reuses the run's sentence", refusalKey("engine_unavailable"), "laneMidi.error.engine_unavailable");
eq("offline says StemDeck stopped answering", refusalKey("offline"), "laneMidi.lost");
eq("an unknown refusal is the general one", refusalKey("weird"), "laneMidi.refusal.unknown");
eq("a refusal note names the lane", refusalNote(409, { code: "no_stem" }, { name: "Piano" }, t), { kind: "error", text: "This song has no Piano stem to transcribe.", code: "no_stem" });
eq("a too-long refusal names the limit", refusalNote(413, { code: "too_long" }, { name: "Bass", maxMin: 25 }, t).text, "This song is too long to transcribe. The most is 25 minutes.");
eq("disabled points at Settings", t(refusalKey("disabled")), "Transcription of this lane is off in Settings, Transcription.");

// ─── The file ───

eq("the safe title", safeTitle("My Song (Live) / 2024!"), "My_Song_Live_2024");
eq("the file name", midiFilename("My Song", "bass"), "My_Song_bass.mid");
eq("no title, the lane alone", midiFilename("", "piano"), "piano.mid");
eq("a title of symbols, the lane alone", midiFilename("???", "piano"), "piano.mid");
check("a long title is cut to 80", midiFilename("a".repeat(200), "bass") === `${"a".repeat(80)}_bass.mid`);
eq("the URL carries the run's time", midiUrl("abc123", "bass", M()), "/api/jobs/abc123/stems/bass.mid?v=2026-11-25T10%3A00%3A00Z");
eq("no time, the plain URL", midiUrl("abc123", "piano", M({ at: "" })), "/api/jobs/abc123/stems/piano.mid");
eq("the job id is encoded", midiUrl("a/b", "bass", null), "/api/jobs/a%2Fb/stems/bass.mid");

// ─── Export: Include MIDI ───

check("stored 1 is on", parseExportMidi("1"));
check("stored 0 is off", !parseExportMidi("0"));
check("nothing stored is off (the default)", !parseExportMidi(null));
check("anything else is off", !parseExportMidi("true"));
check("ticked and a file: the zip asks for MIDI", zipWantsMidi(true, job({ bass: M() })));
check("ticked and no file: it does not", !zipWantsMidi(true, job({ bass: M({ state: "running", has_file: false }) })));
check("not ticked: it does not", !zipWantsMidi(false, job({ bass: M() })));

// ─── The strings ───

const KEYS = Object.keys(en).filter((k) => k.startsWith("laneMidi.") || k.startsWith("settings.midi.") || k.startsWith("help.midi")
  || k.startsWith("help.set.midi") || k.startsWith("export.includeMidi") || k === "help.exportMidi"
  || k === "settings.tab.transcription" || k === "settings.desc.transcription" || k === "settings.group.midi");
check("the English table has the MIDI strings", KEYS.length >= 80, String(KEYS.length));
check("no em dash in the MIDI English", KEYS.every((k) => !en[k].includes("\u2014")));
check("no \"we\" in the MIDI English", KEYS.every((k) => !/\bwe\b|\bour\b/i.test(en[k])));
const FULL = ["pl", "ja", "zh-Hans", "de", "fr", "es", "pt", "id", "ko"];
for (const lang of FULL) {
  const table = TRANSLATIONS[lang];
  const missing = KEYS.filter((k) => {
    const m = /^(.*)\.(one|few|many|other)$/.exec(k);
    return m ? !Object.keys(table).some((x) => x.startsWith(`${m[1]}.`)) : !(k in table);
  });
  check(`${lang} has every MIDI string`, missing.length === 0, missing.join(", "));
  check(`${lang} has no em dash in them`, KEYS.every((k) => !(table[k] || "").includes("\u2014")));
}
check("pt-PT overrides only keys pt has", Object.keys(TRANSLATIONS["pt-PT"]).filter((k) => KEYS.includes(k)).every((k) => k in TRANSLATIONS.pt));

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
