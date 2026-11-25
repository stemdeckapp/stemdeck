// Seeing, hearing and downloading a Piano or Bass lane's MIDI, the pure part
// (static/js/midiNotesView.js): the notes answer, the piano roll's
// rectangles, the source switch, the download choices and the synth voice.
// tests/e2e/lane-midi.spec.mjs checks the drawing and the sound being added.
//
// Run:  node tests/js/midi-notes-view.test.mjs

import { TRANSLATIONS } from "../../static/js/i18n.js";
import {
  ROLL_MIN_SPAN,
  ROLL_NOTE_H,
  ROLL_PAD,
  SOURCES,
  audioFormat,
  bothZipName,
  bothZipUrl,
  cleanNotes,
  downloadChoices,
  engineSource,
  needsMidiSound,
  normalSource,
  pitchHz,
  renderSeconds,
  rollGeometry,
  rollRects,
  soundKey,
  sourceSwitch,
  velocityGain,
  voiceFor,
} from "../../static/js/midiNotesView.js";

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
const near = (a, b, tol = 1e-9) => Math.abs(a - b) <= tol;

// ─── The notes answer ───

eq("usable notes are kept, sorted by start", cleanNotes({ notes: [[2, 3, 50, 90], [0.5, 1, 45, 60]] }), [[0.5, 1, 45, 60], [2, 3, 50, 90]]);
eq("a bad answer is no notes", cleanNotes(null), []);
eq("notes without notes", cleanNotes({ notes: "x" }), []);
eq("broken entries are dropped", cleanNotes({ notes: [[1, 0.5, 60, 1], [0, 1, 128, 1], [0, 1, 60.5, 1], [-1, 1, 60, 1], [0, 1, 60], "x"] }), []);
eq("velocity is clamped and a missing one is 100", cleanNotes({ notes: [[0, 1, 60, 300], [1, 2, 61, null]] }), [[0, 1, 60, 127], [1, 2, 61, 100]]);

// ─── The piano roll ───

check("no notes, no roll", rollGeometry([], 10) === null);
check("no length, no roll", rollGeometry([[0, 1, 60, 100]], 0) === null);
let g = rollGeometry([[1, 2, 40, 127], [3, 3.5, 60, 0]], 10);
eq("the viewBox is seconds wide", g.width, 10);
eq("and the lane's range, padded, rows tall", [g.lo, g.hi, g.height], [40 - ROLL_PAD, 60 + ROLL_PAD, 60 - 40 + 2 * ROLL_PAD + 1]);
eq("a note's x and width are its start and length", [g.rects[0].x, g.rects[0].w], [1, 1]);
check("the highest note is at the top", g.rects[1].y < g.rects[0].y);
check("the top note sits ROLL_PAD rows down", near(g.rects[1].y, ROLL_PAD + (1 - ROLL_NOTE_H) / 2));
check("the lowest note sits ROLL_PAD rows above the bottom", near(g.rects[0].y + ROLL_NOTE_H + (1 - ROLL_NOTE_H) / 2, g.height - ROLL_PAD));
eq("velocity is opacity, 0.35 to 1", [g.rects[0].o, g.rects[1].o], [1, 0.35]);
g = rollGeometry([[0, 1, 60, 64]], 5);
eq("one pitch still spans the minimum range", g.height, ROLL_MIN_SPAN);
check("centred in it", g.lo <= 60 - ROLL_PAD && g.hi >= 60 + ROLL_PAD);
g = rollGeometry([[4, 7, 60, 64], [6, 8, 62, 64]], 5);
eq("a note past the end is cut at the end", g.rects.map((r) => [r.x, r.w]), [[4, 1]]);
// One pitch, 60: rows 55 to 66 (the minimum span around it), so its row
// starts 6 rows down, plus the gap above the note.
eq("the rects as SVG", rollRects(rollGeometry([[1, 1.5, 60, 127]], 4)), '<rect x="1" y="6.1" width="0.5" height="0.8" fill-opacity="1"></rect>');
eq("no geometry, no rects", rollRects(null), "");

// ─── The source switch ───

eq("the sources", [...SOURCES], ["stem", "midi", "both"]);
eq("an unknown source is the stem", normalSource("loud"), "stem");
eq("the engine calls the MIDI its alt", ["stem", "midi", "both", "x"].map(engineSource), ["stem", "alt", "both", "stem"]);
eq("the MIDI sound is needed for MIDI and Both", SOURCES.map(needsMidiSound), [false, true, true]);
eq("no file: no switch", sourceSwitch({ hasFile: false, engineSupports: true }), { show: false, disabled: false, reasonKey: "" });
eq("a file and the full engine: on", sourceSwitch({ hasFile: true, engineSupports: true }), { show: true, disabled: false, reasonKey: "" });
eq("while it is prepared: off", sourceSwitch({ hasFile: true, engineSupports: true, preparing: true }).disabled, true);
eq("the streaming engine: off, saying why", sourceSwitch({ hasFile: true, engineSupports: false }), { show: true, disabled: true, reasonKey: "laneMidi.source.unsupported" });

