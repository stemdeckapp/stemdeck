// Settings > Separation, "My models" (static/js/myModelsView.js): the rules
// behind the form, the list rows and the requests. Pure, so it runs without a
// browser; tests/e2e/my-models.spec.mjs checks the drawing against a stubbed server.
//
// Run:  node tests/js/my-models-view.test.mjs

import { TRANSLATIONS } from "../../static/js/i18n.js";
import {
  ADD_MODEL_VALUE,
  ADD_STEMS,
  CONFIG_ACCEPT,
  MAX_NAME,
  MODEL_ACCEPT,
  REASON_CODES,
  addRoute,
  anyActive,
  canAdd,
  canRunModels,
  downloadPct,
  needsDownload,
  entriesOf,
  focusAfterRemove,
  formatSize,
  isActive,
  isHfLink,
  isMyModelsAnswer,
  linkBody,
  metaLine,
  missingKeys,
  missingLine,
  nameFrom,
  parseHfLink,
  reasonText,
  refusalCode,
  shortSha,
  sourceLine,
  statusView,
  stemsText,
  uploadFields,
  uploadPct,
  userSourceView,
} from "../../static/js/myModelsView.js";

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

const tableT = (table) => (key, vars = {}) =>
  String(table[key] ?? TRANSLATIONS.en[key] ?? key).replace(/\{(\w+)\}/g, (_, k) => (k in vars ? String(vars[k]) : `{${k}}`));
const en = TRANSLATIONS.en;
const t = tableT(en);

const LINK = "https://huggingface.co/owner/repo";
const SHA_A = "a".repeat(64);

// The link rule, the server's (app/core/hf_fetch.py, plan section 6.2).
for (const good of [
  LINK,
  `${LINK}/`,
  ` ${LINK} `,
  `${LINK}?x=1#readme`,
  "HTTPS://HuggingFace.co/owner/repo",
  "https://huggingface.co:443/owner/repo",
  `${LINK}/tree/main`,
  `${LINK}/tree/0123456789abcdef0123456789abcdef01234567`,
  `${LINK}/blob/main/model.ckpt`,
  `${LINK}/resolve/v1.0/weights/model.safetensors`,
  `${LINK}/blob/main/a/b/model.ckpt`,
  `${LINK}/blob/main/my%20model.ckpt`,
]) check(`link accepted: ${good}`, isHfLink(good));
for (const bad of [
  "",
  "http://huggingface.co/owner/repo",
  "https://huggingface.co/owner",
  "https://huggingface.co/",
  "https://example.com/owner/repo",
  "https://huggingface.co.evil.com/owner/repo",
  "https://evilhuggingface.co/owner/repo",
  "https://huggingface.co./owner/repo",
  "https://user@huggingface.co/owner/repo",
  "https://user:pw@huggingface.co/owner/repo",
  "https://huggingface.co:8443/owner/repo",
  "https://huggingface.co/owner/repo/tree",
  "https://huggingface.co/owner/repo/tree/main/extra",
  "https://huggingface.co/owner/repo/blob/main",
  "https://huggingface.co/owner/repo/blob/main/a/b/c/model.ckpt",
  "https://huggingface.co/owner/repo/blob/main/..%2Fx.ckpt",
  "https://huggingface.co/owner/repo/blob/main/.hidden",
  "https://huggingface.co/owner/repo/discussions/1",
  "https://huggingface.co/.owner/repo",
  "https://huggingface.co//owner/repo",
  "https://huggingface.co\\owner/repo",
  `https://huggingface.co/owner/${"r".repeat(300)}`,
  "huggingface.co/owner/repo",
]) check(`link refused: ${JSON.stringify(bad).slice(0, 70)}`, !isHfLink(bad));
eq("a model page parses to its parts", parseHfLink(LINK), { owner: "owner", repo: "repo", rev: "main", file: null });
eq("a file link names the file", parseHfLink(`${LINK}/resolve/abc/w/m.ckpt`), { owner: "owner", repo: "repo", rev: "abc", file: "w/m.ckpt" });

