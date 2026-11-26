// Settings > Models, "My models": add a model of your own (a Hugging Face
// link, or a model file and its config file from this computer), the list of
// the models you added, what each is used for, and Check again, Cancel and
// Remove on each. Wired by separationSettings.js, whose stem selects offer a
// model once it is usable and whose "Add a model..." opens this section.
//
// The section is static HTML in catalog.js (".my-models-group"), hidden until
// GET /api/my-models answers: an older server has no route and no section.
// The words come from the pure myModelsView.js; the routes are listed there.
//
// - Add by link posts JSON and the server downloads; the list then polls GET
//   about once a second while a model downloads or is checked.
// - Add by files sends multipart through XMLHttpRequest, because fetch cannot
//   report upload progress; Cancel aborts it.
// - Add by link needs online sources on (it makes network calls). Off, the link
//   field is disabled with a line that says so, and files work either way.
// - A model that became usable, or went, changes the stem selects: the caller's
//   onModelsChanged reads the settings again so they repaint.
// - "Used for" on each row is the stems the next job separates with it, from
//   the settings answer the stem selects were drawn from (fromPlan).
// - Every name and reason is written as text (textContent), never as HTML: a
//   model name is the user's own text.

import { t, getLanguage } from "./i18n.js";
import { onOnlineSourcesChange, onlineSourcesFrom, onlineSourcesOn } from "./onlineSources.js";
import { usedForLine } from "./modelsView.js";
import { linkedLine } from "./sourceLink.js";
import {
  CONFIG_ACCEPT,
  MODEL_ACCEPT,
  POLL_FAIL_LIMIT,
  POLL_MS,
  addRoute,
  anyActive,
  canAdd,
  canRunModels,
  entriesOf,
  needsDownload,
  focusAfterRemove,
  isMyModelsAnswer,
  linkBody,
  metaLine,
  missingLine,
  reasonText,
  refusalCode,
  sourceLine,
  userSourceView,
  statusView,
  uploadFields,
  uploadPct,
} from "./myModelsView.js";

/** separationSettings.js asks for the section when "Add a model..." is picked:
 *  it opens the Models tab at the form. */
export const OPEN_MY_MODELS_EVENT = "stemdeck:open-my-models";

const API = "/api/my-models";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function readJson(r) {
  try {
    return await r.json();
  } catch (err) {
    console.warn("[my models] answer not read:", err);
    return null;
  }
}

/** What a usable model changes in the stem selects: when this differs, they repaint. */
const usableSignature = (entries) =>
  entries.filter((e) => e.status === "ready" && e.usable !== false && e.missing !== true).map((e) => e.id).sort().join(",");

/**
 * Wires the section in an open Settings overlay. Returns
 * { start, offered, fromSettings, fromPlan }, or null when the overlay has no section.
 *   onModelsChanged()  a model became usable or went: read the settings again
 *   onOffered()        the server has the routes: the selects may offer "Add a model..."
 */