// ─── The download choices ───

eq("no MIDI file: the plain download", downloadChoices({ hasMidi: false }), []);
eq("with one: Audio, MIDI, Audio and MIDI", downloadChoices({ hasMidi: true }).map((c) => c.id), ["audio", "midi", "both"]);
const en = TRANSLATIONS.en;
for (const c of downloadChoices({ hasMidi: true })) {
  check(`${c.id} has its label and title`, typeof en[c.key] === "string" && typeof en[`${c.key}Title`] === "string");
}
eq("the format of a WAV stem", audioFormat("/api/jobs/a/stems/bass.wav"), "wav");
eq("of an MP3 stem with a query", audioFormat("/api/jobs/a/stems/bass.mp3?v=2"), "mp3");
eq("an unknown one is WAV", audioFormat("#"), "wav");
eq("the Audio and MIDI zip asks for the one stem and its MIDI", bothZipUrl("abc", "bass", "/api/jobs/abc/stems/bass.mp3"), "/api/jobs/abc/stems/all.zip?format=mp3&stems=bass&midi=1");
eq("its name", bothZipName("My_Song", "piano"), "My_Song_piano_audio_midi.zip");
eq("its name without a title", bothZipName("", "bass"), "bass_audio_midi.zip");

// ─── The synth voice ───

const keys = voiceFor("piano");
const bass = voiceFor("bass");
check("Keys is sine partials, no filter", keys.partials[0].type === "sine" && keys.lowpass === 0);
check("Bass is a sine and a quieter saw through a low-pass", bass.partials.map((p) => p.type).join() === "sine,sawtooth" && bass.partials[1].gain < bass.partials[0].gain && bass.lowpass > 0 && bass.lowpass <= 1200);
check("both start fast (under 10 ms)", keys.attack < 0.01 && bass.attack < 0.01);
check("both release short (under 0.3 s)", keys.release <= 0.3 && bass.release <= 0.3);
check("Keys sustains lower than Bass (a piano decays)", keys.sustain < bass.sustain);
check("an unknown lane gets the Keys voice", JSON.stringify(voiceFor("other")) === JSON.stringify(keys));
check("A4 is 440 Hz", near(pitchHz(69), 440));
check("an octave doubles", near(pitchHz(81), 880));
eq("velocity 127 is full", velocityGain(127), 1);
eq("velocity 0 is silent", velocityGain(0), 0);
check("a soft note stays audible (curved)", velocityGain(32) > 32 / 127);
eq("the sound lasts the song", renderSeconds([[0, 1, 60, 100]], 10, keys), 10);
check("or the last release past it", near(renderSeconds([[9, 12, 60, 100]], 10, keys), 12 + keys.release + 0.05));
eq("a rendering is reused per song, lane, file and rate", soundKey("abc", "bass", "2026", 48000), "abc|bass|2026|48000");
check("another file is another rendering", soundKey("abc", "bass", "a", 48000) !== soundKey("abc", "bass", "b", 48000));

// ─── The strings ───

const KEYS = [
  "laneMidi.roll.aria", "laneMidi.source.label", "laneMidi.source.aria", "laneMidi.source.stem", "laneMidi.source.midi",
  "laneMidi.source.both", "laneMidi.source.stemTitle", "laneMidi.source.midiTitle", "laneMidi.source.bothTitle",
  "laneMidi.source.unsupported", "laneMidi.source.preparing", "laneMidi.source.failed", "laneMidi.dl.aria",
  "help.midiRoll", "help.midiRollTitle", "help.midiSource", "help.downloadMidi", "help.dlAudio", "help.dlMidi", "help.dlBoth",
];
for (const lang of ["en", "pl", "ja", "zh-Hans", "de", "fr", "es", "pt", "id", "ko"]) {
  const missing = KEYS.filter((k) => typeof TRANSLATIONS[lang][k] !== "string");
  check(`${lang} has every new MIDI string`, missing.length === 0, missing.join(", "));
}
check("the text chip's word is gone", !("laneMidi.chip" in en));
check("no em dash in the new English", KEYS.every((k) => !en[k].includes("\u2014")));

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
