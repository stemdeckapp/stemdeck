// ─── Notification centre ───
//
// Until now the bell held exactly one hardcoded card ("New release available")
// and the badge/empty-state were toggled inline at its two call sites. This
// module owns the panel instead: any number of cards, persisted across
// reloads, each one openable into a dialog that can hand the failure to GitHub
// as a pre-filled bug report.
//
// Why persist: a failure used to live in a transient #error banner. Dismiss it
// (or reload) and the evidence was gone -- which is exactly the position issue
// #359 complained about, where a reporter has nothing to paste and guesses at
// a cause instead. A failure the user scrolled past is still reportable here.

import { storeGet, storeSet } from "./utils.js";
import { t, plural } from "./i18n.js";

const FAILURES_KEY = "stemdeck:failures";
// Enough to cover a bad session without letting a crash loop fill the store.
const MAX_FAILURES = 20;
const NEW_ISSUE_URL = "https://github.com/stemdeckapp/stemdeck/issues/new";
// Support channel for a quicker back-and-forth than a public issue -- same
// invite as the About dialog's Discord icon (index.html).
const DISCORD_URL = "https://discord.gg/YhCKsjhcwB";
// Named views the Settings -> Logs viewer offers (app/main.py _LOG_VIEWS).
// Opt-in only (fetchRecentLogs): unlike the per-job traceback, these cover
// more than one failure and are worth a deliberate click, not an automatic
// fetch every time the dialog opens.
const LOG_VIEWS = ["backend", "application", "setup"];

// What each failure class is called in the report and on the card. A
// function, not a static object -- the labels must reflect whatever language
// is active *now*, at render time, not whatever was active when this module
// first loaded.
const KIND_KEYS = {
  import: "notifKind.importFailed",
  playback: "notifKind.playbackFailed",
  export: "notifKind.exportFailed",
  update: "notifKind.updateCheckFailed",
};
function kindLabel(kind) {
  return kind in KIND_KEYS ? t(KIND_KEYS[kind]) : t("notifKind.default");
}

let failures = [];
// Set by catalog.js when an update is pending; owned here so the badge and the
// empty state have a single source of truth.
let releasePending = false;
// Injected at init so this module never imports catalog.js (which imports it).
let getDiagnostics = async () => ({});

// ─── Report URL ───────────────────────────────────────────────────────────
//
// The bug form is a GitHub issue form, so its field ids can be prefilled from
// the query string. `preflight` is deliberately absent: checkboxes cannot be
// prefilled, so the user still confirms they are on the latest release and
// searched for duplicates. blank_issues_enabled is false in the repo config,
// so `template` is mandatory rather than decorative.

/** The exact string from the form's OS dropdown, or prefill silently no-ops. */
export function osOption(target) {
  if (!target) return "Other";
  if (target.os === "macos") return target.arch === "arm64" ? "macOS (Apple Silicon)" : "macOS (Intel)";
  if (target.os === "windows") return "Windows";
  if (target.os === "linux") return "Linux";
  return "Other";
}

/** Likewise for "How did you install it?". No Tauri means it is served. */
export function installOption(target, isDesktop) {
  if (!isDesktop) return "Docker / self-hosted";
  if (target?.os === "macos") return "macOS DMG";
  if (target?.os === "windows") return "Windows ZIP";
  if (target?.os === "linux") return "Linux tar.gz";
  return "From source";
}

