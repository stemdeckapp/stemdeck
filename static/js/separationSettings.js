// Settings > Separation, "Model for each stem": one row per stem, a select
// for each of the five stems with a model to pick (Guitar and Piano too, so a
// model the user added has somewhere to show) and a read-only line for Lead +
// Backing and Other, the reason for the recommendation, the time a 4 minute
// song takes, and "Use recommended". The words and the numbers come from the
// pure stemModelsView.js; this file draws them and saves a choice.
//
// "My models" (myModels.js) is in the Models tab, wired from here because the
// selects depend on it. While the server offers it, every stem select ends
// with "Add a model...", which is never posted: it opens the Models tab at
// that section's form. When a model the user added becomes usable or goes,
// the settings are read again, so the selects list it or drop it. Every
// answer the rows paint is handed to it too, for each model's "Used for".
//
// Server: GET /api/settings (stem_models, stem_models_resolved,
// stem_models_recommended, stem_models_reason, stem_model_catalog) and
// POST /api/settings { stem_models: { stem: "auto" | model id } }. An older
// server sends none of these fields: the group then stays hidden.
//
// The Compute device and Separation quality rows in the same pane are wired by
// catalog.js (wireGeneralSettings). Both change the recommendation and the
// estimate, so every settings answer catalog.js applies is also announced on
// the overlay as "stemdeck:settings-applied", and this group repaints from it.
//
// "Benchmark" (below the model rows) measures this machine: POST
// /api/speed-test starts it (409 "busy" while a track is being separated),
// GET /api/speed-test is polled about once a second while it runs, POST
// /api/speed-test/cancel stops it. "Apply recommendation" saves the device and
// every stem on "auto" through POST /api/settings. A settings answer this file
// gets is handed to catalog.js as "stemdeck:settings-answer", so the Compute
// device row shows it too. A server without the routes: the group stays hidden.

import { t, getLanguage } from "./i18n.js";
import {
  STEM_LABEL,
  estimateLine,
  fixedLine,
  hasStemModels,
  hintLine,
  noteLine,
  postValue,
  resetPatch,
  selectedValue,
  stemOptions,
  stemRows,
  warningFor,
  whyLine,
} from "./stemModelsView.js";
import {
  POLL_FAIL_LIMIT,
  POLL_MS,
  applyPatch,
  canApply,
  diskLines,
  endLine,
  failedRows,
  isResult,
  isRunning,
  isSpeedTestState,
  isStale,
  headlineLine,
  progressPct,
  reasonLines,
  resultRows,
  runningLine,
} from "./speedTestView.js";
import { OPEN_MY_MODELS_EVENT, wireMyModels } from "./myModels.js";
import { ADD_MODEL_VALUE } from "./myModelsView.js";
import { STEM_MODELS_CHANGED_EVENT } from "./laneModelView.js";

/** catalog.js announces every settings answer it applied. */
export const SETTINGS_APPLIED_EVENT = "stemdeck:settings-applied";
/** catalog.js applies a settings answer this file got (the device row). */
export const SETTINGS_ANSWER_EVENT = "stemdeck:settings-answer";

/** The settings fields the Separation tab shows. An answer this file gets is
 *  handed on with only these, so catalog.js never writes a late answer over a
 *  field in another tab (a port being typed in Network, say). */
export const SEPARATION_FIELDS = [
  "demucs_device", "demucs_devices_available", "demucs_device_resolved", "demucs_device_why",
  "device_diagnostics", "gpu_backend", "torch_error", "separation_quality",
  "idle_unload_minutes", "idle_unload_minutes_max",
  "stem_models", "stem_models_resolved", "stem_models_recommended", "stem_models_reason",
  "stem_model_catalog", "speed_test",
];

export function separationFields(d) {
  const out = {};
  for (const key of SEPARATION_FIELDS) if (d && key in d) out[key] = d[key];
  return out;
}

/** Hands a settings answer to catalog.js (the device row) and, through its
 *  "applied" event, to the model rows: the Separation fields only. */
