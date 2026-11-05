// The queue strings must exist in every language table, not only in English.
//
// t() falls back to English for a missing key, so an untranslated queue label
// looks fine in every check that reads the English table (#799). This reads
// each table's OWN keys, with no fallback, so a gap fails by name.
//
// pt-PT is a regional variant that resolves through pt, so it is checked via
// the same fallback the app uses instead of for own keys.
//
// Run:  node tests/js/queue-i18n.test.mjs

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

const KEYS = ["queue.pausedStatus", "queue.positionInLine", "queue.emptyState"];
const FALLBACK = { "pt-PT": "pt" };

for (const { code } of LANGUAGES) {
  const own = TRANSLATIONS[code];
  const table = own && FALLBACK[code] ? { ...TRANSLATIONS[FALLBACK[code]], ...own } : own;
  for (const key of KEYS) {
    const text = table?.[key];
    check(`${code}: ${key} exists`, typeof text === "string" && text.trim() !== "");
    // A non-English table that repeats the English text is a placeholder.
    if (code !== "en" && text) {
      check(`${code}: ${key} is translated`, text !== TRANSLATIONS.en[key]);
    }
  }
  check(
    `${code}: positionInLine keeps its {position} placeholder`,
    (table?.["queue.positionInLine"] ?? "").includes("{position}"),
  );
}

// The library row and the queue view show the position one way: "#N".
check("en: position reads #N", TRANSLATIONS.en["queue.positionInLine"].includes("#{position}"));

// No hardcoded English left in the code that builds these labels.
const queueJs = readFileSync(new URL("../../static/js/queue.js", import.meta.url), "utf8");
const catalogJs = readFileSync(new URL("../../static/js/catalog.js", import.meta.url), "utf8");
check("queue.js has no hardcoded 'Paused' label", !/["'`]Paused["'`]/.test(queueJs));
check("queue.js has no hardcoded 'Queued - ' label", !/Queued - /.test(queueJs));
check("catalog.js has no hardcoded queue empty state", !/Nothing importing/.test(catalogJs));

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
