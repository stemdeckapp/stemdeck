// The Master drawer: opened from the footer's Master chip or from the row in
// the Export Mix menu. It sits between the lanes and the footer, in the
// layout (the lanes give it room), and holds four columns:
//
//   settings (on/off, preset, target, ceiling, glue, listen, readout, excerpt)
//   | master EQ: its toolbar as a bar over the graph (E2's general panel on
//     the MASTER lane) | meters (approximate)
//
// The truth lives in masterEngine.js and eqEngine.js; this module only draws
// and forwards. Every string goes through i18n.

import { t, onLanguageChange } from "./i18n.js";
import * as eq from "./eqEngine.js";
import * as master from "./masterEngine.js";
import { MASTER_LIMITS, PRESET_ORDER } from "./masterMath.js";
import { fmtDb, LOSSY_CEILING_DBTP } from "./masterMath.js";
import { audioEngine, totalDuration } from "./state.js";
import { transport, togglePlayPause } from "./transport.js";
import { excerptUrl } from "./player.js";
import { onModeChange } from "./mode.js";

const EXCERPT_SEC = 30;
const METER_MS = 100;

let drawer = null;
let chip = null;
let ui = null; // built on first open
let open = false;
let eqPanel = null;
let meters = null;
let meterTimer = 0;
let excerpt = null; // { master: HTMLAudioElement, original: HTMLAudioElement, start, end }
// The excerpt being rendered. Closing the drawer (or Stop) aborts it: before,
// the render finished after the drawer had closed and played with nothing on
// screen to stop it.
let excerptCtl = null;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function btn(cls, text) {
  const b = el("button", cls, text);
  b.type = "button";
  return b;
}

function exportFormat() {
  return document.querySelector(".export-fmt.active")?.id?.replace("t-fmt-", "") || "wav";
}

// ─── Building ───

