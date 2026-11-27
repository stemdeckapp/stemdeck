// The REAPER export rows, the External editors setting and their Guide
// entries (#1071): the strings reach all ten language tables, and the markup,
// the Guide and the code name the same keys and ids.
//
// Run:  node tests/js/reaper-export.test.mjs

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { TRANSLATIONS } from "../../static/js/i18n.js";

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

const read = (p) => readFileSync(fileURLToPath(new URL(`../../static/${p}`, import.meta.url)), "utf8");
const html = read("index.html");
const help = read("js/helpMode.js");
const catalog = read("js/catalog.js");
const main = read("js/main.js");
const player = read("js/player.js");

const KEYS = [
  "export.reaper",
  "export.reaperDesc",
  "export.openReaper",
  "export.openReaperDesc",
  "settings.group.externalEditors",
  "settings.reaper.title",
  "settings.reaper.desc",
  "settings.reaper.choose",
  "settings.reaper.reset",
  "settings.reaper.foundSuffix",
  "settings.reaper.notFound",
  "settings.reaper.chosenMissing",
  "settings.reaper.pickerFailed",
  "help.exportReaper",
  "help.openReaper",
  "help.set.reaper",
];

// pt-PT is a regional variant that resolves through pt, so it needs no copy.
const full = Object.keys(TRANSLATIONS).filter((l) => l !== "pt-PT");
check("the tables are the ten full languages", full.length === 10, full.join(","));

for (const lang of full) {
  const table = TRANSLATIONS[lang];
  const missing = KEYS.filter((k) => typeof table[k] !== "string" || !table[k].trim());
  check(`${lang} has every REAPER key`, missing.length === 0, missing.join(", "));
}

// An English sentence copied into another table is the silent failure the
// fallback hides. The product name alone is the same everywhere.
for (const lang of full.filter((l) => l !== "en")) {
  const same = KEYS.filter((k) => k !== "settings.reaper.title" && TRANSLATIONS[lang][k] === TRANSLATIONS.en[k]);
  check(`${lang} translates every REAPER sentence`, same.length === 0, same.join(", "));
}

for (const lang of full) {
  check(
    `${lang} keeps the {path} placeholder`,
    TRANSLATIONS[lang]["settings.reaper.chosenMissing"].includes("{path}"),
  );
}

// Markup, Guide and code agree.
for (const id of ["t-export-reaper", "t-export-open-reaper"]) {
  check(`index.html has #${id}`, html.includes(`id="${id}"`));
  check(`the Guide covers #${id}`, help.includes(`"#${id}"`));
}
check(
  "Open in REAPER starts hidden (desktop only)",
  /id="t-export-open-reaper"[^>]*\bhidden\b/.test(html),
);
check("the Guide lists the REAPER setting", help.includes('["settings.reaper.title", "help.set.reaper"]'));
check("the REAPER row title is translated", catalog.includes('data-i18n="settings.reaper.title"'));
check("the settings group is desktop only", /group\.remove\(\)/.test(catalog));
check("the export menu asks the shell for REAPER", main.includes('invoke("reaper_status")'));

// The zip request: reaper=1 only for the REAPER rows, so the default export
// URL is exactly what it was.
check("only the REAPER request sets reaper=1", (player.match(/q\.set\("reaper", "1"\)/g) || []).length === 1);
check("it is conditional on the reaper flag", /if \(reaper\) q\.set\("reaper", "1"\)/.test(player));
check(
  "Export Extracted Stems does not ask for a project",
  /menu\.wireRow\(itemStems,[\s\S]{0,300}downloadAllStemsZip\(menu\.format, onStart\), t\(/.test(main),
);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
