// Settings > Models, "Built-in models": every model StemDeck comes with,
// grouped by what it is for (Separation, Transcription, Lyrics and beat grid),
// with its licence, its source, whether it is downloaded and the space it
// takes, and what it is used for now. A model that is not (all) downloaded
// has a Download button beside its state; while it runs the state says
// "Downloading X of Y MB" and the button is Cancel (app/core/model_get.py
// does the download, one at a time). Nothing here removes a model.
// "My models" below it is myModels.js, wired by separationSettings.js.
//
// The group is static HTML in catalog.js (".models-builtin-group"), hidden
// until GET /api/models answers: an older server has no route and no group.
// The words come from the pure modelsView.js.
//
// "Used for" follows the choices in the Separation and Transcription tabs:
// every settings answer catalog.js applies is announced on the overlay as
// "stemdeck:settings-applied" (the Transcription tab's saves too), and a
// saved model choice is announced on the document as STEM_MODELS_CHANGED_EVENT.
// Only the "Used for" lines repaint for that. The list itself is read once
// per opening, and again when a download ends, so its state and size are the
// server's.

import { t, getLanguage } from "./i18n.js";
import { SETTINGS_APPLIED_EVENT } from "./separationSettings.js";
import { STEM_MODELS_CHANGED_EVENT } from "./laneModelView.js";
import {
  DOWNLOAD_POLL_FAILS,
  DOWNLOAD_POLL_MS,
  canDownload,
  downloadErrorText,
  downloadRunning,
  downloadView,
  groupByPurpose,
  hasPlan,
  isDownloadState,
  isModelsAnswer,
  licenceLine,
  purposeLine,
  sourceView,
  stateView,
  usedForLine,
} from "./modelsView.js";
import { linkedLine } from "./sourceLink.js";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export async function wireModelsSettings(overlay) {
  const group = overlay?.querySelector(".models-builtin-group");
  const list = group?.querySelector(".models-inventory");
  if (!group || !list) return;

  let answer = null;
  let settings = null;
  // The Download button's run, as the server last said it.
  let dl = null;
  // A start the server refused, said on its row until the next press.
  let refused = null; // { id, text }
  let pollTimer = null;
  let pollFails = 0;

  // One row per model. Every word is text: a name a deployment set (a
  // Karaoke model's file name) is never read as HTML.
  const buildRow = (m) => {
    const locale = getLanguage();
    const row = el("div", "settings-row models-row");
    row.dataset.model = m.id;
    row.dataset.group = m.group;
    const text = el("div", "settings-row-text");
    text.append(
      el("div", "settings-row-title models-name", m.name),
      el("div", "settings-row-desc models-purpose", purposeLine(m, t, locale)),
      el("div", "settings-row-desc models-licence", licenceLine(m, t)),
      linkedLine("settings-row-desc models-source", sourceView(m, t)),
      el("div", "settings-row-desc models-used", usedForLine(m, settings, t, locale)),
    );
    const live = downloadView(m, dl, t, locale);
    const view = live?.active ? { state: "downloading", text: live.text } : stateView(m, t, locale);
    const badge = el("span", "models-state", view.text);
    badge.dataset.state = view.state;
    const side = el("div", "models-side");
    side.append(badge);
    if (live?.active) {
      const cancel = el("button", "settings-btn models-cancel", t("settings.models.downloadCancel"));
      cancel.type = "button";
      cancel.setAttribute("aria-label", t("settings.models.downloadCancelLabel", { name: m.name }));
      cancel.addEventListener("click", () => cancelDownload(cancel));
      side.append(cancel);
    } else if (canDownload(m)) {
      const get = el("button", "settings-btn models-download", t("settings.models.download"));
      get.type = "button";
      get.setAttribute("aria-label", t("settings.models.downloadLabel", { name: m.name }));
      // One download at a time: the others wait for it.
      get.disabled = downloadRunning(dl);
      get.addEventListener("click", () => startDownload(m, get));
      side.append(get);
    }
    // What went wrong, in the page's own words; read out when it changes.
    const note = el("div", "settings-row-desc models-download-msg");
    note.setAttribute("role", "status");
    note.setAttribute("aria-live", "polite");
    note.textContent = refused?.id === m.id ? refused.text : live?.error || "";
    text.append(note);
    row.append(text, side);
    return row;
  };

  const render = () => {
    // Keep focus on a row's button across a repaint: Download becomes Cancel
    // while it runs, and Cancel becomes Download again when it stops.
    const focused = document.activeElement;
    const keepId = focused && list.contains(focused) && focused.matches(".models-download, .models-cancel")
      ? focused.closest(".models-row")?.dataset.model
      : null;
    const groups = groupByPurpose(answer);
    list.replaceChildren(...groups.map((g) => {
      const section = el("div", "models-section");
      section.dataset.group = g.id;
      const id = `modelsGroup-${g.id}`;
      const title = el("h5", "models-group-title", t(g.title));
      title.id = id;
      const card = el("div", "settings-section");
      card.setAttribute("role", "group");
      card.setAttribute("aria-labelledby", id);
      card.append(...g.models.map(buildRow));
      section.append(title, card);
      return section;
    }));
    group.hidden = groups.length === 0;
    if (keepId) {
      const row = [...list.querySelectorAll(".models-row")].find((r) => r.dataset.model === keepId);
      row?.querySelector(".models-download, .models-cancel")?.focus();
    }
  };

  const readJson = (url, init) => fetch(url, { cache: "no-store", ...init })
    .then(async (r) => ({ ok: r.ok, status: r.status, body: await r.json().catch(() => null) }));

  // The list again, for the state and size the server now has.
  const reload = async () => {
    try {
      const res = await readJson("/api/models");
      if (res.ok && isModelsAnswer(res.body)) {
        answer = res.body;
        if (isDownloadState(res.body.download)) dl = res.body.download;
      }
    } catch (err) {
      console.warn("[models] the list could not be read again:", err);
    }
    if (overlay.isConnected) render();
  };

  const stopPolling = () => {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
  };

  const poll = () => {
    stopPolling();
    pollTimer = setTimeout(async () => {
      pollTimer = null;
      // Settings closed: nothing to follow. Opening it again reads the run.
      if (!overlay.isConnected) return;
      try {
        const res = await readJson("/api/models/download");
        if (!res.ok || !isDownloadState(res.body)) throw new Error(`HTTP ${res.status}`);
        pollFails = 0;
        const was = dl;
        dl = res.body;
        if (downloadRunning(dl)) {
          render();
          poll();
        } else if (downloadRunning(was)) {
          await reload();
        }
      } catch (err) {
        pollFails += 1;
        console.warn("[models] the download state could not be read:", err);
        if (pollFails < DOWNLOAD_POLL_FAILS) {
          poll();
          return;
        }
        refused = { id: dl?.model, text: downloadErrorText("lost", t) };
        render();
      }
    }, DOWNLOAD_POLL_MS);
  };

  const startDownload = async (m, btn) => {
    btn.disabled = true;
    refused = null;
    let res;
    try {
      res = await readJson(`/api/models/${encodeURIComponent(m.id)}/download`, { method: "POST" });
    } catch (err) {
      console.warn("[models] the download could not be started:", err);
      refused = { id: m.id, text: downloadErrorText("lost", t) };
      render();
      return;
    }
    if (res.status === 202) {
      dl = { state: "running", model: m.id, phase: "download", bytes: 0, total: m.download_bytes, error_code: null };
      pollFails = 0;
      render();
      poll();
      return;
    }
    const code = typeof res.body?.code === "string" ? res.body.code : "failed";
    if (code === "downloaded") {
      // It arrived meanwhile (a song needed it): the list says so.
      await reload();
      return;
    }
    refused = { id: m.id, text: downloadErrorText(code, t) };
    render();
  };

  const cancelDownload = async (btn) => {
    btn.disabled = true;
    try {
      const res = await readJson("/api/models/download/cancel", { method: "POST" });
      if (res.ok && isDownloadState(res.body)) dl = res.body;
    } catch (err) {
      console.warn("[models] the download could not be cancelled:", err);
    }
    stopPolling();
    if (downloadRunning(dl)) poll();
    else await reload();
  };

  // A new choice changes "Used for" only.
  const repaintUsed = () => {
    if (!answer) return;
    const locale = getLanguage();
    for (const row of list.querySelectorAll(".models-row")) {
      const m = answer.models.find((e) => e?.id === row.dataset.model);
      const used = row.querySelector(".models-used");
      if (m && used) used.textContent = usedForLine(m, settings, t, locale);
    }
  };

  const take = (d) => {
    if (!hasPlan(d)) return;
    settings = { ...(settings || {}), ...d };
    repaintUsed();
  };

  overlay.addEventListener(SETTINGS_APPLIED_EVENT, (e) => take(e.detail));
  const onChoice = (e) => {
    if (!overlay.isConnected) {
      document.removeEventListener(STEM_MODELS_CHANGED_EVENT, onChoice);
      return;
    }
    take(e.detail);
  };
  document.addEventListener(STEM_MODELS_CHANGED_EVENT, onChoice);

  const [modelsRes, settingsRes] = await Promise.allSettled([
    fetch("/api/models", { cache: "no-store" }).then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))),
    fetch("/api/settings", { cache: "no-store" }).then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))),
  ]);
  if (settingsRes.status === "fulfilled") take(settingsRes.value);
  else console.warn("[models] the settings could not be read:", settingsRes.reason);
  if (modelsRes.status === "fulfilled" && isModelsAnswer(modelsRes.value)) {
    answer = modelsRes.value;
    if (isDownloadState(answer.download)) dl = answer.download;
    render();
    // Opened while a download runs: follow it.
    if (downloadRunning(dl)) poll();
  } else {
    // An older server: no route, no group, nothing to say.
    console.info("[models] the built-in list is not offered by this server:", modelsRes.reason?.message ?? "not a models answer");
    group.hidden = true;
  }
}