function build() {
  const root = el("div", "master-drawer-inner");

  // Settings column.
  const side = el("div", "master-side");
  const head = el("div", "master-head");
  const title = el("span", "master-title");
  const power = btn("eq-power master-power", "");
  const close = btn("master-close", "×");
  head.append(title, power, close);

  const presets = el("div", "eq-seg master-presets");
  presets.setAttribute("role", "radiogroup");
  const presetBtns = PRESET_ORDER.map((p) => {
    const b = btn("eq-seg-btn master-preset", "");
    b.dataset.preset = p;
    b.setAttribute("role", "radio");
    b.addEventListener("click", () => master.choosePreset(p));
    presets.append(b);
    return b;
  });
  const presetDesc = el("p", "master-preset-desc");

  const fields = el("div", "master-fields");
  const field = (cls, min, max, step, unit) => {
    const lab = el("label", `master-field ${cls}`);
    const name = el("span", "master-field-name");
    // Text, not type=number: a number field in an English-locale browser took
    // "-12,5" as -125. The value is parsed here, comma or dot alike, and the
    // arrow keys step it.
    const input = el("input", "master-num");
    input.type = "text";
    input.inputMode = "decimal";
    input.setAttribute("aria-valuemin", String(min));
    input.setAttribute("aria-valuemax", String(max));
    input.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
      e.preventDefault();
      const cur = parseNum(input.value);
      const base = Number.isFinite(cur) ? cur : min;
      const next = Math.round((base + (e.key === "ArrowUp" ? step : -step)) / step) * step;
      input.value = String(Math.max(min, Math.min(max, +next.toFixed(2))));
      input.dispatchEvent(new Event("change"));
    });
    const u = el("span", "master-unit", unit);
    lab.append(name, input, u);
    fields.append(lab);
    return { lab, name, input };
  };
  const target = field("master-target", MASTER_LIMITS.targetMin, MASTER_LIMITS.targetMax, 0.5, "LUFS");
  const ceiling = field("master-ceiling", MASTER_LIMITS.ceilingMin, MASTER_LIMITS.ceilingMax, 0.1, "dBTP");
  const glueRow = el("label", "master-field master-glue");
  const glueName = el("span", "master-field-name");
  const glue = el("input", "master-range");
  glue.type = "range";
  glue.min = "0";
  glue.max = "100";
  glue.step = "5";
  const glueVal = el("span", "master-unit master-glue-val");
  glueRow.append(glueName, glue, glueVal);
  fields.append(glueRow);

  const listenRow = el("div", "master-listen");
  const listenName = el("span", "master-field-name");
  const listenSeg = el("div", "eq-seg master-listen-seg");
  listenSeg.setAttribute("role", "radiogroup");
  const listenBtns = ["master", "original"].map((which) => {
    const b = btn("eq-seg-btn", "");
    b.dataset.listen = which;
    b.setAttribute("role", "radio");
    b.addEventListener("click", () => { master.setListen(which); syncExcerptSource(); });
    listenSeg.append(b);
    return b;
  });
  const matchLab = el("label", "master-match");
  const matchBox = el("input");
  matchBox.type = "checkbox";
  const matchText = el("span");
  matchLab.append(matchBox, matchText);
  listenRow.append(listenName, listenSeg, matchLab);

  const readout = el("div", "master-readout");
  readout.setAttribute("aria-live", "polite");
  const table = el("div", "master-readout-grid");
  const cells = {};
  for (const key of ["h0", "hBefore", "hAfter", "lLufs", "bLufs", "aLufs", "lTp", "bTp", "aTp", "lGain", "bGain", "aGain"]) {
    cells[key] = el("span", `master-cell master-${key}`);
    table.append(cells[key]);
  }
  const status = el("p", "master-status");
  readout.append(table, status);

  const exRow = el("div", "master-excerpt");
  const exBtn = btn("eq-btn master-excerpt-btn", "");
  const exState = el("span", "master-excerpt-state");
  exRow.append(exBtn, exState);
  const note = el("p", "eq-note master-note");

  side.append(head, presets, presetDesc, fields, listenRow, readout, exRow, note);

  // Master EQ: E2's general panel on the MASTER lane.
  const eqSide = el("div", "master-eq-side eq-side");
  const eqHead = el("div", "eq-side-head");
  const eqTitle = el("span", "eq-side-title master-eq-title");
  const eqPower = btn("eq-power master-eq-power", "");
  eqPower.addEventListener("click", () => eq.setBypass(eq.MASTER, !eq.getLaneEq(eq.MASTER).bypass));
  eqHead.append(eqTitle, eqPower);
  const eqControls = el("div", "eq-side-controls");
  eqSide.append(eqHead, eqControls);
  const eqBody = el("div", "master-eq-body");
  for (const type of ["pointerdown", "click"]) eqBody.addEventListener(type, (e) => e.stopPropagation());

  // Meters.
  const meterCol = el("div", "master-meters");
  meterCol.setAttribute("role", "group");
  const meterHead = el("span", "master-meters-head");
  const bars = el("div", "master-bars");
  const mkBar = (cls) => {
    const wrap = el("div", `master-bar ${cls}`);
    const fill = el("div", "master-bar-fill");
    const ceil = el("div", "master-bar-ceil");
    const lab = el("span", "master-bar-label");
    const val = el("span", "master-bar-val");
    const track = el("div", "master-bar-track");
    track.append(fill, ceil);
    wrap.append(lab, track, val);
    bars.append(wrap);
    return { wrap, fill, ceil, lab, val, lufs: el("span", "master-lufs-val") };
  };
  const inBar = mkBar("master-bar-in");
  const outBar = mkBar("master-bar-out");
  const lufsRow = el("div", "master-lufs-row");
  const lufsCap = el("span", "master-lufs-cap");
  lufsRow.append(inBar.lufs, outBar.lufs, lufsCap);
  const gr = el("span", "master-gr");
  meterCol.append(meterHead, bars, lufsRow, gr);

  // The master EQ's controls run as a bar above its graph, so the graph gets
  // the drawer's width (beside them it was 2 px wide at 1280 px).
  const eqArea = el("div", "master-eq-area");
  eqArea.append(eqSide, eqBody);
  root.append(side, eqArea, meterCol);
  drawer.replaceChildren(root);

  // ─── Events ───
  power.addEventListener("click", () => master.setMasterOn(!master.isMasterOn()));
  close.addEventListener("click", () => setOpen(false));
  const onNum = (which) => () => {
    const m = master.getMaster();
    const v = parseNum(which === "target" ? target.input.value : ceiling.input.value);
    if (Number.isFinite(v)) {
      const params = { ...m.params, preset: "custom" };
      if (which === "target") params.target_lufs = v;
      else params.ceiling_dbtp = v;
      master.setMaster({ ...m, bypass: false, params });
    }
    // Show what was kept, clamped, even while the field has focus: paint()
    // leaves a focused field alone, so a typed -40 stayed on screen as -40
    // while the master used the limit, and an unreadable entry left it blank.
    writeFields();
  };
  target.input.addEventListener("change", onNum("target"));
  ceiling.input.addEventListener("change", onNum("ceiling"));
  glue.addEventListener("input", () => {
    glueVal.textContent = `${glue.value}%`;
  });
  glue.addEventListener("change", () => {
    const m = master.getMaster();
    master.setMaster({ ...m, params: { ...m.params, comp: Number(glue.value) / 100 } });
  });
  matchBox.addEventListener("change", () => { master.setMatch(matchBox.checked); syncExcerptSource(); });
  exBtn.addEventListener("click", () => (excerpt ? stopExcerpt() : startExcerpt()));
  // Keys typed in the drawer are the drawer's: Space must not start playback
  // from a field, Delete must not move the song to the Trash.
  drawer.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { setOpen(false); chip?.focus(); return; }
    const control = e.target?.closest?.("button, input, select");
    if (e.key === "Delete" || e.key === "Backspace" || (control && (e.code === "Space" || e.key === "Enter"))) {
      e.stopPropagation();
    }
  });

  ui = {
    title, power, close, presetBtns, presetDesc, target, ceiling, glue, glueName, glueVal,
    listenName, listenBtns, matchBox, matchText, cells, status, exBtn, exState, note, lufsCap,
    eqTitle, eqPower, eqControls, eqBody, meterHead, inBar, outBar, gr, presets, listenSeg, meterCol,
  };
  relabel();
}

