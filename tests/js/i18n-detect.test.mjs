// Every shipped language must be reachable from the browser's own locale.
//
// Adding a table to LANGUAGES and forgetting the matching branch in
// _detectDefault fails silently in the worst way: the language appears in the
// picker, every string is translated, and a native speaker still opens the app
// in English. Nothing throws and no existing test notices.
//
// That is not hypothetical. French shipped without a branch and stayed that way
// until Spanish was added next to it and the gap was spotted by reading the
// function. This pins it so the next language cannot repeat it.
//
// Run:  node tests/js/i18n-detect.test.mjs

import { LANGUAGES, _detectDefault } from "../../static/js/i18n.js";

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

/** Pretend the browser reports `tag`, then ask what StemDeck would pick. */
function detectFor(tag) {
  Object.defineProperty(globalThis, "navigator", {
    value: { languages: [tag], language: tag },
    configurable: true,
    writable: true,
  });
  return _detectDefault();
}

// The realistic locale a speaker of each shipped language actually sends.
const LOCALES = {
  en: ["en", "en-US", "en-GB"],
  pl: ["pl", "pl-PL"],
  ja: ["ja", "ja-JP"],
  ko: ["ko", "ko-KR"],
  "zh-Hans": ["zh", "zh-CN", "zh-Hans", "zh-SG"],
  de: ["de", "de-DE", "de-AT"],
  fr: ["fr", "fr-FR", "fr-CA"],
  es: ["es", "es-ES", "es-MX", "es-419"],
  pt: ["pt", "pt-BR"],
  "pt-PT": ["pt-PT"],
  id: ["id", "id-ID"],
};

// 1. Nothing ships without a way to be found.
for (const { code } of LANGUAGES) {
  check(
    `${code} has locales covering it`,
    Array.isArray(LOCALES[code]) && LOCALES[code].length > 0,
    "add this language's locales to LOCALES above",
  );
}

// 2. Every one of those locales resolves to its own table.
for (const [code, tags] of Object.entries(LOCALES)) {
  if (!LANGUAGES.some((l) => l.code === code)) continue; // language was removed
  for (const tag of tags) {
    const got = detectFor(tag);
    check(`${tag} -> ${code}`, got === code, `got ${got}`);
  }
}

// 3. Case is not something a browser guarantees.
check("PT-pt is still European Portuguese", detectFor("PT-pt") === "pt-PT");
check("ES-MX is still Spanish", detectFor("ES-MX") === "es");

// 4. Anything unrecognised falls back to English rather than throwing.
for (const tag of ["", "xx", "kl-GL", "zz-ZZ"]) {
  check(`${tag || "(empty)"} falls back to en`, detectFor(tag) === "en");
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