// The Add model rule: a link or both files, a stem, and the licence box.
check("a link, a stem and the licence: on", canAdd({ link: LINK, stems: ["guitar"], licence: true }));
check("both files instead of a link: on", canAdd({ fileName: "model.ckpt", configName: "c.yaml", stems: ["drums"], licence: true }));
check("a file without its config: off (the config is required)", !canAdd({ fileName: "model.ckpt", stems: ["drums"], licence: true }));
check("a config without its file: off", !canAdd({ configName: "c.yaml", stems: ["drums"], licence: true }));
check("no link and no file: off", !canAdd({ stems: ["guitar"], licence: true }));
check("no stem: off", !canAdd({ link: LINK, stems: [], licence: true }));
check("licence box not ticked: off", !canAdd({ link: LINK, stems: ["guitar"], licence: false }));
check("a licence that is only truthy is not ticked", !canAdd({ link: LINK, stems: ["guitar"], licence: "yes" }));
check("a link that is not Hugging Face: off", !canAdd({ link: "https://example.com/owner/repo", stems: ["guitar"], licence: true }));
check("a bad link but both files: on", canAdd({ link: "nope", fileName: "m.ckpt", configName: "c.yaml", stems: ["bass"], licence: true }));
check("a blank file name is no file", !canAdd({ fileName: "  ", configName: "c.yaml", stems: ["bass"], licence: true }));
check("empty form: off", !canAdd());
check("online sources off: a link does not count", !canAdd({ link: LINK, stems: ["vocals"], licence: true, linkAllowed: false }));
check("online sources off: files still work", canAdd({ link: LINK, fileName: "m.ckpt", configName: "c.yaml", stems: ["vocals"], licence: true, linkAllowed: false }));
eq("a link and files: the link is used", addRoute({ link: LINK, fileName: "m.ckpt", configName: "c.yaml" }), "link");
eq("a link and files with link import off: the files", addRoute({ link: LINK, fileName: "m.ckpt", configName: "c.yaml", linkAllowed: false }), "files");
eq("neither: no route", addRoute({ link: "x" }), null);

// What is still missing, in words.
eq("what is missing, all of it", missingLine({}, t, "en"), "Still needed: a link or a file, at least one stem, and the licence box.");
eq("a link typed wrong", missingLine({ link: "huggingface.co/x", stems: ["vocals"], licence: true }, t, "en"),
  "Still needed: a Hugging Face link like https://huggingface.co/owner/repo.");
eq("a file without its config", missingLine({ fileName: "m.ckpt", stems: ["vocals"], licence: true }, t, "en"), "Still needed: the config file.");
eq("a config without its file", missingLine({ configName: "c.yaml", stems: ["vocals"], licence: true }, t, "en"), "Still needed: the model file.");
eq("link import off: both files", missingKeys({ link: LINK, stems: ["vocals"], licence: true, linkAllowed: false }), ["settings.myModels.missing.files"]);
eq("nothing missing", missingLine({ link: LINK, stems: ["vocals"], licence: true }, t, "en"), "");
eq("the list joins in the reader's language", missingLine({}, tableT(TRANSLATIONS.de), "de"),
  "Es fehlt noch: ein Link oder eine Datei, mindestens ein Stem und das Lizenzkästchen.");

// Names, bodies and fields.
eq("name from the link's repo", nameFrom({ link: "https://huggingface.co/owner/guitar-roformer" }), "guitar-roformer");
eq("name from a file link", nameFrom({ link: `${LINK}/blob/main/w/kim_vocals.ckpt` }), "kim_vocals");
eq("name from the file, no extension", nameFrom({ fileName: "my.model.ckpt" }), "my.model");
eq("name from a path keeps only the file", nameFrom({ fileName: "C:\\models\\vocals.ckpt" }), "vocals");
eq("no source: no name (the server names it)", nameFrom({}), "");
eq("a long name is cut to the server's limit", [...nameFrom({ fileName: `${"x".repeat(80)}.ckpt` })].length, MAX_NAME);
eq("the link body", linkBody({ link: ` ${LINK} `, stems: ["bass", "vocals", "bass"] }),
  { link: LINK, stems: ["vocals", "bass"], licence_confirmed: true, name: "repo" });
check("the link body always confirms the licence as true", linkBody({ link: LINK, stems: ["vocals"] }).licence_confirmed === true);
eq("the upload fields", uploadFields({ fileName: "drums.safetensors", stems: ["piano", "drums", "nope"] }),
  [["stems", "[\"drums\",\"piano\"]"], ["licence", "true"], ["name", "drums"]]);