async function mountEq() {
  if (eqPanel || !ui) return;
  try {
    const mod = await import("./eq/eqGraphPanel.js");
    if (!ui || eqPanel) return;
    eqPanel = mod.mountPanel({
      lane: eq.MASTER, kind: "graph", body: ui.eqBody, controls: ui.eqControls,
      name: t("master.eqTitle"), color: "#f4b740",
    });
  } catch (err) {
    console.warn("[master] loading the master EQ failed:", err);
  }
}

// ─── Painting ───

function relabel() {
  if (!ui) return;
  ui.title.textContent = t("master.title");
  ui.close.setAttribute("aria-label", t("master.close"));
  ui.close.title = t("master.close");
  ui.presets.setAttribute("aria-label", t("master.presetsAria"));
  for (const b of ui.presetBtns) b.textContent = t(`master.preset.${b.dataset.preset}`);
  ui.target.name.textContent = t("master.target");
  ui.ceiling.name.textContent = t("master.ceiling");
  ui.glueName.textContent = t("master.glue");
  ui.glue.setAttribute("aria-label", t("master.glue"));
  ui.glue.title = t("master.glueTitle");
  ui.listenName.textContent = t("master.listen");
  ui.listenSeg.setAttribute("aria-label", t("master.listenAria"));
  ui.listenBtns[0].textContent = t("master.listenMaster");
  ui.listenBtns[1].textContent = t("master.listenOriginal");
  ui.matchText.textContent = t("master.match");
  ui.matchBox.parentElement.title = t("master.matchTitle");
  ui.cells.hBefore.textContent = t("master.before");
  ui.cells.hAfter.textContent = t("master.after");
  ui.cells.lLufs.textContent = "LUFS";
  ui.cells.lTp.textContent = "dBTP";
  ui.cells.lGain.textContent = t("master.gain");
  ui.lufsCap.textContent = t("master.meterLufs");
  ui.exBtn.title = t("master.excerptTitle");
  ui.eqTitle.textContent = t("master.eqTitle");
  ui.meterHead.textContent = t("master.meters");
  ui.meterCol.setAttribute("aria-label", t("master.meterAria"));
  ui.meterCol.title = t("master.meterTitle");
  ui.inBar.lab.textContent = t("master.meterIn");
  ui.outBar.lab.textContent = t("master.meterOut");
  eqPanel?.relabel?.();
  paint();
}

function paintChip() {
  if (!chip) return;
  const on = master.isMasterOn();
  const m = master.getMaster();
  const state = on ? t(`master.preset.${m.params.preset}`) : t("master.state.off");
  chip.classList.toggle("on", on);
  chip.dataset.state = on ? m.params.preset : "off";
  chip.setAttribute("aria-expanded", String(open));
  // The icon alone on screen: the name and the state are in the label.
  const label = t("master.exportRow", { state });
  chip.setAttribute("aria-label", label);
  chip.title = `${label}. ${t("master.chipTitle")}`;
  const row = document.getElementById("t-export-master");
  if (row) {
    const label = row.querySelector(".chip-item-title");
    const desc = row.querySelector(".chip-item-desc");
    if (label) label.textContent = t("master.exportRow", { state });
    if (desc) desc.textContent = on ? presetSummary(m) : t("master.exportRowOff");
    row.classList.toggle("on", on);
  }
}

