// No two controls in the Extract row may carry the same word, in any language.
//
// The row holds three kinds of control that all sound like a selection: the
// All beside the chips, which means every stem; the six stem chips; and the
// vocal mode toggle, which is only about how the vocals are divided. The mode
// button used to be called "All" too, a few pixels from the other one, so the
// state most people open the app in showed two buttons with the same label
// meaning different things. A reporter read them as a duplicate control and
// asked why there were two (#659).
//
// It is now "Combined", and this is what stops it coming back. The risk is not
// the English string, which is under someone's eye: it is a translator or a
// future table reaching for the local word for "all" twice, which restores the
// collision in one language and is invisible to everyone who does not read it.
// Before the rename, every one of the ten tables had the two keys identical.
//
// Run:  node tests/js/extract-labels.test.mjs

import { TRANSLATIONS, LANGUAGES } from "../../static/js/i18n.js";

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

// Every label a user can read in that row, in the order they appear.
const ROW_KEYS = [
  "extract.all",
  "stem.vocals",
  "vocalMode.all",
  "vocalMode.split",
  "stem.drums",
  "stem.bass",
  "stem.guitar",
  "stem.piano",
  "stem.other",
];

// Regional tables are partial and fall through to the base language, so
// pt-PT inherits most of its row from pt. Resolving the same way the app does
// is the point: a collision that only appears after the fallback is still a
// collision on screen.
function label(code, key) {
  const base = code.includes("-") ? code.split("-")[0] : null;
  const chain = [TRANSLATIONS[code], base ? TRANSLATIONS[base] : null, TRANSLATIONS.en];
  for (const table of chain) {
    if (table && table[key] != null) return table[key];
  }
  return null;
}

const codes = LANGUAGES.map((l) => l.code);
check("every shipped language has a table", codes.every((c) => TRANSLATIONS[c]), codes.join(","));

for (const code of codes) {
  const seen = new Map();
  const clashes = [];
  for (const key of ROW_KEYS) {
    const text = label(code, key);
    if (text == null) {
      clashes.push(`${key} is missing`);
      continue;
    }
    // Case and surrounding space are not what tells two buttons apart.
    const norm = text.trim().toLowerCase();
    if (seen.has(norm)) clashes.push(`${seen.get(norm)} and ${key} are both "${text}"`);
    else seen.set(norm, key);
  }
  check(`${code}: every control in the Extract row reads differently`, clashes.length === 0, clashes.join("; "));
}

// The specific pair the bug was about, stated on its own so a failure names it.
for (const code of codes) {
  check(
    `${code}: the vocal mode is not called what the stem row's All is called`,
    label(code, "vocalMode.all").trim().toLowerCase() !== label(code, "extract.all").trim().toLowerCase(),
    `both "${label(code, "extract.all")}"`,
  );
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
