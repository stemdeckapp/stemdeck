// Settings > Separation, "My models": what the form and the list say, and
// what a request sends. Pure functions with `t` (i18n.t) passed in, so
// tests/js/my-models-view.test.mjs runs them without a browser. myModels.js
// draws the section from what these return.
//
// Server (app/api/my_models.py):
//   GET    /api/my-models               { models: [entry], limits, registry_reset, available }
//   POST   /api/my-models/link          { link, stems, licence_confirmed: true, name? }   202 entry
//                                       (403 "online_sources_off" while online sources are off)
//   POST   /api/my-models/upload        multipart: model, config, stems (JSON), name, licence "true"   202 entry
//   POST   /api/my-models/{id}/check    202 entry
//   POST   /api/my-models/{id}/cancel   200 { id, model }
//   DELETE /api/my-models/{id}          200 { removed: id }
// An entry: { id, name, status, pct, step, code, size, sha256, stems, arch,
// params, source: { kind, label, license_declared }, added_at, checked_at,
// cpu_rtf, usable, missing }. status is staging, downloading, checking, ready,
// refused, or missing (a ready model whose file went).
// A refusal is { detail, code }; the page says it with
// settings.myModels.reason.<code>, never with the server's own words.

/** The stems a model can declare, in the order the form lists them. */
export const ADD_STEMS = ["vocals", "drums", "bass", "guitar", "piano"];
/** The value of the last option of every stem select. Never posted. */
export const ADD_MODEL_VALUE = "__add_model__";
/** A user model's id, as the server makes it. */
export const USER_MODEL_ID_RE = /^user_[a-f0-9]{12}$/;
/** Polling while a model downloads or is checked, as the speed test does. */
export const POLL_MS = 1000;
/** Polls in a row that may fail before the page stops and says so. */
export const POLL_FAIL_LIMIT = 5;
/** The longest display name the server keeps. */
export const MAX_NAME = 60;
/** The longest link the server reads. */
export const MAX_LINK = 300;
/** What the file pickers offer. The server decides by the bytes, not the name. */
export const MODEL_ACCEPT = ".safetensors,.ckpt,.pth,.pt";
export const CONFIG_ACCEPT = ".yaml,.yml";

/** Every reason the server can give, each with its sentence. */
export const REASON_CODES = [
  "too_large", "empty", "bad_format", "bad_config", "config_missing", "unsupported_arch",
  "unsupported_audio", "stem_not_in_model", "too_big_model", "key_mismatch", "non_finite",
  "load_failed", "forward_failed", "timeout", "torch_missing", "torch_too_old", "unavailable",
  "duplicate", "limit", "disk_full", "network", "not_found", "gated", "ambiguous",
  "bad_download", "bad_link", "interrupted", "changed", "busy", "in_use", "online_sources_off",
  "licence", "gates_missing",
];

/** The states that are still moving: the page polls while one is shown. */
export const ACTIVE_STATUSES = ["staging", "downloading", "checking"];

const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);

// The Hugging Face link rule, the same as the server's (app/core/hf_fetch.py):
// https, the host huggingface.co, a model page, or a tree, blob or resolve
// address in it. The page only decides whether Add is on; the server checks again.
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$/;
const REV_RE = /^(?:[0-9a-f]{40}|[A-Za-z0-9._-]{1,64})$/;
const FILE_RE = /^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$/;

