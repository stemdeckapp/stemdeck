// Words a person can read for a failed request or a thrown value.
//
// `new Error(data.detail || res.statusText)` gave an empty message when the
// server's detail was missing or not a string (FastAPI's validation errors are
// a list, and `new Error([])` is "") and the status line had no reason phrase
// (HTTP/2 and HTTP/3 never send one). A rejected fetch can carry no message
// either. Each of those showed "Failed to restore track:" with nothing after it.

/**
 * The reason a non-ok response gives: its string `detail`, the `msg` of each
 * entry of a list `detail`, then the status text, then "HTTP <code>". Empty
 * only when there is no response at all.
 */
export function responseErrorMessage(res, data) {
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
