// playlist.js — importing a whole playlist as one queued batch.
//
// Two steps by design. Expanding a playlist is a network round trip whose
// result the user has to agree to ("this is 47 tracks, still want it?"), and
// queueing 47 jobs from a single click with no warning is not a thing to do to
// someone's machine. The track list is never sent from here: the server expands
// the URL again when creating, so nothing outside its allowlist can be
// smuggled in between the two calls.

import { addPlaylistToLibrary } from "./catalog.js";
import { showError } from "./job.js";
import { t, plural } from "./i18n.js";

/** Does this URL look like a playlist we should offer to expand? Deliberately
 *  permissive -- the server is the authority, this only decides which flow the
 *  submit button takes. */
export function looksLikePlaylist(url) {
  const text = String(url || "").trim();
  if (!/^https?:\/\//i.test(text)) return false;
  let parsed;
  try {
    parsed = new URL(text);
  } catch {
    return false;
  }
  const host = parsed.hostname.toLowerCase().replace(/^(www|m|music)\./, "");
  if (host === "soundcloud.com") return /^\/[^/]+\/sets\/[^/]+\/?$/.test(parsed.pathname);
  if (!/^(youtube\.com|youtube-nocookie\.com)$/.test(host)) return false;
  const list = parsed.searchParams.get("list");
  // RD* is algorithmic radio: endless and per-viewer, so there is no set to
  // import. Let it fall through to the single-track path instead.
  return !!list && !/^RD/i.test(list);
}

function countLine(preview) {
  const parts = [];
  if (preview.skipped_unavailable) parts.push(t("playlist.skip.unavailable", { count: preview.skipped_unavailable }));
  if (preview.skipped_too_long) parts.push(t("playlist.skip.tooLong", { count: preview.skipped_too_long }));
  const overflow =
    preview.total_found - preview.skipped_unavailable - preview.skipped_too_long - preview.will_queue;
  if (overflow > 0) parts.push(t("playlist.skip.overflow", { count: overflow }));
  if (preview.truncated) parts.push(t("playlist.skip.truncated", { cap: preview.cap }));
  if (!parts.length) return "";
  return t("playlist.skippingPrefix", { list: parts.join(t("playlist.listSep")) });
}

let openDialog = null;

function closeDialog() {
  openDialog?.remove();
  openDialog = null;
}

function confirmImport(preview) {
  return new Promise((resolve) => {
    closeDialog();
    const overlay = document.createElement("div");
    overlay.className = "reset-confirm-backdrop playlist-confirm";
    const skipped = countLine(preview);
    const confirmBody = plural("playlist.confirmBody", preview.will_queue, { skipped: skipped ? ` ${skipped}` : "" });
    overlay.innerHTML = `
      <div class="reset-confirm-card" role="dialog" aria-modal="true" aria-label="${t("playlist.importAriaLabel")}">
        <div class="reset-confirm-title playlist-confirm-title">${t("playlist.confirmTitle")}</div>
        <p class="reset-confirm-body">
          <strong class="playlist-name"></strong><br />
          ${confirmBody}
        </p>
        <div class="reset-confirm-actions">
          <button class="reset-confirm-cancel" type="button">${t("playlist.cancel")}</button>
          <button class="playlist-confirm-go" type="button">${plural("playlist.import", preview.will_queue)}</button>
        </div>
      </div>
    `;
    // textContent, not innerHTML: the title comes from an external service.
    overlay.querySelector(".playlist-name").textContent = preview.playlist_title;

    const finish = (value) => { closeDialog(); resolve(value); };
    overlay.querySelector(".reset-confirm-cancel").addEventListener("click", () => finish(false));
    overlay.querySelector(".playlist-confirm-go").addEventListener("click", () => finish(true));
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) finish(false); });
    document.addEventListener("keydown", function onKey(e) {
      if (e.code !== "Escape") return;
      document.removeEventListener("keydown", onKey);
      finish(false);
    });

    document.body.appendChild(overlay);
    openDialog = overlay;
    overlay.querySelector(".playlist-confirm-go").focus();
  });
}

/** Expand, confirm, queue, and file the results under a folder named after the
 *  playlist. Returns the number of tracks queued (0 if cancelled or refused). */
export async function importPlaylist(url, stems) {
  let preview;
  try {
    const res = await fetch("/api/playlist/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    preview = await res.json();
    if (!res.ok) throw new Error(preview.detail || res.statusText);
  } catch (err) {
    showError(t("playlist.couldNotRead", { message: err.message }));
    return 0;
  }

  if (!preview.will_queue) {
    showError(
      preview.capacity_left === 0
        ? t("playlist.queueFull")
        : t("playlist.nothingImportable"),
      countLine(preview) || null,
      { retry: false },
    );
    return 0;
  }

  if (!(await confirmImport(preview))) return 0;

  let result;
  try {
    const res = await fetch("/api/playlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, stems }),
    });
    result = await res.json();
    if (!res.ok) throw new Error(result.detail || res.statusText);
  } catch (err) {
    showError(t("playlist.couldNotImport", { message: err.message }));
    return 0;
  }

  addPlaylistToLibrary(result.playlist_title, result.jobs || []);
  return result.queued || 0;
}