/** { owner, repo, rev, file } from a Hugging Face link, or null. */
export function parseHfLink(link) {
  const s = String(link ?? "").trim();
  if (!s || s.length > MAX_LINK || !/^https:\/\//i.test(s) || s.includes("\\")) return null;
  // "user@host" before the host, which a URL parser would quietly accept.
  const authority = s.slice(8).split(/[/?#]/)[0];
  if (authority.includes("@")) return null;
  let u;
  try {
    u = new URL(s);
  } catch {
    return null;
  }
  if (u.protocol !== "https:" || u.username || u.password) return null;
  if (u.port && u.port !== "443") return null;
  if (u.hostname !== "huggingface.co") return null;
  const raw = u.pathname.replace(/\/$/, "").split("/").slice(1);
  let parts;
  try {
    parts = raw.map((p) => decodeURIComponent(p));
  } catch {
    return null;
  }
  if (parts.length < 2 || parts.some((p) => p === "")) return null;
  const [owner, repo, kind, rev, ...file] = parts;
  if (!NAME_RE.test(owner) || !NAME_RE.test(repo)) return null;
  if (parts.length === 2) return { owner, repo, rev: "main", file: null };
  if (!REV_RE.test(rev ?? "")) return null;
  if (kind === "tree") return parts.length === 4 ? { owner, repo, rev, file: null } : null;
  if (kind !== "blob" && kind !== "resolve") return null;
  if (file.length < 1 || file.length > 3 || !file.every((f) => FILE_RE.test(f))) return null;
  return { owner, repo, rev, file: file.join("/") };
}

/** Whether `link` reads as a Hugging Face model address the server accepts. */
export function isHfLink(link) {
  return parseHfLink(link) !== null;
}

const filled = (v) => String(v ?? "").trim() !== "";

/** Which way the form adds: "link", "files", or null when neither is complete.
 *  A link counts only while link import is allowed (online sources on). */
export function addRoute({ link = "", fileName = "", configName = "", linkAllowed = true } = {}) {
  if (linkAllowed && isHfLink(link)) return "link";
  if (filled(fileName) && filled(configName)) return "files";
  return null;
}

/** What the form has to add: a link, or a model file with its config file,
 *  one stem or more, and the licence box. */
export function canAdd(state = {}) {
  const { stems = [], licence = false } = state;
  return addRoute(state) !== null && stems.length > 0 && licence === true;
}

/** A list in the reader's language: "a, b and c". */
export function listText(items, locale) {
  try {
    return new Intl.ListFormat(locale || undefined, { style: "long", type: "conjunction" }).format(items);
  } catch {
    return items.join(", ");
  }
}

/** The keys of what is still missing, in the order the form asks for them. */
export function missingKeys({ link = "", fileName = "", configName = "", stems = [], licence = false, linkAllowed = true } = {}) {
  const keys = [];
  if (addRoute({ link, fileName, configName, linkAllowed }) === null) {
    const hasFile = filled(fileName);
    const hasConfig = filled(configName);
    if (hasFile && !hasConfig) keys.push("settings.myModels.missing.config");
    else if (!hasFile && hasConfig) keys.push("settings.myModels.missing.file");
    else if (!linkAllowed) keys.push("settings.myModels.missing.files");
    else if (filled(link)) keys.push("settings.myModels.missing.badLink");
    else keys.push("settings.myModels.missing.link");
  }
  if (!stems.length) keys.push("settings.myModels.missing.stem");
  if (licence !== true) keys.push("settings.myModels.missing.licence");
  return keys;
}

/** Why Add model is still off, in one line, or "" once it is on. */
export function missingLine(state, t, locale) {
  const keys = missingKeys(state);
  if (!keys.length) return "";
  return t("settings.myModels.missing", { items: listText(keys.map((k) => t(k)), locale) });
}

/** A display name from the link (its repo) or the file name (no extension),
 *  at most MAX_NAME characters. "" when there is neither: the server names it. */
export function nameFrom({ link = "", fileName = "" } = {}) {
  const hf = parseHfLink(link);
  let name = "";
  if (hf) name = hf.file ? hf.file.split("/").pop().replace(/\.[^.]+$/, "") || hf.repo : hf.repo;
  else if (filled(fileName)) {
    const f = String(fileName).trim().split(/[/\\]/).pop();
    name = f.replace(/\.[^.]+$/, "") || f;
  }
  return [...name].slice(0, MAX_NAME).join("").trim();
}

/** The stems ticked, in the form's own order, each once. */
export function orderedStems(stems) {
  const set = new Set(stems || []);
  return ADD_STEMS.filter((s) => set.has(s));
}

/** The JSON body of "Add model" by link. */
export function linkBody({ link = "", stems = [], name = "" } = {}) {
  const body = { link: String(link).trim(), stems: orderedStems(stems), licence_confirmed: true };
  const n = nameFrom({ link }) || String(name).trim();
  if (n) body.name = n;
  return body;
}

/** The text fields of "Add model" by files, beside the two files. */
export function uploadFields({ fileName = "", stems = [] } = {}) {
  const fields = [["stems", JSON.stringify(orderedStems(stems))], ["licence", "true"]];
  const n = nameFrom({ fileName });
  if (n) fields.push(["name", n]);
  return fields;
}

/** "3f9a12...c21e": the first 6 and last 4 digits. */
export function shortSha(hex) {
  const s = String(hex ?? "");
  return s.length > 12 ? `${s.slice(0, 6)}...${s.slice(-4)}` : s;
}

function formatNumber(value, locale, digits) {
  try {
    return new Intl.NumberFormat(locale || undefined, { maximumFractionDigits: digits }).format(value);
  } catch {
    return String(value);
  }
}

/** Bytes in MB, or in GB from 10 GB up; "known after download" when there is none yet. */
export function formatSize(bytes, t, locale) {
  const b = Number(bytes);
  if (!Number.isFinite(b) || b <= 0) return t("settings.myModels.size.unknown");
  const mb = b / 1_000_000;
  if (mb >= 10_000) return t("settings.myModels.size.gb", { n: formatNumber(Math.round(mb / 100) / 10, locale, 1) });
  return t("settings.myModels.size.mb", { n: formatNumber(Math.max(1, Math.round(mb)), locale, 0) });
}

/** "Guitar", "Drums and Bass", in the reader's language. */
export function stemsText(stems, t, locale) {
  return listText((stems || []).map((s) => t(`stem.${s}`)), locale);
}

/** The sentence for a reason code; a code this page does not know gets the general one. */
export function reasonText(code, t) {
  return REASON_CODES.includes(code) ? t(`settings.myModels.reason.${code}`) : t("settings.myModels.reason.unknown");
}

/** The code of a refused request: the body's own, else one read from the HTTP status. */
export function refusalCode(status, body) {
  const code = isObj(body) && typeof body.code === "string" ? body.code : "";
  if (code) return code;
  if (status === 413) return "too_large";
  if (status === 404) return "not_found";
  return "unknown";
}

/** Whether a GET /api/my-models answer is one (an older server has no route). */
export function isMyModelsAnswer(d) {
  return isObj(d) && Array.isArray(d.models);
}

/** Whether this install can run a model the user adds (torch and the
 *  RoFormer classes are there). Only an explicit false says no. */
export function canRunModels(d) {
  return d?.available !== false;
}

/** The entries of an answer that have an id and a name, as the list shows them. */
export function entriesOf(d) {
  if (!isMyModelsAnswer(d)) return [];
  return d.models.filter((m) => isObj(m) && typeof m.id === "string" && m.id !== "");
}

/** Whether an entry is still downloading or being checked. */
export function isActive(entry) {
  return ACTIVE_STATUSES.includes(entry?.status);
}

/** Whether any entry is: the page polls while one is. */
export function anyActive(entries) {
  return (entries || []).some(isActive);
}

/** A whole percent 0..100 from the entry's pct, which the server sends 0..100. */
export function pctOf(entry) {
  const p = Number(entry?.pct);
  return Number.isFinite(p) ? Math.max(0, Math.min(100, Math.round(p))) : 0;
}

/** The share of an entry's pct a link download takes (app/api/my_models.py
 *  _LINK_SHARE); the test run is the rest. */
export const DOWNLOAD_SHARE = 80;

/** How far the model file's download is, 0..100, from the entry's pct. */
export function downloadPct(entry) {
  return Math.min(100, Math.round((pctOf(entry) * 100) / DOWNLOAD_SHARE));
}

/**
 * What a row's status says:
 *   { status, label, reason, pct, active }
 * status is the badge: downloading, checking, ready, refused, missing, unusable.
 * "staging" is an upload the server is still storing: it reads as checking.
 */
export function statusView(entry, t) {
  const status = entry?.status;
  if (status === "downloading") {
    const pct = downloadPct(entry);
    return { status: "downloading", label: t("settings.myModels.status.downloading", { pct }), reason: "", pct, active: true };
  }
  if (status === "checking" || status === "staging") {
    const pct = pctOf(entry);
    return { status: "checking", label: t("settings.myModels.status.checking"), reason: "", pct, active: true };
  }
  // The server sends "missing" as the status of a ready model whose file went.
  if (status === "missing" || (status === "ready" && entry.missing === true)) {
    return { status: "missing", label: t("settings.myModels.status.missing"), reason: reasonText("changed", t), pct: 0, active: false };
  }
  if (status === "ready") {
    if (entry.usable === false) return { status: "unusable", label: t("settings.myModels.status.unusable"), reason: reasonText(entry.code || "unavailable", t), pct: 0, active: false };
    return { status: "ready", label: t("settings.myModels.status.ready"), reason: "", pct: 0, active: false };
  }
  return { status: "refused", label: t("settings.myModels.status.refused"), reason: reasonText(entry?.code, t), pct: 0, active: false };
}

/** The line under a row's name: size, stems and the start of its SHA-256. */
export function metaLine(entry, t, locale) {
  return t("settings.myModels.row.meta", {
    size: formatSize(entry?.size, t, locale),
    stems: stemsText(entry?.stems, t, locale),
    sha: shortSha(entry?.sha256) || "?",
  });
}

/** Whether Check again downloads the model again: a link entry whose files
 *  were never kept (its download failed). That needs online sources on. */
export function needsDownload(entry) {
  return entry?.source?.kind === "hf" && !entry?.sha256 && !isActive(entry);
}

/** Where the model came from, and the licence its source declares, if any. */
export function sourceLine(entry, t) {
  const src = isObj(entry?.source) ? entry.source : {};
  const label = String(src.label || "");
  const parts = [];
  if (label) parts.push(t(src.kind === "hf" ? "settings.myModels.row.source.hf" : "settings.myModels.row.source.upload", { label }));
  if (typeof src.license_declared === "string" && src.license_declared) {
    parts.push(t("settings.myModels.row.licenseDeclared", { license: src.license_declared }));
  }
  return parts.join(" ");
}

/** Which button keeps focus after the row it was on went: the next row's
 *  Remove (the one now at the same place), else the previous one, else none. */
export function focusAfterRemove(index, remaining) {
  if (remaining <= 0) return -1;
  return Math.min(index, remaining - 1);
}

/** Upload progress as a whole percent, or null when the browser cannot tell. */
export function uploadPct(loaded, total) {
  const l = Number(loaded), tot = Number(total);
  if (!Number.isFinite(l) || !Number.isFinite(tot) || tot <= 0) return null;
  return Math.max(0, Math.min(100, Math.floor((l / tot) * 100)));
}
