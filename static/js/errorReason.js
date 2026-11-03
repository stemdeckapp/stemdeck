// Words a person can read for a failed request or a thrown value.
//
// `new Error(data.detail || res.statusText)` gave an empty message when the
// server's detail was missing or not a string (FastAPI's validation errors are
// a list, and `new Error([])` is "") and the status line had no reason phrase
// (HTTP/2 and HTTP/3 never send one). A rejected fetch can carry no message
// either. Each of those showed "Failed to restore track:" with nothing after it.

import { t } from "./i18n.js";

// The server's code for a write it refuses after the stems folder moved and
// before the restart (refuse_writes_until_restart in app/main.py).
export const RESTART_REQUIRED = "restart_required";
// The server's code for a delete, a library move or a reset it refuses while a
// stem fill or a vocal split runs (refuse_while_busy in app/main.py).
export const JOB_BUSY = "job_busy";

/** The translated sentence for a code this side knows, or "". */
export function codeMessage(data) {
  if (data?.code === RESTART_REQUIRED) return t("settings.stemsLocation.restartToSave");
  if (data?.code === JOB_BUSY) return t("library.songBusy");
  return "";
}

/**
 * The reason a non-ok response gives: the translated sentence for a code this
 * side knows, its string `detail`, the `msg` of each entry of a list `detail`,
 * then the status text, then "HTTP <code>". Empty only when there is no
 * response at all.
 */
export function responseErrorMessage(res, data) {
  const known = codeMessage(data);
  if (known) return known;
  const detail = data?.detail;
  if (typeof detail === "string" && detail.trim()) return detail.trim();
  if (Array.isArray(detail)) {
    const parts = detail
      .map((entry) => (typeof entry === "string" ? entry : entry?.msg))
      .filter((msg) => typeof msg === "string" && msg.trim())
      .map((msg) => msg.trim());
    if (parts.length) return parts.join("; ");
  }
  const statusText = typeof res?.statusText === "string" ? res.statusText.trim() : "";
  if (statusText) return statusText;
  return Number.isInteger(res?.status) && res.status > 0 ? `HTTP ${res.status}` : "";
}

/**
 * The text of a caught value: its message, or "" when there is nothing to
 * show, and the caller then shows a sentence of its own. An exception's name
 * ("TypeError") is not a reason a person can act on, so it is never shown.
 */
export function errorReason(err) {
  if (typeof err === "string") return err.trim();
  return typeof err?.message === "string" ? err.message.trim() : "";
}

// The causes classify_failure gives a failed job (app/pipeline/errors.py), and
// the sentence that says each one with its next step (#175).
const CAUSE_KEYS = {
  "out-of-memory": "job.cause.outOfMemory",
  "unsupported-device": "job.cause.unsupportedDevice",
  "disk-full": "job.cause.diskFull",
  "source-blocked": "job.cause.sourceBlocked",
  "source-unavailable": "job.cause.sourceUnavailable",
  "bad-input": "job.cause.badInput",
  unknown: "job.cause.unknown",
};

/**
 * A failed job's error_detail, "<cause> <dash> <words>" as
 * _quarantine_failed_job in app/pipeline/runner.py writes it, split into the
 * cause and the server's own words, which it has already redacted. A detail
 * that does not start with a known cause has cause "" and is all words.
 */
export function failureCause(detail) {
  const text = typeof detail === "string" ? detail.trim() : "";
  const m = /^([a-z-]+)(?:\s+\S\s+([\s\S]*))?$/.exec(text);
  if (m && Object.prototype.hasOwnProperty.call(CAUSE_KEYS, m[1])) return { cause: m[1], message: (m[2] || "").trim() };
  return { cause: "", message: text };
}

/**
 * What a failed import says: its cause as a sentence in the chosen language,
 * with the server's words as the detail line under it. An upload adds that
 * the file has to be added again, because there is no link to fetch it from.
 * A failure the pipeline did not classify keeps the server's own message.
 */
export function importFailure(state) {
  const { cause, message } = failureCause(state?.error_detail);
  if (!cause) return { message: state?.error || t("job.audioProcessingFailed"), detail: message };
  const sentence = t(CAUSE_KEYS[cause]);
  return {
    message: String(state.source_url || "").startsWith("local:") ? t("job.uploadFailed", { cause: sentence }) : sentence,
    detail: message,
  };
}