function presetSummary(m) {
  const p = m.params;
  const tgt = p.target_lufs === null ? t("master.targetNone") : `${fmtDb(p.target_lufs)} LUFS`;
  return `${tgt}, ${fmtDb(p.ceiling_dbtp)} dBTP`;
}

/** "-12,5" and "-12.5" alike; NaN for anything else. */
function parseNum(s) {
  const v = Number(String(s).trim().replace(",", "."));
  return String(s).trim() === "" ? NaN : v;
}

function writeFields() {
  if (!ui) return;
  const p = master.getMaster().params;
  ui.target.input.value = p.target_lufs === null ? "" : String(p.target_lufs);
  ui.target.input.placeholder = p.target_lufs === null ? t("master.targetNone") : "";
  ui.ceiling.input.value = String(p.ceiling_dbtp);
}

function paint() {
  paintChip();
  if (!ui) return;
  const on = master.isMasterOn();
  const m = master.getMaster();
  const p = m.params;
  ui.power.classList.toggle("on", on);
  ui.power.setAttribute("aria-pressed", String(on));
  ui.power.textContent = on ? t("master.power.on") : t("master.power.off");
  ui.power.title = t(on ? "master.power.onTitle" : "master.power.offTitle");
  ui.power.setAttribute("aria-label", t("master.power.aria"));
  for (const b of ui.presetBtns) {
    const sel = on && b.dataset.preset === p.preset;
    b.setAttribute("aria-pressed", String(sel));
    b.setAttribute("aria-checked", String(sel));
  }
  ui.presetDesc.textContent = t(`master.presetDesc.${p.preset}`);
  const custom = p.preset === "custom";
  ui.target.input.disabled = !on || !custom;
  ui.ceiling.input.disabled = !on || !custom;
  // A focused field is being typed into: do not overwrite it.
  if (document.activeElement !== ui.target.input) {
    ui.target.input.value = p.target_lufs === null ? "" : String(p.target_lufs);
    ui.target.input.placeholder = p.target_lufs === null ? t("master.targetNone") : "";
  }
  if (document.activeElement !== ui.ceiling.input) ui.ceiling.input.value = String(p.ceiling_dbtp);
  ui.glue.disabled = !on;
  ui.glue.value = String(Math.round(p.comp * 100));
  ui.glueVal.textContent = `${Math.round(p.comp * 100)}%`;
  const listen = master.getListen();
  for (const b of ui.listenBtns) {
    const sel = b.dataset.listen === listen;
    b.setAttribute("aria-pressed", String(sel));
    b.setAttribute("aria-checked", String(sel));
    b.disabled = !on;
  }
  ui.matchBox.checked = master.getMatch();
  ui.matchBox.disabled = !on;
  paintReadout();
  ui.exBtn.disabled = !on && !excerpt;
  ui.exBtn.textContent = excerpt ? t("master.excerptStop") : t("master.excerpt");
  const lossy = ["mp3", "ogg", "mp4"].includes(exportFormat()) && p.ceiling_dbtp > LOSSY_CEILING_DBTP;
  ui.note.textContent = lossy ? `${t("master.approx")} ${t("master.lossyNote")}` : t("master.approx");
  const eqOn = !eq.getLaneEq(eq.MASTER).bypass;
  ui.eqPower.classList.toggle("on", eqOn);
  ui.eqPower.setAttribute("aria-pressed", String(eqOn));
  ui.eqPower.textContent = eqOn ? t("eq.power.on") : t("eq.power.bypassed");
  ui.eqPower.title = t(eqOn ? "eq.power.bypassTitle" : "eq.power.enableTitle");
  ui.eqPower.setAttribute("aria-label", t("eq.power.aria", { name: t("master.title") }));
  drawer.classList.toggle("master-off", !on);
}