eq("the pickers: no ONNX, no JSON config", [MODEL_ACCEPT, CONFIG_ACCEPT], [".safetensors,.ckpt,.pth,.pt", ".yaml,.yml"]);
eq("the stems a model can declare", ADD_STEMS, ["vocals", "drums", "bass", "guitar", "piano"]);
check("the Add option's value is not an id the server makes", !/^user_/.test(ADD_MODEL_VALUE));

// Sizes, hashes, stems.
eq("sizes", [formatSize(912_000_000, t, "en"), formatSize(400_000, t, "en"), formatSize(12_500_000_000, t, "en"), formatSize(0, t, "en")],
  ["912 MB", "1 MB", "12.5 GB", "Size known after download"]);
eq("sizes in French use Mo", formatSize(912_000_000, tableT(TRANSLATIONS.fr), "fr"), "912 Mo");
eq("short hash", shortSha("0123456789abcdef0123"), "012345...0123");
eq("stem names", stemsText(["drums", "bass"], t, "en"), "Drums and Bass");

// Reasons: every code has its sentence, and an unknown one the general one.
for (const code of REASON_CODES) check(`reason ${code} has a sentence`, `settings.myModels.reason.${code}` in en);
eq("an unknown code says the general sentence", reasonText("from_the_future", t), en["settings.myModels.reason.unknown"]);
eq("a known code says its own", reasonText("key_mismatch", t), "The model file does not match its config file.");
eq("a refusal's code is the body's", refusalCode(409, { detail: "x", code: "duplicate" }), "duplicate");
eq("a 413 without a code is too large", refusalCode(413, null), "too_large");
eq("a 422 from the validator has no code", refusalCode(422, { detail: [{ loc: ["body", "stems"] }] }), "unknown");
check("a refusal never says the server's own words", !reasonText(refusalCode(422, { detail: "C:\\secret\\path" }), t).includes("secret"));

// The answer and its rows.
const READY_BASE = { id: "user_0123456789ab", name: "Guitar RoFormer", status: "ready", stems: ["guitar"] };
const READY = { id: "user_0123456789ab", name: "Guitar RoFormer", status: "ready", pct: 100, code: null, size: 912_000_000,
  sha256: "a".repeat(64), stems: ["guitar"], usable: true, missing: false,
  source: { kind: "hf", label: "owner/guitar@0123456789ab", license_declared: "MIT" } };
const answer = { models: [READY, { id: "user_aaaaaaaaaaaa", name: "Bad", status: "refused", code: "too_big_model", stems: ["vocals"] }, { nope: 1 }],
  limits: { max_bytes: 2147483648, max_models: 12, max_params: 500000000 }, registry_reset: false };