export function wireMyModels(overlay, { onModelsChanged, onOffered } = {}) {
  const group = overlay?.querySelector(".my-models-group");
  if (!group) return null;
  const $ = (sel) => group.querySelector(sel);
  const toggle = $(".my-models-toggle");
  const body = $(".my-models-body");
  const form = $(".my-models-form");
  const link = $(".my-models-link");
  const linkOff = $(".my-models-link-off");
  const fileInput = $(".my-models-file-input");
  const fileBtn = $(".my-models-file-btn");
  const fileName = $("#myModelsFileName");
  const cfgInput = $(".my-models-config-input");
  const cfgBtn = $(".my-models-config-btn");
  const cfgName = $(".my-models-config-name");
  const lic = $(".my-models-licence-input");
  const add = $(".my-models-add");
  const uploadCancel = $(".my-models-upload-cancel");
  const uploadBar = $(".my-models-upload");
  const uploadFill = uploadBar?.querySelector(".my-models-upload-bar");
  const missing = $(".my-models-missing");
  const msg = $(".my-models-msg");
  const list = $(".my-models-list");
  const empty = $(".my-models-empty");
  const live = $(".my-models-live");
  const listTitle = $(".my-models-list-title");
  const resetNote = $(".my-models-registry-reset");
  const pinnedNote = $(".my-models-pinned");
  const unavailableNote = $(".my-models-unavailable");
  if (!toggle || !body || !form || !list) return null;

  fileInput?.setAttribute("accept", MODEL_ACCEPT);
  cfgInput?.setAttribute("accept", CONFIG_ACCEPT);

  let entries = [];
  let offered = false;
  let polling = false;
  let pollLost = false;
  let upload = null;
  let linkAllowed = onlineSourcesOn();
  // This install can run a model the user adds (GET says "available").
  let runnable = true;
  let signature = null;
  // The settings answer "Used for" is read from (stem_models_resolved).
  let plan = null;
  // The last status each row showed, so a change is said once.
  const shown = new Map();

  const open = (on) => {
    body.hidden = !on;
    toggle.setAttribute("aria-expanded", String(on));
  };
  toggle.addEventListener("click", () => open(body.hidden));

  // Written only when it changes: both regions are polite, and a poll a
  // second that rewrites the same words would have a screen reader repeat them.
  const say = (text, isError = false) => {
    if (!msg) return;
    const next = text || "";
    if (msg.textContent !== next) msg.textContent = next;
    msg.classList.toggle("error", Boolean(isError) && Boolean(next));
  };
  const announce = (text) => {
    if (live && text) live.textContent = text;
  };

  const formState = () => ({
    link: link?.value || "",
    fileName: fileInput?.files?.[0]?.name || "",
    configName: cfgInput?.files?.[0]?.name || "",
    stems: [...group.querySelectorAll(".my-models-stem:checked")].map((b) => b.value),
    licence: lic?.checked === true,
    linkAllowed,
  });
  const refreshAdd = () => {
    const s = formState();
    // An install that cannot run one would refuse it after a 2 GB upload.
    if (add) add.disabled = upload !== null || !runnable || !canAdd(s);
    if (missing) missing.textContent = upload ? "" : missingLine(s, t, getLanguage());
  };
  form.addEventListener("input", refreshAdd);
  form.addEventListener("change", refreshAdd);

  const paintLinkAllowed = () => {
    if (link) link.disabled = !linkAllowed || upload !== null;
    if (linkOff) linkOff.hidden = linkAllowed;
    refreshAdd();
    // A row whose Check again downloads follows the switch too.
    if (entries.some(needsDownload)) render();
  };
  // The switch lives in another tab of the same dialog; follow it while this
  // dialog is open, and stop listening once it has closed.
  const stopOnline = onOnlineSourcesChange((on) => {
    if (!overlay.isConnected) {
      stopOnline();
      return;
    }
    linkAllowed = on;
    paintLinkAllowed();
  });

  // The chosen file's name, or "No ... chosen". Only the second one is a
  // translated text: a language switch must not write over a file name.
  const showChosen = (node, file, noneKey) => {
    if (!node) return;
    if (file) {
      node.removeAttribute("data-i18n");
      node.textContent = file.name;
    } else {
      node.setAttribute("data-i18n", noneKey);
      node.textContent = t(noneKey);
    }
  };
  fileBtn?.addEventListener("click", () => fileInput?.click());
  fileInput?.addEventListener("change", () => {
    showChosen(fileName, fileInput.files?.[0], "settings.myModels.file.none");
    refreshAdd();
  });
  cfgBtn?.addEventListener("click", () => cfgInput?.click());
  cfgInput?.addEventListener("change", () => {
    showChosen(cfgName, cfgInput.files?.[0], "settings.myModels.config.none");
    refreshAdd();
  });

  const clearForm = () => {
    form.reset();
    showChosen(fileName, null, "settings.myModels.file.none");
    showChosen(cfgName, null, "settings.myModels.config.none");
    refreshAdd();
  };

  // ── The list ──

  const buildRow = (m) => {
    const view = statusView(m, t);
    const locale = getLanguage();
    const li = el("li", "my-model-row");
    li.dataset.id = m.id;
    li.dataset.status = view.status;
    const main = el("div", "my-model-main");
    main.append(el("div", "my-model-name", m.name || m.id));
    const meta = el("div", "settings-row-desc my-model-meta", metaLine(m, t, locale));
    if (m.sha256) meta.title = `SHA-256 ${m.sha256}`;
    main.append(meta);
    // Who it comes from: always the person, with the link they added it from.
    main.append(linkedLine("settings-row-desc my-model-source-by", userSourceView(m, t)));
    const source = sourceLine(m, t);
    if (source) main.append(el("div", "settings-row-desc my-model-source", source));
    const status = el("div", "my-model-status-line");
    const badge = el("span", "my-model-status", view.label);
    badge.dataset.status = view.status;
    status.append(badge);
    if (view.status === "downloading") {
      const bar = el("span", "my-model-progress");
      bar.setAttribute("role", "progressbar");
      bar.setAttribute("aria-valuemin", "0");
      bar.setAttribute("aria-valuemax", "100");
      bar.setAttribute("aria-valuenow", String(view.pct));
      bar.setAttribute("aria-label", t("settings.myModels.progressLabel", { name: m.name || m.id }));
      const fill = el("span", "my-model-progress-fill");
      fill.style.width = `${view.pct}%`;
      bar.append(fill);
      status.append(bar);
    } else if (view.status === "checking") {
      const bar = el("span", "my-model-checking-bar");
      bar.setAttribute("aria-hidden", "true");
      status.append(bar);
    }
    if (view.reason) status.append(el("span", "settings-row-desc my-model-reason", view.reason));
    main.append(status);
    // A link entry with no files: Check again fetches it again, which needs
    // online sources on. Said here, and Check again is off while they are off.
    // What the next job separates with it; nothing while it is not usable.
    const used = el("div", "settings-row-desc my-model-used", view.status === "ready" ? usedForLine({ id: m.id, use: "stems" }, plan, t, locale) : "");
    main.append(used);
    const redownload = needsDownload(m);
    if (redownload) main.append(el("div", "settings-row-desc my-model-redownload", t("settings.myModels.row.redownload")));

    const btns = el("div", "my-model-actions");
    const name = m.name || m.id;
    if (view.active) {
      const cancel = el("button", "settings-btn my-model-cancel", t("settings.myModels.cancel"));
      cancel.type = "button";
      cancel.setAttribute("aria-label", t("settings.myModels.cancelLabel", { name }));
      cancel.addEventListener("click", () => act(m, "cancel", cancel));
      btns.append(cancel);
    }
    const again = el("button", "settings-btn my-model-check", t("settings.myModels.check"));
    again.type = "button";
    again.disabled = view.active || (redownload && !linkAllowed);
    again.setAttribute("aria-label", t("settings.myModels.checkLabel", { name }));
    again.addEventListener("click", () => act(m, "check", again));
    const rm = el("button", "settings-btn my-model-remove", t("settings.myModels.remove"));
    rm.type = "button";
    rm.setAttribute("aria-label", t("settings.myModels.removeLabel", { name }));
    rm.addEventListener("click", () => act(m, "remove", rm));
    btns.append(again, rm);
    li.append(main, btns);
    return li;
  };

  const render = () => {
    // Keep focus on a row's button across a repaint when the row stays.
    const focused = document.activeElement;
    const keep = focused && list.contains(focused)
      ? {
        id: focused.closest(".my-model-row")?.dataset.id,
        cls: ["my-model-cancel", "my-model-check", "my-model-remove"].find((c) => focused.classList.contains(c)),
      }
      : null;
    list.replaceChildren(...entries.map(buildRow));
    if (empty) empty.hidden = entries.length > 0;
    list.hidden = entries.length === 0;
    if (keep?.id) {
      const row = [...list.children].find((li) => li.dataset.id === keep.id);
      const btn = keep.cls ? row?.querySelector(`.${keep.cls}`) : null;
      if (btn && !btn.disabled) btn.focus();
      else row?.querySelector(".my-model-remove")?.focus();
    }
  };

  // Say each change of state once: "X is ready.", or why it was refused.
  const announceChanges = () => {
    for (const m of entries) {
      const view = statusView(m, t);
      const before = shown.get(m.id);
      shown.set(m.id, view.status);
      if (before === undefined || before === view.status || view.active) continue;
      const name = m.name || m.id;
      announce(view.status === "ready"
        ? t("settings.myModels.ready", { name })
        : t("settings.myModels.notReady", { name, reason: view.reason }));
    }
    for (const id of [...shown.keys()]) if (!entries.some((m) => m.id === id)) shown.delete(id);
  };

  const apply = (d) => {
    entries = entriesOf(d);
    if (resetNote) resetNote.hidden = d?.registry_reset !== true;
    runnable = canRunModels(d);
    if (unavailableNote) unavailableNote.hidden = runnable;
    refreshAdd();
    render();
    announceChanges();
    const next = usableSignature(entries);
    if (signature !== null && next !== signature) onModelsChanged?.();
    signature = next;
    if (anyActive(entries)) poll();
  };

  const load = async () => {
    const r = await fetch(API, { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    if (!isMyModelsAnswer(d)) throw new Error("not a my-models answer");
    apply(d);
    return d;
  };

  const reload = async () => {
    try {
      await load();
    } catch (err) {
      console.warn("[my models] list not read:", err);
      say(t("settings.myModels.listFailed"), true);
    }
  };

  const poll = async () => {
    if (polling) return;
    polling = true;
    let misses = 0;
    try {
      // Stops when Settings closes: the work goes on on the server, and the
      // next opening picks it up again.
      while (overlay.isConnected && anyActive(entries)) {
        await new Promise((resolve) => setTimeout(resolve, POLL_MS));
        if (!overlay.isConnected) break;
        try {
          await load();
          misses = 0;
          if (pollLost) {
            pollLost = false;
            say("");
          }
        } catch (err) {
          console.warn("[my models] state not read:", err);
          if (++misses >= POLL_FAIL_LIMIT) {
            pollLost = true;
            say(t("settings.myModels.pollLost"), true);
            break;
          }
        }
      }
    } finally {
      polling = false;
    }
  };

  // Check again, Cancel and Remove on one row.
  const act = async (m, kind, btn) => {
    const name = m.name || m.id;
    const id = encodeURIComponent(m.id);
    const req = kind === "remove"
      ? { url: `${API}/${id}`, method: "DELETE" }
      : { url: `${API}/${id}/${kind}`, method: "POST" };
    const index = entries.findIndex((e) => e.id === m.id);
    btn.disabled = true;
    say("");
    let r;
    try {
      r = await fetch(req.url, req);
    } catch (err) {
      console.warn(`[my models] ${kind} not sent:`, err);
      btn.disabled = false;
      say(t("settings.myModels.actionFailed", { name }), true);
      return;
    }
    if (!r.ok) {
      const code = refusalCode(r.status, await readJson(r));
      console.warn(`[my models] ${kind} refused: HTTP ${r.status} ${code}`);
      btn.disabled = false;
      say(t("settings.myModels.notReady", { name, reason: reasonText(code, t) }), true);
      await reload();
      return;
    }
    if (kind === "remove") {
      announce(t("settings.myModels.removed", { name }));
      shown.delete(m.id);
      await reload();
      // Focus goes to the next row's Remove, else the list title.
      const at = focusAfterRemove(index, entries.length);
      const next = at >= 0 ? list.querySelectorAll(".my-model-remove")[at] : null;
      (next || listTitle)?.focus();
      return;
    }
    if (kind === "check") announce(t("settings.myModels.checking", { name }));
    await reload();
  };

  // ── Adding ──

  const showUpload = (on, pct = null) => {
    if (uploadCancel) uploadCancel.hidden = !on;
    if (uploadBar) {
      uploadBar.hidden = !on;
      uploadBar.setAttribute("aria-valuenow", String(pct ?? 0));
      if (uploadFill) uploadFill.style.width = `${pct ?? 0}%`;
    }
    for (const node of [link, fileBtn, cfgBtn, lic, ...group.querySelectorAll(".my-models-stem")]) {
      if (node) node.disabled = on;
    }
    if (!on) paintLinkAllowed();
    refreshAdd();
  };

  // An add the server accepted: the new row shows, the form empties, and
  // focus goes to the list, where the model now is.
  const added = async (entry) => {
    clearForm();
    announce(t("settings.myModels.added", { name: entry?.name || "" }));
    await reload();
    listTitle?.focus();
  };

  const refused = (status, data) => {
    const code = refusalCode(status, data);
    console.warn(`[my models] add refused: HTTP ${status} ${code}`);
    say(t("settings.myModels.addRefused", { reason: reasonText(code, t) }), true);
    // The server saw online sources off: show the link field as it is.
    if (code === "online_sources_off") {
      linkAllowed = false;
      paintLinkAllowed();
    }
  };

  const addByLink = async (s) => {
    say("");
    if (add) add.disabled = true;
    try {
      const r = await fetch(`${API}/link`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(linkBody(s)),
      });
      const data = await readJson(r);
      if (!r.ok) {
        refused(r.status, data);
        return;
      }
      await added(data);
    } catch (err) {
      console.warn("[my models] link not sent:", err);
      say(t("settings.myModels.addFailed"), true);
    } finally {
      refreshAdd();
    }
  };

  const addByFiles = (s) => {
    const model = fileInput?.files?.[0];
    const config = cfgInput?.files?.[0];
    if (!model || !config) return;
    say(t("settings.myModels.upload.progress", { pct: 0 }));
    const fd = new FormData();
    fd.append("model", model, model.name);
    fd.append("config", config, config.name);
    for (const [k, v] of uploadFields(s)) fd.append(k, v);
    const xhr = new XMLHttpRequest();
    upload = xhr;
    showUpload(true, 0);
    xhr.upload.addEventListener("progress", (e) => {
      const pct = uploadPct(e.loaded, e.lengthComputable ? e.total : 0);
      if (pct === null) return;
      if (uploadBar) uploadBar.setAttribute("aria-valuenow", String(pct));
      if (uploadFill) uploadFill.style.width = `${pct}%`;
      say(t("settings.myModels.upload.progress", { pct }));
    });
    const done = () => {
      // Cancel hides now: a keyboard user on it lands on the file button.
      const onCancel = document.activeElement === uploadCancel;
      upload = null;
      showUpload(false);
      if (onCancel) fileBtn?.focus();
    };
    xhr.addEventListener("load", async () => {
      let data = null;
      try {
        data = JSON.parse(xhr.responseText || "null");
      } catch (err) {
        console.warn("[my models] upload answer not read:", err);
      }
      done();
      if (xhr.status >= 200 && xhr.status < 300) {
        say("");
        await added(data);
      } else {
        refused(xhr.status, data);
      }
    });
    xhr.addEventListener("error", () => {
      console.warn("[my models] upload failed");
      done();
      say(t("settings.myModels.addFailed"), true);
    });
    xhr.addEventListener("abort", () => {
      done();
      say(t("settings.myModels.upload.cancelled"));
    });
    xhr.open("POST", `${API}/upload`);
    xhr.send(fd);
  };

  uploadCancel?.addEventListener("click", () => upload?.abort());

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const s = formState();
    if (upload || !canAdd(s)) return;
    if (addRoute(s) === "link") addByLink(s);
    else addByFiles(s);
  });

  // "Add a model..." in a stem select: open the Models tab, and the section
  // at its form. The tab's click is the one way into a section (catalog.js).
  overlay.addEventListener(OPEN_MY_MODELS_EVENT, () => {
    if (!offered) return;
    const pane = group.closest(".settings-pane");
    if (pane?.dataset.pane) overlay.querySelector(`.settings-tab[data-tab="${pane.dataset.pane}"]`)?.click();
    open(true);
    group.scrollIntoView({ block: "start", behavior: "smooth" });
    const target = link && !link.disabled ? link : fileBtn;
    target?.focus({ preventScroll: true });
  });

  /** What each stem is separated with now, from a settings answer: the rows'
   *  "Used for" follows it. */
  const fromPlan = (d) => {
    if (!d || typeof d !== "object" || !d.stem_models_resolved) return;
    plan = d;
    if (entries.length) render();
  };

  paintLinkAllowed();
  refreshAdd();

  return {
    /** Whether this server has the routes (the section shows). */
    offered: () => offered,
    /** Online sources and a deployment's pinned model, from a settings answer. */
    fromSettings(d) {
      if (d && typeof d === "object" && "online_sources" in d) {
        linkAllowed = onlineSourcesFrom(d);
        paintLinkAllowed();
      }
      if (pinnedNote) pinnedNote.hidden = !d?.stem_model_catalog?.deployment;
      fromPlan(d);
    },
    fromPlan,
    /** Shows the section when this server has the routes, and follows work under way. */
    async start() {
      try {
        await load();
        group.hidden = false;
        offered = true;
        onOffered?.();
      } catch (err) {
        // An older server: no routes, no section, nothing to say.
        console.info("[my models] not offered by this server:", err.message);
        group.hidden = true;
        offered = false;
      }
    },
  };
}