function handOn(overlay, d) {
  overlay.dispatchEvent(new CustomEvent(SETTINGS_ANSWER_EVENT, { detail: separationFields(d) }));
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** One row: the stem's name, its lines, and its select when it has a choice. */
function buildRow(row) {
  const wrap = el("div", "settings-row stem-model-row");
  wrap.dataset.stem = row.stem;
  const text = el("div", "settings-row-text");
  const name = t(STEM_LABEL[row.stem] || `stem.${row.stem}`);
  if (row.kind === "select") {
    const id = `setStemModel-${row.stem}`;
    const title = el("label", "settings-row-title", name);
    title.htmlFor = id;
    const note = el("div", "settings-row-desc stem-model-note");
    note.id = `${id}-note`;
    const hint = el("div", "settings-row-desc set-stem-model-hint");
    hint.id = `${id}-hint`;
    hint.dataset.stem = row.stem;
    const warn = el("div", "settings-row-desc set-stem-model-warn");
    warn.id = `${id}-warn`;
    warn.dataset.stem = row.stem;
    warn.hidden = true;
    text.append(title, note, hint, warn);
    const sel = el("select", "settings-select settings-select-wide set-stem-model");
    sel.id = id;
    sel.dataset.stem = row.stem;
    sel.setAttribute("aria-describedby", `${note.id} ${hint.id} ${warn.id}`);
    wrap.append(text, sel);
  } else {
    const fixed = el("div", "settings-row-desc stem-model-fixed");
    fixed.dataset.stem = row.stem;
    text.append(el("div", "settings-row-title", name), fixed);
    if (row.fixed === "karaoke") {
      const warn = el("div", "settings-row-desc set-stem-model-warn is-unavailable");
      warn.dataset.stem = row.stem;
      warn.hidden = true;
      text.append(warn);
    }
    wrap.append(text);
  }
  return wrap;
}

/** A row list's shape: rebuilt only when this changes, so focus survives a repaint. */
const shapeOf = (rows) => rows.map((r) => `${r.stem}:${r.kind}:${r.fixed || ""}:${(r.choices || []).join(",")}`).join("|");

/** The model rows. Returns { paint(settings answer), repaint() }, or null
 *  without the group. `answers` numbers every settings request this tab
 *  makes, so only the latest one's answer is painted. `addOption()` says
 *  whether the selects end with "Add a model...". `onPaint(d)` gets every
 *  answer the rows paint. */
function wireStemModels(overlay, answers, { addOption = () => false, onPaint = () => {} } = {}) {
  const group = overlay.querySelector(".stem-models-group");
  const list = group?.querySelector(".stem-models-rows");
  const why = group?.querySelector(".set-stem-models-why");
  const estimate = group?.querySelector(".set-stem-models-estimate");
  const reset = group?.querySelector(".set-stem-models-reset");
  const msg = group?.querySelector(".stem-models-msg");
  if (!group || !list) return null;

  let data = null;
  let shape = "";

  const say = (key, isError = false) => {
    if (!msg) return;
    msg.textContent = key ? t(key) : "";
    msg.classList.toggle("error", Boolean(isError));
  };

  const paintSelect = (sel, d, choices) => {
    const stem = sel.dataset.stem;
    const opts = stemOptions(d, stem, choices, t, { addOption: addOption() });
    sel.replaceChildren(...opts.map((o) => {
      const opt = document.createElement("option");
      opt.value = o.value;
      opt.textContent = o.label;
      opt.disabled = o.disabled;
      if (o.recommended) opt.dataset.recommended = "true";
      if (o.add) opt.dataset.add = "true";
      return opt;
    }));
    const value = selectedValue(d, stem);
    sel.value = value;
    const locale = getLanguage();
    const row = sel.closest(".stem-model-row");
    const note = row?.querySelector(".stem-model-note");
    if (note) {
      note.textContent = noteLine(d, value, t);
      note.hidden = !note.textContent;
    }
    const hint = row?.querySelector(".set-stem-model-hint");
    if (hint) hint.textContent = hintLine(d, value, t, locale);
    const warn = row?.querySelector(".set-stem-model-warn");
    if (warn) {
      const w = warningFor(d, value, t, locale);
      warn.hidden = !w;
      warn.textContent = w ? w.text : "";
      warn.dataset.kind = w ? w.kind : "";
    }
  };

  const paint = (d) => {
    if (!hasStemModels(d)) {
      // An older server, or an answer without the fields: no group, no error.
      group.hidden = true;
      return;
    }
    data = d;
    group.hidden = false;
    onPaint(d);
    const rows = stemRows(d);
    const nextShape = shapeOf(rows);
    if (nextShape !== shape) {
      list.replaceChildren(...rows.map(buildRow));
      shape = nextShape;
    }
    const locale = getLanguage();
    for (const row of rows) {
      const node = list.querySelector(`.stem-model-row[data-stem="${row.stem}"]`);
      if (!node) continue;
      if (row.kind === "select") {
        paintSelect(node.querySelector(".set-stem-model"), d, row.choices);
        continue;
      }
      const fixed = node.querySelector(".stem-model-fixed");
      if (fixed) fixed.textContent = fixedLine(d, row, t);
      const warn = node.querySelector(".set-stem-model-warn");
      if (warn) {
        warn.hidden = row.available !== false;
        warn.textContent = row.available === false ? t("split.reason.unsupported_platform") : "";
      }
    }
    if (why) {
      const text = whyLine(d, t, locale);
      why.textContent = text || "";
      why.hidden = !text;
    }
    // With no choice to make (a deployment pins every stem), there is
    // nothing to set back.
    if (reset) reset.hidden = !rows.some((r) => r.kind === "select");
    if (estimate) {
      const text = estimateLine(d, t, locale);
      estimate.textContent = text || "";
      estimate.hidden = !text;
    }
  };

  const save = async (patch) => {
    // Only the answer to the latest request is painted: a slower earlier one
    // must not put back a choice the user has already changed again.
    const seq = answers.take();
    try {
      const r = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const next = await r.json();
      // The lanes' Model tabs compare each song with this default (laneModelRows.js).
      document.dispatchEvent(new CustomEvent(STEM_MODELS_CHANGED_EVENT, { detail: next }));
      if (answers.latest(seq)) {
        paint(next);
        say("settings.stemModels.saved");
      }
      return true;
    } catch (err) {
      console.warn("[separation] the model choice was not saved:", err);
      if (answers.latest(seq)) {
        // Not saved: show what is still in force, and say so.
        if (data) paint(data);
        say("settings.stemModels.saveFailed", true);
      }
      return false;
    }
  };

  list.addEventListener("change", (e) => {
    const sel = e.target.closest?.(".set-stem-model");
    if (!sel || !data) return;
    const stem = sel.dataset.stem;
    const value = postValue(data, stem, sel.value);
    if (sel.value === ADD_MODEL_VALUE || value === null) {
      // Never posted: back to the model in use, then open My models at its form.
      sel.value = selectedValue(data, stem);
      overlay.dispatchEvent(new CustomEvent(OPEN_MY_MODELS_EVENT, { detail: { stem } }));
      return;
    }
    say(null);
    save({ stem_models: { [stem]: value } });
  });

  reset?.addEventListener("click", () => {
    if (!data) return;
    say(null);
    save(resetPatch());
  });

  // The device and quality rows save through catalog.js: repaint from the
  // answer it applied, so the reason and the estimate follow at once.
  overlay.addEventListener(SETTINGS_APPLIED_EVENT, (e) => {
    if (hasStemModels(e.detail)) paint(e.detail);
  });

  return {
    paint,
    /** Draws the last answer again (the "Add a model..." option came or went). */
    repaint() {
      if (data) paint(data);
    },
  };
}

/** The speed test. Returns { fromSettings, start }, or null without the group. */
function wireSpeedTest(overlay, { rocm, answers }) {
  const group = overlay.querySelector(".speed-test-group");
  const runBtn = group?.querySelector(".set-speedtest-run");
  const cancelBtn = group?.querySelector(".set-speedtest-cancel");
  const bar = group?.querySelector(".set-speedtest-progress");
  const fill = bar?.querySelector(".speed-test-bar");
  const msg = group?.querySelector(".set-speedtest-msg");
  const resultEl = group?.querySelector(".set-speedtest-result");
  const stale = group?.querySelector(".set-speedtest-stale");
  const reason = group?.querySelector(".speed-test-reason");
  const why = group?.querySelector(".speed-test-why");
  const disk = group?.querySelector(".speed-test-disk");
  const table = group?.querySelector(".set-speedtest-table");
  const devicesBtn = group?.querySelector(".set-speedtest-devices");
  const estimateNote = group?.querySelector(".speed-test-estimate-note");
  const applyBtn = group?.querySelector(".set-speedtest-apply");
  if (!group || !runBtn || !table) return null;

  let result = null;
  let catalog = null;
  let polling = false;
  // Only the recommended device's rows show until the person asks for all.
  let allDevices = false;

  // Rows of the other devices hide while collapsed; the estimate note shows
  // while a row on view is an estimate. Without a row of the recommended
  // device (or of any other) there is nothing to fold, and every row shows.
  const foldRows = () => {
    const rows = [...(table.tBodies[0]?.rows || [])].filter((tr) => tr.dataset.model);
    const foldable = rows.some((tr) => tr.dataset.other === "true") && rows.some((tr) => tr.dataset.other !== "true");
    for (const tr of rows) tr.hidden = foldable && !allDevices && tr.dataset.other === "true";
    if (devicesBtn) {
      devicesBtn.hidden = !foldable;
      devicesBtn.setAttribute("aria-expanded", String(foldable && allDevices));
      devicesBtn.textContent = t(allDevices ? "settings.speedTest.hideOthers" : "settings.speedTest.showAll");
    }
    if (estimateNote) estimateNote.hidden = !rows.some((tr) => !tr.hidden && tr.dataset.measured !== "true");
  };

  // Written only when it changes: the region is polite, and a poll a second
  // that rewrites the same words would have a screen reader say them again.
  const say = (text, isError = false) => {
    if (!msg) return;
    const next = text || "";
    if (msg.textContent !== next) msg.textContent = next;
    msg.classList.toggle("error", Boolean(isError));
  };

  const showResult = (r) => {
    if (!isResult(r)) return;
    result = r;
    const locale = getLanguage();
    if (resultEl) resultEl.hidden = false;
    if (stale) stale.hidden = !isStale(r);
    if (reason) {
      const text = headlineLine(r, t, locale, rocm());
      reason.textContent = text || "";
      reason.hidden = !text;
    }
    if (why) {
      why.replaceChildren(...reasonLines(r, t, locale).map((line) => el("span", "speed-test-why-line", line)));
      why.hidden = !why.childElementCount;
    }
    if (disk) {
      disk.replaceChildren(...diskLines(r, t, locale).map((line) =>
        el("span", line.warn ? "speed-test-disk-line is-slow" : "speed-test-disk-line", line.text)));
      disk.hidden = !disk.childElementCount;
    }
    const body = table.tBodies[0] || table.createTBody();
    // A device the test could not measure says why, on its own line.
    const failed = failedRows(r, t, rocm()).map((row) => {
      const tr = document.createElement("tr");
      tr.className = "is-failed";
      tr.dataset.device = row.device;
      tr.dataset.failed = row.code;
      const td = el("td", "", row.text);
      td.colSpan = 3;
      tr.append(td);
      return tr;
    });
    body.replaceChildren(...failed, ...resultRows(r, { t, locale, catalog, rocm: rocm() }).map((row) => {
      const tr = document.createElement("tr");
      tr.classList.toggle("is-recommended", row.recommended);
      tr.dataset.device = row.device;
      tr.dataset.model = row.model;
      tr.dataset.measured = String(row.measured);
      tr.dataset.other = String(row.other);
      // The friendly name, with the technical one the Compute device select
      // uses beside it.
      const deviceTd = el("td", "speed-test-device", row.deviceName);
      if (row.deviceText && row.deviceText !== row.deviceName) deviceTd.append(" ", el("span", "speed-test-tech", row.deviceText));
      // The time in words and its rating as a word on a coloured badge; the
      // speed factor is the tooltip and, for a screen reader or a touch
      // screen with no hover, part of the cell's own text.
      const timeTd = el("td", "speed-test-time");
      timeTd.title = row.factorText;
      const badge = el("span", "speed-test-rating", row.ratingText);
      badge.dataset.rating = row.rating;
      timeTd.append(el("span", "speed-test-time-text", row.timeText), " ", badge, " ", el("span", "speed-test-sr", row.factorText));
      tr.append(deviceTd, el("td", "", row.modelText), timeTd);
      return tr;
    }));
    foldRows();
    if (applyBtn) applyBtn.hidden = !canApply(r);
  };

  const showRunning = (on, s = null) => {
    runBtn.hidden = on;
    if (cancelBtn) cancelBtn.hidden = !on;
    if (bar) {
      bar.hidden = !on;
      const pct = on ? progressPct(s) : 0;
      bar.setAttribute("aria-valuenow", String(pct));
      if (fill) fill.style.width = `${pct}%`;
    }
    if (on) say(runningLine(s, t, rocm()));
  };

  const handOnHere = (d) => handOn(overlay, d);

  // After a test the "auto" choices may resolve differently: read the
  // settings again and let catalog.js and the model rows repaint.
  const refreshSettings = async () => {
    const seq = answers.take();
    try {
      const r = await fetch("/api/settings", { cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      // A choice saved since this was asked is newer: leave it showing.
      if (!answers.latest(seq)) return;
      catalog = d.stem_model_catalog || catalog;
      handOnHere(d);
    } catch (err) {
      console.warn("[speed test] settings not read again:", err);
    }
  };

  const finish = (s) => {
    // Cancel hides now: a keyboard user on it lands on Run, not on nothing.
    const onCancel = document.activeElement === cancelBtn;
    showRunning(false);
    if (onCancel) runBtn.focus();
    const end = endLine(s, t);
    say(end ? end.text : "", end?.error);
    if (s.state === "done" && isResult(s.result)) {
      showResult(s.result);
      refreshSettings();
    }
  };

  // The server stopped answering (it closed, or the network went): stop
  // asking, say so, and offer Run again.
  const lost = () => {
    const onCancel = document.activeElement === cancelBtn;
    showRunning(false);
    if (onCancel) runBtn.focus();
    say(t("settings.speedTest.pollLost"), true);
  };

  const poll = async () => {
    if (polling) return;
    polling = true;
    let misses = 0;
    try {
      // Stops when Settings closes: the test goes on on the server, and the
      // next opening picks it up again.
      while (overlay.isConnected) {
        await new Promise((resolve) => setTimeout(resolve, POLL_MS));
        if (!overlay.isConnected) break;
        let s;
        try {
          const r = await fetch("/api/speed-test", { cache: "no-store" });
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          s = await r.json();
        } catch (err) {
          console.warn("[speed test] state not read:", err);
          if (++misses >= POLL_FAIL_LIMIT) {
            lost();
            break;
          }
          continue;
        }
        misses = 0;
        if (!isSpeedTestState(s)) continue;
        if (isRunning(s)) {
          showRunning(true, s);
          continue;
        }
        finish(s);
        break;
      }
    } finally {
      polling = false;
    }
  };

  runBtn.addEventListener("click", async () => {
    say("");
    try {
      const r = await fetch("/api/speed-test", { method: "POST" });
      if (r.status === 409) {
        let code = "";
        try {
          code = (await r.json()).code || "";
        } catch (err) {
          console.warn("[speed test] refusal body not read:", err);
        }
        say(t(code === "busy" ? "settings.speedTest.busy" : "settings.speedTest.failed"), true);
        return;
      }
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      showRunning(true, { state: "running", step: null, pct: 0 });
      cancelBtn?.focus();
      poll();
    } catch (err) {
      console.warn("[speed test] not started:", err);
      say(t("settings.speedTest.failed"), true);
    }
  });

  cancelBtn?.addEventListener("click", async () => {
    try {
      const r = await fetch("/api/speed-test/cancel", { method: "POST" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
    } catch (err) {
      console.warn("[speed test] not cancelled:", err);
    }
    // The poll says "cancelled" once the server has stopped it. Focus goes
    // to Run, which shows again then; until then it stays on Cancel.
  });

  devicesBtn?.addEventListener("click", () => {
    allDevices = !allDevices;
    foldRows();
  });

  applyBtn?.addEventListener("click", async () => {
    if (!canApply(result)) return;
    say("");
    const seq = answers.take();
    try {
      const r = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(applyPatch(result)),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      // Saved either way; a newer answer is already showing what is in force.
      if (answers.latest(seq)) handOnHere(d);
      say(t("settings.speedTest.applied"));
    } catch (err) {
      console.warn("[speed test] recommendation not applied:", err);
      say(t("settings.speedTest.applyFailed"), true);
    }
  });

  return {
    /** The stored result and the model names, from a settings answer. */
    fromSettings(d) {
      catalog = d?.stem_model_catalog || catalog;
      if (isResult(d?.speed_test)) showResult(d.speed_test);
    },
    /** Shows the group when this server has the routes, and follows a test already running. */
    async start() {
      try {
        const r = await fetch("/api/speed-test", { cache: "no-store" });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const s = await r.json();
        if (!isSpeedTestState(s)) throw new Error("not a speed test answer");
        group.hidden = false;
        if (isRunning(s)) {
          showRunning(true, s);
          poll();
        } else if (!result && isResult(s.result)) {
          showResult(s.result);
        }
      } catch (err) {
        // An older server: no routes, no group, nothing to say.
        console.info("[speed test] not offered by this server:", err.message);
        group.hidden = true;
      }
    },
  };
}

export async function wireSeparationSettings(overlay) {
  let seq = 0;
  const answers = { take: () => ++seq, latest: (n) => n === seq };
  let myModels = null;
  const stemModels = wireStemModels(overlay, answers, {
    addOption: () => myModels?.offered() === true,
    onPaint: (d) => myModels?.fromPlan(d),
  });
  let gpuBackend = "";
  const speedTest = wireSpeedTest(overlay, { rocm: () => gpuBackend === "rocm", answers });
  const modelsGroup = overlay.querySelector(".stem-models-group");
  // A model the user added became usable or went: the selects follow the
  // settings answer, so read it again.
  const refreshSettings = async () => {
    const seqNow = answers.take();
    try {
      const r = await fetch("/api/settings", { cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      // A choice saved since this was asked is newer: leave it showing.
      if (answers.latest(seqNow)) handOn(overlay, d);
    } catch (err) {
      console.warn("[my models] settings not read again:", err);
    }
  };
  myModels = wireMyModels(overlay, { onModelsChanged: refreshSettings, onOffered: () => stemModels?.repaint() });
  speedTest?.start();
  myModels?.start();
  const first = answers.take();
  try {
    const r = await fetch("/api/settings", { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    gpuBackend = d.gpu_backend || "";
    // A choice made before this first answer came back is newer than it.
    if (answers.latest(first)) stemModels?.paint(d);
    speedTest?.fromSettings(d);
    myModels?.fromSettings(d);
  } catch (err) {
    console.warn("[separation] the settings could not be read:", err);
    if (modelsGroup && !modelsGroup.querySelector(".stem-model-row")) modelsGroup.hidden = true;
  }
}