function technicalBlock(record, diag) {
  const ctx = record.context || {};
  const rows = [
    ["StemDeck", diag.version ? `v${diag.version}` : "(unknown)"],
    ["Failure", `${record.kind}${record.cause ? ` / ${record.cause}` : ""}`],
    ["Stage", ctx.stage],
    ["Device", ctx.device && ctx.gpuFallback ? `${ctx.device} (fell back to CPU)` : ctx.device],
    ["Model", ctx.model || diag.model],
    ["Engine", ctx.engine],
    ["ffmpeg", diag.ffmpegConfigured === undefined ? undefined : String(diag.ffmpegConfigured)],
    ["Timings", ctx.timings],
  ];
  return rows
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${k}: ${v}`)
    .join("\n");
}

/**
 * The technical block plus the full (untruncated) stderr tail and backend
 * traceback, for copying to the clipboard rather than the URL -- and for the
 * dialog's own on-screen preview, so what's shown matches what gets pasted.
 * Pure and exported for the same reason as buildReportUrl.
 */
export function buildReportText(record, diag = {}) {
  const tail = Array.isArray(record.tail) ? record.tail : [];
  const tb = Array.isArray(record.traceback) ? record.traceback : [];
  const parts = [technicalBlock(record, diag)];
  if (tail.length) parts.push("", "Stderr tail:", "```", ...tail, "```");
  if (tb.length) parts.push("", "Traceback:", "```", ...tb, "```");
  if (record.logs) {
    for (const view of LOG_VIEWS) {
      const text = record.logs[view];
      if (text) parts.push("", `Recent ${view} log:`, "```", text, "```");
    }
  }
  return parts.join("\n");
}

// Windows opens this URL via explorer.exe, which silently falls back to a
// plain File Explorer window past roughly 2000 characters rather than
// erroring, instead of opening GitHub (the bug this whole scheme guards
// against). Kept a healthy margin under that so the fill reliably lands.
// Exported so the length-ceiling test can check against the real value
// rather than a duplicated magic number.
export const SAFE_URL_CHARS = 1800;

/**
 * Build the pre-filled bug-report URL, filling the "Logs / screenshots"
 * field directly with as much of the traceback/stderr tail as safely fits --
 * no paste needed for the common case.
 *
 * What doesn't fit (a long trace, or anything from the opt-in "include
 * recent logs" button, which never goes in the URL at all -- it can be far
 * larger than a URL should ever carry) falls back to the clipboard, which
 * buildReportText always has the complete version of; a note in the field
 * says so. Truncation keeps the END of the trace, since that is where the
 * actual failure line is.
 *
 * Pure and exported so the field mapping can be tested without a browser --
 * an OS string that does not match the dropdown option exactly is dropped by
 * GitHub without complaint, which is the kind of bug only a test catches.
 */
export function buildReportUrl(record, diag = {}) {
  const label = kindLabel(record.kind);
  const title = `[Bug]: ${label}${record.cause ? ` - ${record.cause}` : ""}`;

  const what = [
    `${label} in StemDeck.`,
    "",
    `**StemDeck said:** ${record.message || "(no message)"}`,
    record.detail ? `**Detail:** ${record.detail}` : null,
    "",
    "<!-- Anything else worth knowing? What were you working on? -->",
  ].filter((l) => l !== null).join("\n");

  const steps = [
    "1. <!-- what were you doing? -->",
    record.context?.stage ? `2. StemDeck failed at: ${record.context.stage}` : "2. It failed.",
  ].join("\n");

  const base = {
    template: "bug_report.yml",
    title,
    what,
    steps,
    os: osOption(diag.buildTarget),
    version: diag.version ? `v${diag.version}` : "",
    install: installOption(diag.buildTarget, Boolean(diag.isDesktop)),
  };
  const toUrl = (extra) => `${NEW_ISSUE_URL}?${new URLSearchParams({ ...base, extra })}`;

  const tech = technicalBlock(record, diag);
  const hasTraceback = Array.isArray(record.traceback) && record.traceback.length > 0;
  const source = hasTraceback ? record.traceback : Array.isArray(record.tail) ? record.tail : [];
  const sourceLabel = hasTraceback ? "Traceback" : "Stderr tail";
  const hasLogs = Boolean(record.logs) && Object.values(record.logs).some(Boolean);
  const pasteNote =
    "_Full report (including any traceback beyond what fits here, and any recent logs you " +
    "included) is on your clipboard from this click - paste to see everything._";

  const build = (keep) => {
    const parts = [tech];
    if (keep > 0) {
      const clipped = keep < source.length;
      parts.push("", `${sourceLabel}${clipped ? " (end)" : ""}:`, "```", ...source.slice(-keep), "```");
    }
    if (keep < source.length || hasLogs) parts.push("", pasteNote);
    return toUrl(parts.join("\n"));
  };

  let url = build(source.length);
  if (url.length <= SAFE_URL_CHARS) return url;
  for (let keep = Math.min(source.length, 60); keep > 0; keep--) {
    url = build(keep);
    if (url.length <= SAFE_URL_CHARS) return url;
  }
  url = build(0);
  if (url.length <= SAFE_URL_CHARS) return url;
  // Pathological case: even the bare technical block didn't fit (an
  // enormous "what"/"steps"). Fall back to clipboard-only, same as the
  // original fix for this bug.
  return toUrl("Copied to your clipboard - paste it into this field.");
}

// ─── Records ──────────────────────────────────────────────────────────────

function nowId() {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

// classify_failure() returns the literal string "unknown" when it cannot place
// a failure. That is a backend sentinel, not a description: shown to a user it
// reads as "Import failed — unknown", and in an issue title it groups every
// unclassified failure under one meaningless heading. Drop it and let the real
// message speak instead.
function cleanCause(value) {
  const text = String(value ?? "").trim();
  return text && text.toLowerCase() !== "unknown" ? text : null;
}

async function persist() {
  await storeSet(FAILURES_KEY, failures);
}

/**
 * Record a failure and surface it in the notification centre.
 *
 * Called at real failure sites only -- deliberately not wired into showError,
 * which also carries benign validation ("All stems are muted - nothing to
 * export."). A bug report about that would waste everyone's time.
 */
export function notifyFailure({ kind, message, detail = null, cause = null, context = {} } = {}) {
  const record = {
    id: nowId(),
    at: Date.now(),
    kind: kind in KIND_KEYS ? kind : "import",
    message: String(message || t("notif.somethingWentWrong")),
    detail: cleanCause(detail) ? String(detail) : null,
    // The backend's classified cause (out-of-memory / disk-full / bad-input /
    // unsupported-device) when error_detail carries one -- it leads the issue
    // title, so duplicates group by cause rather than by wording.
    cause: cleanCause(cause) || (detail ? cleanCause(String(detail).split("—")[0]) : null),
    context,
  };

  // One failure, one card. A failed job is reported by whichever path noticed
  // it -- the foreground SSE handler, the background queue reconciler, or both
  // -- and applyState can run the error branch on more than one frame. Key on
  // the job id where there is one, so the later sighting refreshes the record
  // instead of stacking a duplicate the user has to dismiss twice.
  const dupe = failures.findIndex((f) =>
    f.kind === record.kind &&
    (record.context?.jobId
      ? f.context?.jobId === record.context.jobId
      : f.message === record.message && record.at - f.at < 5000),
  );
  if (dupe !== -1) {
    // Keep whichever sighting carried the richer detail.
    const prev = failures[dupe];
    record.id = prev.id;
    record.detail = record.detail || prev.detail;
    record.cause = record.cause || prev.cause;
    record.context = { ...prev.context, ...record.context };
    record.tail = record.tail || prev.tail;
    failures.splice(dupe, 1);
  }

  failures.unshift(record);
  failures = failures.slice(0, MAX_FAILURES);
  persist();
  render();
  return record;
}

export function dismissFailure(id) {
  failures = failures.filter((f) => f.id !== id);
  persist();
  render();
}

/** Clear failures tied to a specific job/track id, because whatever the
 * failure was about reached a resolved state -- retried successfully, or
 * ceased to exist. kind=null clears every kind for that id (the id itself
 * is gone, so any failure tagged with it is moot); a specific kind clears
 * only that action's record, so e.g. a resolved playback failure can't
 * silently swallow an unresolved import failure for the same track. */
export function dismissFailuresByJobId(jobId, kind = null) {
  if (!jobId) return;
  const before = failures.length;
  failures = failures.filter((f) => !(f.context?.jobId === jobId && (kind === null || f.kind === kind)));
  if (failures.length === before) return; // nothing changed -- skip the write/render
  persist();
  render();
}

/** Clear id-less failures of one kind (update checks, log exports -- neither
 * is ever tied to a job). Never touches a failure that carries a jobId, even
 * if it shares this kind -- a per-track export failure can only be cleared
 * via dismissFailuresByJobId for that track. */
export function dismissFailuresByKind(kind) {
  const before = failures.length;
  failures = failures.filter((f) => !(f.kind === kind && !f.context?.jobId));
  if (failures.length === before) return;
  persist();
  render();
}

export function clearFailures() {
  failures = [];
  persist();
  render();
}

/** Test-only accessor: `failures` is module-private and render() writes to a
 * DOM that doesn't exist under plain Node, so this is the only way a test can
 * assert on notification state. */
export function getFailures() {
  return failures;
}

/** catalog.js tells us whether an update card is showing, for badge/empty. */
export function setReleasePending(pending) {
  releasePending = Boolean(pending);
  render();
}

// ─── Rendering ────────────────────────────────────────────────────────────

function relativeTime(ts) {
  const secs = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (secs < 60) return t("time.justNow");
  const mins = Math.round(secs / 60);
  if (mins < 60) return plural("time.minAgo", mins);
  const hours = Math.round(mins / 60);
  if (hours < 24) return plural("time.hoursAgo", hours);
  return new Date(ts).toLocaleDateString();
}

function iconSvg() {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("width", "14");
  svg.setAttribute("height", "14");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.9");
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", "M12 9v4 M12 17h.01 M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z");
  svg.appendChild(path);
  return svg;
}

function buildCard(record) {
  const card = document.createElement("div");
  card.className = "daw-notif-card daw-notif-error";
  card.dataset.failureId = record.id;
  card.setAttribute("role", "button");
  card.setAttribute("tabindex", "0");

  const icon = document.createElement("div");
  icon.className = "daw-notif-card-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.appendChild(iconSvg());

  const body = document.createElement("div");
  body.className = "daw-notif-card-body";
  const title = document.createElement("div");
  title.className = "daw-notif-card-title";
  title.textContent = kindLabel(record.kind);
  const desc = document.createElement("div");
  desc.className = "daw-notif-card-desc";
  desc.textContent = `${record.cause || record.message} · ${relativeTime(record.at)}`;
  body.append(title, desc);

  const dismiss = document.createElement("button");
  dismiss.className = "daw-notif-dismiss";
  dismiss.type = "button";
  dismiss.setAttribute("aria-label", t("notif.dismiss"));
  dismiss.textContent = "×";
  dismiss.addEventListener("click", (e) => {
    e.stopPropagation();
    dismissFailure(record.id);
  });

  const open = () => openFailureDialog(record);
  card.addEventListener("click", open);
  card.addEventListener("keydown", (e) => {
    if (e.code === "Enter" || e.code === "Space") { e.preventDefault(); open(); }
  });

  card.append(icon, body, dismiss);
  return card;
}

/** The one place the badge and empty state are decided, for every card kind. */
export function render() {
  const list = document.getElementById("notifList");
  const badge = document.getElementById("notifBadge");
  const empty = document.getElementById("notifEmpty");
  if (!list) return;

  for (const el of list.querySelectorAll(".daw-notif-error")) el.remove();
  const anchor = list.firstChild;
  for (const record of failures) list.insertBefore(buildCard(record), anchor);

  const any = failures.length > 0 || releasePending;
  badge?.classList.toggle("hidden", !any);
  empty?.classList.toggle("hidden", any);
}

// ─── Detail dialog ────────────────────────────────────────────────────────

async function openFailureDialog(record) {
  const dialog = document.getElementById("failureDialog");
  if (!dialog) return;
  const titleEl = document.getElementById("failureTitle");
  const whenEl = document.getElementById("failureWhen");
  const msgEl = document.getElementById("failureMessage");
  const techEl = document.getElementById("failureTech");
  const reportEl = document.getElementById("failureReport");
  const discordEl = document.getElementById("failureReportDiscord");
  const logsBtn = document.getElementById("failureIncludeLogs");

  if (titleEl) titleEl.textContent = kindLabel(record.kind);
  if (whenEl) whenEl.textContent = new Date(record.at).toLocaleString();
  if (msgEl) msgEl.textContent = record.detail ? `${record.message}\n${record.detail}` : record.message;
  if (techEl) techEl.textContent = t("failure.collecting");
  if (reportEl) reportEl.removeAttribute("href");
  if (discordEl) discordEl.href = DISCORD_URL;
  if (logsBtn) {
    delete record.logs;
    logsBtn.disabled = false;
    logsBtn.textContent = t("failure.includeLogs");
  }

  dialog.classList.remove("hidden");

  // The stderr tail and full traceback live in the quarantined error.txt and
  // are fetched now rather than at capture time -- one request when a user
  // actually looks, instead of one per failure whether or not they care.
  // Both are already home-directory-redacted server-side (redact_home) before
  // this response is built, since this is headed for a public report.
  if (!record.tail && record.context?.jobId) {
    try {
      const res = await fetch(`/api/jobs/${record.context.jobId}/failure`);
      if (res.ok) {
        const data = await res.json();
        record.tail = Array.isArray(data.tail) ? data.tail : [];
        record.traceback = Array.isArray(data.traceback) ? data.traceback : [];
        record.cause = record.cause || cleanCause(data.cause);
        record.context = {
          ...record.context,
          stage: record.context.stage || data.stage,
          device: record.context.device || data.device,
          model: data.model,
          timings: record.context.timings || data.timings,
        };
        persist();
      } else {
        record.tail = [];
        record.traceback = [];
      }
    } catch (e) {
      console.warn("[notifications] failure detail fetch failed:", e);
      record.tail = [];
      record.traceback = [];
    }
  }

  const diag = await getDiagnostics();
  if (techEl) techEl.textContent = buildReportText(record, diag);
  // One shared payload: the logs button mutates record.logs in place, and the
  // report buttons must see that mutation on their next click without a
  // separate re-sync step.
  const payload = { record, diag };
  // Set at open time; the global external-link handler reads href on click and
  // routes it through Tauri's open_url on desktop, a new tab in a browser.
  // The click handlers wired in wireFailureDialog read record/diag back off
  // this element's dataset to copy the full report to the clipboard - the
  // reason the URL itself can stay short (see buildReportUrl).
  if (reportEl) {
    reportEl.href = buildReportUrl(record, diag);
    reportEl._reportPayload = payload;
  }
  if (discordEl) discordEl._reportPayload = payload;
  if (logsBtn) logsBtn._reportPayload = payload;
}

/** Opt-in: the notification centre never fetches these on its own. Scoped to
 * the failure's own timestamp (plus a small buffer) rather than a blind "last
 * hour", since a user can open this dialog long after the failure happened.
 * Already home-directory-redacted server-side (redact_home in main.py). */
async function fetchRecentLogs(record) {
  const minutes = Math.min(1440, Math.max(15, Math.ceil((Date.now() - record.at) / 60000) + 5));
  const logs = {};
  for (const view of LOG_VIEWS) {
    try {
      const res = await fetch(`/api/logs/${view}?minutes=${minutes}`);
      logs[view] = res.ok ? (await res.text()).trim() : "";
    } catch (e) {
      console.warn(`[notifications] failed to fetch the ${view} log:`, e);
      logs[view] = "";
    }
  }
  return logs;
}

/** Full diagnostic text is only useful pasted somewhere, so both report
 * buttons copy it to the clipboard on click - the GitHub link's `extra`
 * field says as much, and Discord has nowhere to prefill at all. */
async function copyReportToClipboard(el) {
  const payload = el._reportPayload;
  if (!payload) return;
  try {
    await navigator.clipboard.writeText(buildReportText(payload.record, payload.diag));
  } catch (e) {
    console.warn("[notifications] clipboard write failed:", e);
  }
}

function wireFailureDialog() {
  const dialog = document.getElementById("failureDialog");
  const close = document.getElementById("failureClose");
  if (!dialog) return;
  const hide = () => dialog.classList.add("hidden");
  close?.addEventListener("click", hide);
  dialog.addEventListener("mousedown", (e) => { if (e.target === dialog) hide(); });
  dialog.addEventListener("keydown", (e) => { if (e.code === "Escape") hide(); });
  document.getElementById("notifClearAll")?.addEventListener("click", (e) => {
    e.stopPropagation();
    clearFailures();
  });
  document.getElementById("failureReport")?.addEventListener("click", (e) => copyReportToClipboard(e.currentTarget));
  document.getElementById("failureReportDiscord")?.addEventListener("click", (e) => copyReportToClipboard(e.currentTarget));
  document.getElementById("failureIncludeLogs")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const payload = btn._reportPayload;
    if (!payload || btn.disabled) return;
    btn.disabled = true;
    btn.textContent = t("failure.fetchingLogs");
    payload.record.logs = await fetchRecentLogs(payload.record);
    persist();
    const techEl = document.getElementById("failureTech");
    if (techEl) techEl.textContent = buildReportText(payload.record, payload.diag);
    btn.textContent = t("failure.logsIncluded");
  });
}

export async function initNotifications({ diagnostics } = {}) {
  if (typeof diagnostics === "function") getDiagnostics = diagnostics;
  wireFailureDialog();
  const stored = await storeGet(FAILURES_KEY, []);
  // Merge rather than assign: reading the store is async, and a failure
  // recorded while it was in flight would otherwise be overwritten by the
  // older list -- losing the one notification the user is about to look for.
  const restored = Array.isArray(stored) ? stored : [];
  const live = new Set(failures.map((f) => f.id));
  failures = [...failures, ...restored.filter((f) => !live.has(f.id))]
    .sort((a, b) => b.at - a.at)
    .slice(0, MAX_FAILURES);
  render();
}
