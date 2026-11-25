// Crash reports (desktop app only, off by default; docs/privacy.md).
//
// The Rust shell owns the switch, the consent record and the reporter
// (desktop/src-tauri/src/telemetry.rs). This module only:
//  - forwards the page's uncaught errors and unhandled rejections to the shell
//    while reports are on, through the `report_web_error` command;
//  - reads and changes the switch for Settings, Privacy.
//
// The shell checks consent again for every forwarded error and scrubs it, so
// this page being wrong about the switch can never send anything. The checks
// here keep an error from even leaving the page while reports are off, and
// keep a loop of errors from flooding the shell.
//
// The pure parts (what is forwarded, the rate limit, the duplicate check) are
// pinned by tests/js/crash-reports.test.mjs without a browser.

/**
 * The version of the consent text in Settings, Privacy. Bump it together with
 * CONSENT_VERSION in telemetry.rs whenever that text changes what is
 * collected: a stored consent of another version reads as off.
 */
export const CRASH_CONSENT_VERSION = 1;

export const MAX_MESSAGE_CHARS = 1000;
export const MAX_STACK_CHARS = 8000;
export const MAX_NAME_CHARS = 100;
export const MAX_PER_MINUTE = 10;
const MAX_SEEN = 256;

const cut = (value, max) => String(value ?? "").slice(0, max);

/**
 * What is forwarded for one error: its kind ("error" or "rejection"), the
 * error's name, message and stack, each cut to size. Nothing else from the
 * page: no address, no file name from the event, no state.
 */
export function webReportFrom(kind, source) {
  const err = kind === "rejection" ? source?.reason : source?.error;
  const isError = err && typeof err === "object";
  const message = isError
    ? err.message ?? ""
    : kind === "rejection"
      ? err
      : source?.message ?? err ?? "";
  return {
    kind: kind === "rejection" ? "rejection" : "error",
    name: cut(isError ? err.name || "Error" : "Error", MAX_NAME_CHARS),
    message: cut(message, MAX_MESSAGE_CHARS),
    stack: cut(isError ? err.stack : "", MAX_STACK_CHARS),
  };
}

/** The same report twice is one report: kind, name, message, first frame. */
export function reportKey(report) {
  const firstFrame = String(report.stack || "")
    .split("\n")
    .map((l) => l.trim())
    .find((l) => l.startsWith("at ") || l.includes("@")) || "";
  return [report.kind, report.name, report.message, firstFrame].join("|");
}

/**
 * A guard around `send`: forwards a report only while `isOn()`, at most
 * `maxPerMinute` in any rolling minute, and each distinct report once per
 * page load. Returns the function to call with a report; it answers whether
 * the report was forwarded.
 */
export function createForwarder({ send, isOn = () => true, now = () => Date.now(), maxPerMinute = MAX_PER_MINUTE } = {}) {
  const sentAt = [];
  const seen = new Set();
  return (report) => {
    if (!isOn()) return false;
    const t = now();
    while (sentAt.length && t - sentAt[0] >= 60_000) sentAt.shift();
    const key = reportKey(report);
    if (seen.has(key) || seen.size >= MAX_SEEN) return false;
    if (sentAt.length >= maxPerMinute) return false;
    seen.add(key);
    sentAt.push(t);
    try {
      send(report);
    } catch (e) {
      console.warn("[crash reports] could not forward an error:", e);
    }
    return true;
  };
}

// ── Page wiring ──

let reportingOn = false;
const listeners = new Set();

const tauriInvoke = () => window.__TAURI__?.core?.invoke;

/** Whether this page runs inside the desktop app, where reports exist at all. */
export function crashReportsAvailable() {
  return Boolean(tauriInvoke());
}

/** The page's copy of the switch. */
export function crashReportsOn() {
  return reportingOn;
}

/** Call `fn(on)` on every change of the switch. Returns the function that stops it. */
export function onCrashReportsChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function remember(status) {
  const next = status?.on === true;
  if (next !== reportingOn) {
    reportingOn = next;
    for (const fn of listeners) {
      try {
        fn(reportingOn);
      } catch (e) {
        console.warn("[crash reports] listener failed:", e);
      }
    }
  }
  return status;
}

/** The shell's status: { on, version, givenAt, lastReport }, or null outside the desktop app. */
export async function readCrashReports() {
  const invoke = tauriInvoke();
  if (!invoke) return null;
  return remember(await invoke("crash_reports_status"));
}

/**
 * The example report: a made-up error put through the shell's real scrubbing,
 * as JSON text. Read-only, works with reports off, sends nothing. Null
 * outside the desktop app.
 */
export async function readCrashReportExample() {
  const invoke = tauriInvoke();
  if (!invoke) return null;
  return invoke("crash_report_example");
}

/** Turn reports on or off. Resolves to the new status; rejects when it was not saved. */
export async function saveCrashReports(on) {
  const invoke = tauriInvoke();
  if (!invoke) return null;
  return remember(await invoke("set_crash_reports", { on: Boolean(on), version: CRASH_CONSENT_VERSION }));
}

let installed = false;

/**
 * Forward uncaught errors to the shell while reports are on. A no-op outside
 * the desktop app, so a server, Docker or a browser never forwards anything.
 */
export function installCrashForwarding() {
  const invoke = tauriInvoke();
  if (!invoke || installed) return;
  installed = true;
  const forward = createForwarder({
    isOn: () => reportingOn,
    send: (report) => {
      invoke("report_web_error", { report }).catch((e) => console.warn("[crash reports] the shell refused an error:", e));
    },
  });
  window.addEventListener("error", (e) => {
    // A resource that failed to load (an image, a script) is not a crash.
    if (!(e instanceof ErrorEvent)) return;
    forward(webReportFrom("error", e));
  });
  window.addEventListener("unhandledrejection", (e) => forward(webReportFrom("rejection", e)));
  readCrashReports().catch((e) => console.warn("[crash reports] could not read the setting:", e));
}
