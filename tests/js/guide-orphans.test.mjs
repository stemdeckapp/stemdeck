// The Guide's sentences against the controls they explain.
//
// Removing a control means removing its Guide entry and its help.* sentence in
// the same change. A sentence no code reads is left over from a control that
// is gone, and it stays in all ten tables for translators to keep up. This
// catches a sentence left behind, not a Guide entry for a control CSS hides.
//
// The footer's seek strip (#footer-scrub) was hidden by daw.css rule 7, so no
// person could reach it, and it was deleted rather than shown again (#934).
//
// Run:  node tests/js/guide-orphans.test.mjs

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
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

const STATIC = fileURLToPath(new URL("../../static/", import.meta.url));

function sources(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) sources(path, out);
    else if (/\.(js|html)$/.test(name) && name !== "i18n.js") out.push(readFileSync(path, "utf8"));
  }
  return out;
}
const code = sources(STATIC).join("\n");

const unread = Object.keys(TRANSLATIONS.en)
  .filter((key) => key.startsWith("help."))
  .filter((key) => !new RegExp(key.replaceAll(".", "[.]") + "(?![A-Za-z0-9_.])").test(code));
check("every help.* sentence is read by a Guide entry or the code", unread.length === 0, unread.join(", "));

const html = readFileSync(join(STATIC, "index.html"), "utf8");
check("the footer has no seek strip hidden from view", !html.includes('id="footer-scrub"'));
check("its label is gone from the tables", !("footer.seekAria" in TRANSLATIONS.en));

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
