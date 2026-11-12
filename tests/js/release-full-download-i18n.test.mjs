// The update dialog's full-download note in every language table.
//
// It is shown when a release changes the Python packages, so the in-app update
// cannot apply it. On Windows the portable package keeps the library in the
// data folder beside StemDeck.exe, so a new folder starts without it: every
// table has to say why it is a full download and to unzip over the current
// folder, not only English.
//
// Run:  node tests/js/release-full-download-i18n.test.mjs

import { readFileSync } from "node:fs";
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

const KEY = "release.fullDownload";
const FALLBACK = { "pt-PT": "pt" };

for (const { code } of LANGUAGES) {
  const own = TRANSLATIONS[code];
  const table = own && FALLBACK[code] ? { ...TRANSLATIONS[FALLBACK[code]], ...own } : own;
  const text = table?.[KEY] ?? "";
  check(`${code}: exists`, text.trim() !== "");
  if (code !== "en") check(`${code}: is translated`, text !== TRANSLATIONS.en[KEY]);
  // Literal names the user sees on disk or in the dialog stay untranslated.
  for (const word of ["Python", "Windows", "StemDeck", "data"]) {
    check(`${code}: names ${word}`, text.includes(word), text);
  }
  check(`${code}: no em dash`, !text.includes(String.fromCharCode(0x2014)));
}

const html = readFileSync(new URL("../../static/index.html", import.meta.url), "utf8");
const fallback = html.match(/data-i18n="release\.fullDownload">([^<]*)</)?.[1];
check("index.html fallback matches the English table", fallback === TRANSLATIONS.en[KEY], fallback);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
