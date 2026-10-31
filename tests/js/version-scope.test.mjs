// Versions of a song (#804, static/js/versionScope.js): the default version
// keeps the record keys every song had (no migration, nothing moved), any
// other version gets keys of its own, a damaged list from the server is made
// safe, and the helpers the Extract button and the popover rely on.
import {
  DEFAULT_VERSION_ID, cleanVersions, setSongVersions, songVersions, activeVersionId,
  activeVersionStems, scopeFor, sameStems, versionWithStems, nextVersionNumber,
} from "../../static/js/versionScope.js";
import { choiceKey } from "../../static/js/partChoice.js";
import { TRANSLATIONS } from "../../static/js/i18n.js";

let failed = 0;
let passed = 0;
function check(name, ok, detail = "") {
  if (ok) { passed += 1; console.log(`PASS  ${name}`); } else { failed += 1; console.log(`FAIL  ${name} ${detail}`); }
}
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const JOB = "a1b2c3d4e5f6";

// -- A song with no versions known
check("an unknown song opens its default version", activeVersionId(JOB) === DEFAULT_VERSION_ID);
check("the default version's scope is the song id", scopeFor(JOB) === JOB);
check("its stem key is the one every song had", choiceKey(JOB, "practice") === `stemdeck:stems:${JOB}:practice`);
check("no version stems are known for it", activeVersionStems(JOB) === null);
check("its implicit list has one version", songVersions(JOB).length === 1 && songVersions(JOB)[0].id === DEFAULT_VERSION_ID);

// -- Two versions
setSongVersions(JOB, [
  { id: "main", name: null, stems: ["vocals", "drums", "bass"], created_at: 1 },
  { id: "c3", name: "Guitar study", stems: ["guitar", "drums"], created_at: 2 },
], "c3");
check("the chosen version is open", activeVersionId(JOB) === "c3");
check("another version has a scope of its own", scopeFor(JOB) === `${JOB}~c3`);
check("its stem key is its own", choiceKey(JOB, "practice") === `stemdeck:stems:${JOB}~c3:practice`);
check("a key for a named version can be asked for", choiceKey(JOB, "daw", "main") === `stemdeck:stems:${JOB}:daw`);
check("its stems are in stem order", eq(activeVersionStems(JOB), ["drums", "guitar"]));
check("a list copy cannot change the stored one", (() => { songVersions(JOB)[1].stems.push("piano"); return eq(activeVersionStems(JOB), ["drums", "guitar"]); })());

setSongVersions(JOB, songVersions(JOB), "gone");
check("an active version that is gone falls back to the first", activeVersionId(JOB) === "main");

let threw = false;
try { scopeFor(JOB, "../x"); } catch { threw = true; }
check("a bad version id never becomes a key", threw);

// -- A damaged list
const cleaned = cleanVersions([
  { id: "main", stems: ["kazoo", "bass"] },
  { id: "main", stems: ["drums"] },
  { id: "BAD ID", stems: ["drums"] },
  null,
  "x",
  { id: "z9", name: "   ", stems: "drums", created_at: "now" },
]);
check("bad entries are dropped, the rest kept", eq(cleaned.map((v) => v.id), ["main", "z9"]));
check("unknown stems are dropped", eq(cleaned[0].stems, ["bass"]));
check("no stems reads as every stem", cleaned[1].stems.length === 6);
check("a blank name is no name", cleaned[1].name === null && cleaned[1].created_at === 0);
setSongVersions("ffffffffffff", "not a list", null);
check("a song sent no usable list keeps its default", activeVersionId("ffffffffffff") === DEFAULT_VERSION_ID);

// -- The helpers
check("the same stems in any order match", sameStems(["drums", "vocals"], ["vocals", "drums"]));
check("different stems do not", !sameStems(["drums"], ["drums", "bass"]));
check("a version with the same stems is found", versionWithStems(songVersions(JOB), ["drums", "guitar"])?.id === "c3");
check("none is found for new stems", versionWithStems(songVersions(JOB), ["piano"]) === null);
check("the next number is one past the count", nextVersionNumber([{ name: null }, { name: "Mine" }]) === 3);
check("a number already used as a name is skipped", nextVersionNumber([{ name: null }, { name: "Version 3" }]) === 4);

// -- Every string the versions UI shows is in all ten tables
const keys = [
  "versions.title", "versions.unnamed", "versions.allStems", "versions.nameLabel", "versions.rename",
  "versions.renamed", "versions.saveNew", "versions.created", "versions.opened", "versions.alreadyOpen",
  "versions.extractingMissing", "versions.extractFailed", "versions.delete", "versions.deleteConfirm",
  "versions.deleted", "versions.failed", "versions.openVersion", "versions.openFailed",
  "help.versionRow", "help.versionSelect", "help.versionName", "help.versionRename",
  "help.versionSaveNew", "help.versionDelete",
];
const tables = Object.entries(TRANSLATIONS).filter(([code]) => code !== "pt-PT");
const missing = [];
for (const k of keys) for (const [code, table] of tables) if (!table[k]) missing.push(`${code}:${k}`);
check("every versions string is in all ten tables", tables.length === 10 && !missing.length, missing.join(" "));
const placeholders = [];
for (const [code, table] of tables) {
  if (!table["versions.unnamed"]?.includes("{n}")) placeholders.push(`${code}:unnamed`);
  if (!table["versions.openVersion"]?.includes("{name}") || !table["versions.openVersion"]?.includes("{stems}")) placeholders.push(`${code}:openVersion`);
}
check("every table keeps the placeholders", !placeholders.length, placeholders.join(" "));

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
