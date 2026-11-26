// A lane's MIDI view, which picture is in front for each Sound: the mapping
// from the row's data-midi-source to the waveform's and the notes' strength
// and order, read from static/css/waves.css, where it lives (one attribute
// set by laneMidiPlay.js, the CSS does the rest).
//   Stem: the waveform in front, the notes faint behind it.
//   MIDI: the notes in front, the waveform dimmed.
//   Both: the two at the same strength.
// tests/e2e/lane-midi.spec.mjs checks the attribute follows the switch.
//
// Run:  node tests/js/midi-roll-layers.test.mjs

import { readFileSync } from "node:fs";
import { SOURCES, normalSource } from "../../static/js/midiNotesView.js";

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

const css = readFileSync(new URL("../../static/css/waves.css", import.meta.url), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

/** Every rule as [selector, body], comments gone. */
const rules = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((m) => [m[1].trim().replace(/\s+/g, " "), m[2]]);

/** The three values a rule sets, or null without the rule. */
function layer(selector) {
  const hit = rules.find(([sel]) => sel === selector);
  if (!hit) return null;
  const prop = (name) => hit[1].match(new RegExp(`${name}\\s*:\\s*([^;]+);`))?.[1].trim() ?? null;
  return {
    wave: Number(prop("--midi-wave-opacity")),
    notes: Number(prop("--midi-notes-opacity")),
    z: prop("--midi-notes-z"),
  };
}

const BASE = ".stem-waveform-row.midi-view";
const of = (source) => layer(`${BASE}[data-midi-source="${source}"]`);

// ─── Every Sound has its picture, and only those ───

for (const s of SOURCES) check(`the "${s}" Sound has a picture`, of(s) !== null);
const named = [...css.matchAll(/data-midi-source="([^"]+)"/g)].map((m) => m[1]);
eq("no picture for a Sound that does not exist", [...new Set(named)].filter((s) => !SOURCES.includes(s)), []);

// Without the attribute the row is drawn as Stem, as an unknown source is.
eq("without a Sound the picture is Stem's", layer(BASE), of(normalSource(undefined)));

// ─── Which is in front ───

const stem = of("stem");
const midi = of("midi");
const both = of("both");
check("Stem: the waveform is stronger than the notes", stem.wave > stem.notes, JSON.stringify(stem));
eq("Stem: the notes are behind the waveform", stem.z, "-1");
check("MIDI: the notes are stronger than the waveform", midi.notes > midi.wave, JSON.stringify(midi));
eq("MIDI: the notes are in front", midi.z, "auto");
eq("MIDI: the notes at full strength", midi.notes, 1);
check("Both: the two at the same strength", both.wave === both.notes, JSON.stringify(both));
check("Both: clearly visible", both.wave >= 0.6, JSON.stringify(both));
eq("Both: the notes over the waveform", both.z, "auto");

// The one behind is dimmed, never gone: the two can still be compared.
for (const [s, v] of [["stem", stem], ["midi", midi]]) {
  check(`${s}: the faint picture still shows`, Math.min(v.wave, v.notes) >= 0.2, JSON.stringify(v));
}

// ─── The values are what the two elements draw with ───

const body = (selector) => rules.filter(([sel]) => sel.split(",").map((x) => x.trim()).includes(selector)).map(([, b]) => b).join(";");
check("the waveform's element takes its opacity", /opacity:\s*var\(--midi-wave-opacity\)/.test(body(`${BASE} > .stem-waveform-svg`)));
check("the roll takes its opacity", /opacity:\s*var\(--midi-notes-opacity\)/.test(body(`${BASE} > .midi-roll`)));
check("the roll takes its order", /z-index:\s*var\(--midi-notes-z\)/.test(body(`${BASE} > .midi-roll`)));
check("the row keeps that order to itself", /isolation:\s*isolate/.test(body(BASE)));
// A dim on the waveform's shapes loses to the .app:not(.is-import) rule that
// comes later; that is how the waveform once stayed bright behind the notes.
eq(
  "no MIDI-view rule dims the waveform's shapes",
  rules.filter(([sel]) => /midi-view/.test(sel) && /stem-waveform-svg\s+(rect|path)/.test(sel)).map(([sel]) => sel),
  [],
);
// A change of Sound fades on the motion tokens, so reduced motion stops it.
check("the fade is on the motion tokens", /transition:\s*opacity\s+var\(--t-(fast|base)\)/.test(body(`${BASE} > .midi-roll`)));

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