function paintReadout() {
  const plan = master.getPlan() || master.getLastPlan();
  const c = ui.cells;
  const on = master.isMasterOn();
  const state = master.getPlanState();
  c.bLufs.textContent = plan && on ? fmtDb(plan.pre?.i) : fmtDb(null);
  c.aLufs.textContent = plan && on ? fmtDb(plan.post?.i) : fmtDb(null);
  c.bTp.textContent = plan && on ? fmtDb(plan.pre?.tp, { sign: true }) : fmtDb(null);
  c.aTp.textContent = plan && on ? fmtDb(plan.post?.tp, { sign: true }) : fmtDb(null);
  c.aGain.textContent = plan && on ? `${fmtDb(plan.gain_db, { sign: true })} dB` : fmtDb(null);
  let key = "master.status.idle";
  if (on && state === "measuring") key = "master.status.measuring";
  else if (on && state === "error") key = "master.status.error";
  else if (on && plan && plan.reached === false) key = "master.status.notReached";
  else if (on && plan) key = "master.status.ready";
  ui.status.textContent = t(key);
  ui.status.dataset.state = on ? state : "off";
}

// ─── Meters ───

function paintBar(bar, side, ceilingDb) {
  const toPct = (db) => (Number.isFinite(db) ? Math.max(0, Math.min(100, ((db + 48) / 48) * 100)) : 0);
  bar.fill.style.height = `${toPct(side?.peak)}%`;
  bar.ceil.style.bottom = `${toPct(ceilingDb)}%`;
  bar.val.textContent = side && Number.isFinite(side.peak) ? fmtDb(side.peak) : fmtDb(null);
  bar.lufs.textContent = side && Number.isFinite(side.lufs) && side.lufs > -70
    ? fmtDb(side.lufs) : fmtDb(null);
}

function tickMeters() {
  if (!ui || !open || document.hidden) return;
  if (!meters && audioEngine?.supportsMaster) meters = master.createMeters();
  const r = meters?.read() || null;
  const ceiling = master.getMaster().params.ceiling_dbtp;
  paintBar(ui.inBar, r?.in, 0);
  paintBar(ui.outBar, r?.out, ceiling);
  // "-0.0" reads as a fault: a reduction under 0.05 dB is shown as 0.0. A
  // stopped song has nothing being reduced, whatever the compressor last held.
  const playing = !!transport()?.isPlaying?.();
  const red = r && playing && r.reduction < -0.05 ? r.reduction : 0;
  if (r && playing) master.noteLiveLoudness(r.in.lufs, r.out.lufs);
  ui.gr.textContent = r ? `${t("master.meterGr")} ${fmtDb(red)} dB` : "";
}

function startMeters() {
  if (meterTimer) return;
  meterTimer = setInterval(tickMeters, METER_MS);
}

function stopMeters() {
  if (meterTimer) clearInterval(meterTimer);
  meterTimer = 0;
  meters?.dispose();
  meters = null;
}

// ─── Render and listen (exact) ───

function excerptSpan() {
  const now = transport()?.getCurrentTime?.() ?? 0;
  const dur = totalDuration || 0;
  if (dur <= 0) return null;
  const len = Math.min(EXCERPT_SEC, dur);
  const start = Math.max(0, Math.min(dur - len, now - len / 3));
  return { start, end: start + len };
}

function excerptLevels() {
  const plan = master.getPlan() || master.getLastPlan();
  const d = plan && Number.isFinite(plan.post?.i) && Number.isFinite(plan.pre?.i)
    ? plan.post.i - plan.pre.i : 0;
  if (!master.getMatch()) return { master: 1, original: 1 };
  return d >= 0 ? { master: 10 ** (-d / 20), original: 1 } : { master: 1, original: 10 ** (d / 20) };
}

function syncExcerptSource() {
  if (!excerpt) return;
  const lv = excerptLevels();
  excerpt.master.volume = Math.min(1, lv.master);
  excerpt.original.volume = Math.min(1, lv.original);
  const which = master.getListen();
  const on = which === "original" ? excerpt.original : excerpt.master;
  const off = which === "original" ? excerpt.master : excerpt.original;
  if (!off.paused) {
    on.currentTime = off.currentTime;
    off.pause();
  }
  if (on.paused) on.play().catch((err) => console.warn("[master] excerpt playback failed:", err));
}