check("an answer with models is one", isMyModelsAnswer(answer));
check("an older server's 404 body is not", !isMyModelsAnswer({ detail: "Not Found" }));
eq("entries without an id are left out", entriesOf(answer).map((m) => m.id), ["user_0123456789ab", "user_aaaaaaaaaaaa"]);
eq("a ready row", statusView(READY, t), { status: "ready", label: "Ready", reason: "", pct: 0, active: false });
eq("a refused row says why", statusView(answer.models[1], t).reason, "The model is too big: it would need more memory than allowed.");
// A link download is 0..80 of the server's pct and the test run 80..100
// (app/api/my_models.py): the badge says how far the file itself is.
eq("a downloading row shows the file's percent", statusView({ status: "downloading", pct: 40 }, t), { status: "downloading", label: "Downloading 50%", reason: "", pct: 50, active: true });
eq("the whole file is 100%", downloadPct({ pct: 80 }), 100);
eq("never over 100%", downloadPct({ pct: 95 }), 100);
eq("staging (an upload being stored) reads as checking", statusView({ status: "staging" }, t).status, "checking");
eq("the server's missing status", statusView({ ...READY_BASE, status: "missing", missing: true, usable: false }, t).status, "missing");
eq("a ready model whose file changed says so", statusView({ ...READY_BASE, usable: false, code: "changed" }, t).reason, en["settings.myModels.reason.changed"]);
check("an install that can run models", canRunModels({ models: [], available: true }) && canRunModels({ models: [] }));
check("an install that cannot", !canRunModels({ models: [], available: false }));
eq("checking is active", statusView({ status: "checking", pct: 90 }, t).active, true);
eq("a ready model whose file went", statusView({ ...READY, missing: true }, t).status, "missing");
eq("a ready model this install cannot run", statusView({ ...READY, usable: false }, t), { status: "unusable", label: "Not available", reason: en["settings.myModels.reason.unavailable"], pct: 0, active: false });
eq("a status this page does not know is refused, generally", statusView({ status: "weird" }, t).reason, en["settings.myModels.reason.unknown"]);
check("active entries keep the poll going", anyActive([READY, { status: "checking" }]) && !anyActive([READY]) && isActive({ status: "downloading" }));
eq("the meta line", metaLine(READY, t, "en"), "912 MB · separates Guitar · SHA-256 aaaaaa...aaaa");
eq("the source line, with the declared licence", sourceLine(READY, t), "From Hugging Face: owner/guitar@0123456789ab. Licence declared by its source: MIT. StemDeck does not check it.");
eq("an upload's source line", sourceLine({ source: { kind: "upload", label: "vocals.ckpt", license_declared: null } }, t), "From your computer: vocals.ckpt.");
eq("no source, no line", sourceLine({}, t), "");
// The Source line: always "added by you", linked to where it was added from.
eq("added from a link: the link", userSourceView({ source: { kind: "hf", link: "https://huggingface.co/owner/guitar" } }, t), {
  before: "Source: added by you, ",
  link: { text: "huggingface.co/owner/guitar", url: "https://huggingface.co/owner/guitar" },
  after: "",
});
eq("added from files: no link", userSourceView({ source: { kind: "upload", link: null } }, t), { before: "Source: added by you", link: null, after: "" });
eq("an older server sends no link", userSourceView({ source: { kind: "hf" } }, t).link, null);
eq("European Portuguese says por si", userSourceView({}, (k, v) => String(TRANSLATIONS["pt-PT"][k] ?? TRANSLATIONS.pt[k]).replace(/\{(\w+)\}/g, (_, x) => v[x] ?? "")).before, "Fonte: adicionado por si");

// Codes the server added late: the gates, and linear attention as a config refusal.
eq("gates_missing has its own sentence", reasonText("gates_missing", t), en["settings.myModels.reason.gates_missing"]);
check("the config refusal names linear attention", /linear attention/.test(reasonText("bad_config", t)));
// A link entry whose download failed keeps no files: Check again downloads it again.
const NO_FILES = { id: "user_dddddddddddd", name: "x", status: "refused", code: "network", sha256: null, size: null, source: { kind: "hf", label: "o/r@abc", license_declared: null } };
check("a link entry with no files downloads again", needsDownload(NO_FILES));
check("an upload never does", !needsDownload({ ...NO_FILES, source: { kind: "upload", label: "m.ckpt" } }));
check("a link entry with its files does not", !needsDownload({ ...NO_FILES, sha256: SHA_A }));
check("nor one that is downloading now", !needsDownload({ ...NO_FILES, status: "downloading" }));

// Focus after Remove, and the upload's percent.
eq("removing the first of three: the new first", focusAfterRemove(0, 2), 0);
eq("removing the last of three: the new last", focusAfterRemove(2, 2), 1);
eq("removing the only one: the list title", focusAfterRemove(0, 0), -1);
eq("upload percent", [uploadPct(50, 200), uploadPct(200, 200), uploadPct(1, 0), uploadPct(NaN, 5)], [25, 100, null, null]);

// Every key this module and myModels.js read is in every table.
const KEYS = Object.keys(en).filter((k) => k.startsWith("settings.myModels.") || k.startsWith("help.set.myModels"));
check("the keys are there to check", KEYS.length > 90, String(KEYS.length));
for (const [lang, table] of Object.entries(TRANSLATIONS)) {
  if (lang === "pt-PT") continue;
  const missing = KEYS.filter((k) => !(k in table));
  check(`${lang} has every My models key`, missing.length === 0, missing.join(", "));
}
// "(yours)" keeps the name in every language.
for (const [lang, table] of Object.entries(TRANSLATIONS)) {
  if (lang === "pt-PT") continue;
  check(`${lang}: the yours pattern carries the name`, String(table["settings.myModels.yours"]).includes("{name}"));
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
