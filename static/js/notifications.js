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

const FAILURES_KEY = "stemdeck:failures";
// Enough to cover a bad session without letting a crash loop fill the store.
const MAX_FAILURES = 20;
const NEW_ISSUE_URL = "https://github.com/stemdeckapp/stemdeck/issues/new";
// Practical ceiling for a URL handed to a browser or, on Windows, to
// explorer.exe. GitHub itself tolerates more, but nothing here is worth
// risking a silently truncated link for -- the tail is trimmed to fit.
const MAX_URL_LENGTH = 6000;

// What each failure class is called in the report and on the card.
const KIND_LABELS = {
  import: "Import failed",
  playback: "Playback failed",
  export: "Export failed",
  update: "Update check failed",
};

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
 * Build the pre-filled bug-report URL.
 *
 * Pure and exported so the field mapping can be tested without a browser --
 * an OS string that does not match the dropdown option exactly is dropped by
 * GitHub without complaint, which is the kind of bug only a test catches.
 */
export function buildReportUrl(record, diag = {}) {
  const label = KIND_LABELS[record.kind] || "Something failed";
  const title = `[Bug]: ${label}${record.cause ? ` — ${record.cause}` : ""}`;

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

  const tail = Array.isArray(record.tail) ? record.tail : [];
  const build = (tailLines, note) => {
    const parts = [technicalBlock(record, diag)];
    if (tailLines.length) parts.push("", "```", ...tailLines, "```");
    if (note) parts.push("", note);
    const params = new URLSearchParams({
      template: "bug_report.yml",
      title,
      what,
      steps,
      os: osOption(diag.buildTarget),
      version: diag.version ? `v${diag.version}` : "",
      install: installOption(diag.buildTarget, Boolean(diag.isDesktop)),
      extra: parts.join("\n"),
    });
    return `${NEW_ISSUE_URL}?${params}`;
  };

  let url = build(tail, "");
  if (url.length <= MAX_URL_LENGTH) return url;

  // Too long: keep the end of the tail, which is where the actual error is,
  // and point at the full logs rather than silently losing them.
  const note = "_Tail truncated — full logs via Settings → Export logs._";
  for (let keep = Math.min(tail.length, 20); keep > 0; keep--) {
    url = build(tail.slice(-keep), note);
    if (url.length <= MAX_URL_LENGTH) return url;
  }
  return build([], note);
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
    kind: kind in KIND_LABELS ? kind : "import",
    message: String(message || "Something went wrong."),
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

export function clearFailures() {
  failures = [];
  persist();
  render();
}

/** catalog.js tells us whether an update card is showing, for badge/empty. */
export function setReleasePending(pending) {
  releasePending = Boolean(pending);
  render();
}

// ─── Rendering ────────────────────────────────────────────────────────────

function relativeTime(ts) {
  const secs = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (secs < 60) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} h ago`;
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
  title.textContent = KIND_LABELS[record.kind] || "Something failed";
  const desc = document.createElement("div");
  desc.className = "daw-notif-card-desc";
  desc.textContent = `${record.cause || record.message} · ${relativeTime(record.at)}`;
  body.append(title, desc);

  const dismiss = document.createElement("button");
  dismiss.className = "daw-notif-dismiss";
  dismiss.type = "button";
  dismiss.setAttribute("aria-label", "Dismiss notification");
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

  if (titleEl) titleEl.textContent = KIND_LABELS[record.kind] || "Something failed";
  if (whenEl) whenEl.textContent = new Date(record.at).toLocaleString();
  if (msgEl) msgEl.textContent = record.detail ? `${record.message}\n${record.detail}` : record.message;
  if (techEl) techEl.textContent = "Collecting details…";
  if (reportEl) reportEl.removeAttribute("href");

  dialog.classList.remove("hidden");

  // The stderr tail lives in the quarantined error.txt and is fetched now
  // rather than at capture time -- one request when a user actually looks,
  // instead of one per failure whether or not they care.
  if (!record.tail && record.context?.jobId) {
    try {
      const res = await fetch(`/api/jobs/${record.context.jobId}/failure`);
      if (res.ok) {
        const data = await res.json();
        record.tail = Array.isArray(data.tail) ? data.tail : [];
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
      }
    } catch (e) {
      console.warn("[notifications] failure detail fetch failed:", e);
      record.tail = [];
    }
  }

  const diag = await getDiagnostics();
  if (techEl) {
    const tail = record.tail?.length ? `\n\n${record.tail.join("\n")}` : "";
    techEl.textContent = `${technicalBlock(record, diag)}${tail}`;
  }
  // Set at open time; the global external-link handler reads href on click and
  // routes it through Tauri's open_url on desktop, a new tab in a browser.
  if (reportEl) reportEl.href = buildReportUrl(record, diag);
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