async function startExcerpt() {
  const span = excerptSpan();
  if (!span || !ui || excerptCtl) return;
  // MP3 for a lossy export (its own ceiling), WAV otherwise: the excerpt is
  // what that export will be.
  const ext = ["mp3", "ogg", "mp4"].includes(exportFormat()) ? "mp3" : "wav";
  const mUrl = excerptUrl(ext, span.start, span.end, true);
  const oUrl = excerptUrl(ext, span.start, span.end, false);
  if (!mUrl || !oUrl) return;
  if (transport()?.isPlaying?.()) togglePlayPause();
  const ctl = new AbortController();
  excerptCtl = ctl;
  ui.exBtn.disabled = true;
  ui.exState.textContent = t("master.excerptRendering");
  try {
    const [mBlob, oBlob] = await Promise.all([mUrl, oUrl].map(async (u) => {
      const r = await fetch(u, { signal: ctl.signal });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.blob();
    }));
    if (ctl.signal.aborted || !open) return;
    const mk = (blob) => {
      const a = new Audio(URL.createObjectURL(blob));
      a.preload = "auto";
      return a;
    };
    excerpt = { master: mk(mBlob), original: mk(oBlob), ...span };
    excerpt.master.addEventListener("ended", stopExcerpt);
    excerpt.original.addEventListener("ended", stopExcerpt);
    ui.exState.textContent = t("master.excerptPlaying", {
      start: clock(span.start), end: clock(span.end),
    });
    drawer.dataset.excerpt = "playing";
    syncExcerptSource();
  } catch (err) {
    if (ctl.signal.aborted) return;
    console.warn("[master] rendering the excerpt failed:", err);
    ui.exState.textContent = t("master.excerptFailed");
    excerpt = null;
  } finally {
    if (excerptCtl === ctl) excerptCtl = null;
    if (ui) paint();
  }
}

/** Stop a render in flight: the fetches end and nothing plays. */
function cancelExcerptRender() {
  if (!excerptCtl) return;
  excerptCtl.abort();
  excerptCtl = null;
  if (ui) ui.exState.textContent = "";
}

function stopExcerpt() {
  if (!excerpt) return;
  for (const a of [excerpt.master, excerpt.original]) {
    a.pause();
    URL.revokeObjectURL(a.src);
  }
  excerpt = null;
  delete drawer?.dataset.excerpt;
  if (ui) ui.exState.textContent = "";
  paint();
}

function clock(sec) {
  const s = Math.max(0, Math.round(sec));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

// ─── Open and close ───

export function setOpen(next) {
  if (!drawer) return;
  open = !!next;
  drawer.hidden = !open;
  if (open) {
    if (!ui) build();
    mountEq();
    paint();
    startMeters();
    master.watch();
    if (master.isMasterOn() && !master.getPlan()) master.measure();
  } else {
    stopMeters();
    cancelExcerptRender();
    stopExcerpt();
  }
  // Open: the live master runs silently, settled, ready to be switched on.
  master.warmLive(open);
  paintChip();
  // The lanes give the drawer its room: lay them out again.
  document.dispatchEvent(new CustomEvent("stemdeck:master-drawer", { detail: { open } }));
}

export function isOpen() { return open; }

export function initMasterPanel() {
  drawer = document.getElementById("master-drawer");
  chip = document.getElementById("t-master-btn");
  if (!drawer || !chip) return;
  chip.addEventListener("click", (e) => {
    e.stopPropagation();
    setOpen(!open);
  });
  // The studio's Space shortcut plays; on this button Space is the button's.
  chip.addEventListener("keydown", (e) => {
    if (e.code === "Space" || e.key === "Enter") e.stopPropagation();
  });
  const row = document.getElementById("t-export-master");
  row?.addEventListener("click", (e) => {
    e.stopPropagation();
    document.getElementById("t-export-panel")?.classList.add("hidden");
    document.getElementById("t-export-btn")?.setAttribute("aria-expanded", "false");
    setOpen(true);
    ui?.power.focus();
  });
  master.onChange((what) => {
    if (what === null) { cancelExcerptRender(); stopExcerpt(); }
    // Off, the warm master follows the preset the power button would use.
    if (open && what === "master" && !master.isMasterOn()) master.warmLive(true);
    paint();
  });
  eq.onChange(({ laneId }) => { if (laneId === eq.MASTER || laneId === null) paint(); });
  onLanguageChange(() => { relabel(); paintChip(); });
  // Practice mode has no master: its drawer closes.
  onModeChange((m) => { if (m === "practice") setOpen(false); });
  for (const id of ["t-fmt-wav", "t-fmt-mp3", "t-fmt-flac", "t-fmt-ogg", "t-fmt-mp4"]) {
    document.getElementById(id)?.addEventListener("click", () => paint());
  }
  paintChip();
}
