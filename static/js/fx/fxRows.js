// The FX tab on a lane and the effects row it opens (Advanced mode only).
//
// Each lane that can carry a chain (a single stem or a recording track,
// chainModel.laneTakesChain) gets an "FX" tab in the head of the EQ row its
// rail's EQ tab opens: the lane's second panel. It is not a fifth rail cell,
// because five cells in the shortest (72 px) row are 14 px each, under the
// 17 px a rail cell needs. The tab opens a row right under the EQ row, in
// both columns, the way the EQ row opens (eqRows.js, which also lays these
// rows out: eqPanelsHeight, syncEqRows and syncEqRowOrder call in here). The
// effects row is open only while the EQ row is; closing the EQ row closes
// both, and opening it again brings the effects row back. While the effects
// change the sound the rail's EQ tab carries a small square (fx-on), so the
// state shows with both rows closed.
//
//   mixer column:    Effects (lane) [On]  Instrument [Auto]  Trim (knob)  meter
//   waveform column: In > [Delay  On  x] > [Delay 2  On  x] > [+ Add effect] > Out
//                    the selected pedal: preset, its selects and its knobs
//
// The strip itself (cards, reorder, pedal panel, trim, meter) is
// fx/chainStrip.js, shared with the live input's chain in the recording
// panel. Nothing here touches audio directly: every change is a new chain
// descriptor for fxEngine.setLaneChain, which saves it and tells the engine.

import { STEM_COLORS, STEM_DISPLAY, isTakeName } from "../constants.js";
import { mixerEl } from "../state.js";
import { t, onLanguageChange } from "../i18n.js";
import { getTracks } from "../capture/takeLanes.js";
import * as fx from "../fxEngine.js";
import { laneTakesChain } from "./chainModel.js";
import { mountChainStrip } from "./chainStrip.js";
import { openKey, parseOpenStore, withOpenChoice } from "../eq/eqPanelKinds.js";

/** Height of an effects row, px, the same in both columns. */
export const FX_PANEL_HEIGHT = 200;
const OPEN_STORE_KEY = "stemdeck.fxPanels";

let _want = false;
let _jobId = null;
let _lanes = [];
let _store = null;
// eqRows.isPanelOpen, passed in by setFxContext (eqRows imports this module).
let _panelOpen = () => false;
// lane -> { waveRow, mixRow, parts }
const _rows = new Map();

const enabled = () => _want && !!_jobId && fx.chainsAllowed();

// ─── Open state ───

function loadStore() {
  if (_store) return _store;
  let raw = null;
  try {
    raw = localStorage.getItem(OPEN_STORE_KEY);
  } catch (e) {
    console.warn("[fx] reading the open effects rows failed:", e);
  }
  _store = parseOpenStore(raw);
  return _store;
}

function saveOpen(lane, open) {
  _store = withOpenChoice(loadStore(), openKey(_jobId, lane), open);
  try {
    localStorage.setItem(OPEN_STORE_KEY, JSON.stringify(_store));
  } catch (e) {
    console.warn("[fx] saving the open effects rows failed:", e);
  }
}

export function isFxOpen(lane) {
  return enabled() && _lanes.includes(lane) && _panelOpen(lane) && loadStore()[openKey(_jobId, lane)] === true;
}

/** Height the open effects rows add to the lane stack (px). */
export function fxPanelsHeight() {
  if (!enabled()) return 0;
  return _lanes.filter(isFxOpen).length * FX_PANEL_HEIGHT;
}

/** The song and its lanes (eqRows.setEqContext passes them on). */
export function setFxContext({ jobId, lanes, enabled: want, panelOpen }) {
  if (typeof panelOpen === "function") _panelOpen = panelOpen;
  if (jobId !== _jobId) for (const lane of [..._rows.keys()]) dropRows(lane);
  _jobId = jobId || null;
  _lanes = (Array.isArray(lanes) ? lanes : []).filter(laneTakesChain);
  _want = !!want;
  if (!enabled()) for (const lane of [..._rows.keys()]) dropRows(lane);
}

function setOpen(lane, open) {
  if (!enabled() || !_lanes.includes(lane)) return;
  saveOpen(lane, !!open);
  if (!open) dropRows(lane);
  document.dispatchEvent(new CustomEvent("stemdeck:eq-rows-changed", { detail: { lane, open: !!open } }));
}

// ─── Names ───

function displayName(lane) {
  if (isTakeName(lane)) return getTracks().find((tr) => tr.id === lane)?.name || STEM_DISPLAY[lane] || lane;
  return STEM_DISPLAY[lane] || lane;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

// ─── Tabs ───

function laneMixRow(lane) {
  return mixerEl?.querySelector(`.lane-header[data-stem="${CSS.escape(lane)}"]`) || null;
}
function laneWaveRow(lane) {
  return document.querySelector(`.stem-waveform-layer .stem-waveform-row[data-stem="${CSS.escape(lane)}"]`) || null;
}

/** The slot in the head of the lane's EQ row (eqRows.buildRows). */
function laneFxSlot(lane) {
  return mixerEl?.querySelector(`.eq-panel-row[data-eq-lane="${CSS.escape(lane)}"] .eq-fx-slot`) || null;
}

function ensureTab(lane) {
  const slot = laneFxSlot(lane);
  if (!slot) return;
  let tab = slot.querySelector(":scope > .fx-tab");
  if (!tab) {
    tab = el("button", "eq-tab fx-tab");
    tab.type = "button";
    tab.dataset.fxLane = lane;
    const dot = el("span", "eq-tab-dot");
    dot.setAttribute("aria-hidden", "true");
    tab.append(el("span", "eq-tab-label"), dot);
    tab.addEventListener("click", (e) => {
      e.stopPropagation();
      setOpen(lane, !isFxOpen(lane));
    });
    slot.append(tab);
  }
  paintTab(tab, lane);
}

function paintTab(tab, lane) {
  if (!tab) return;
  const open = isFxOpen(lane);
  const desc = fx.getLaneChain(lane);
  const active = fx.isLaneActive(lane);
  tab.querySelector(".eq-tab-label").textContent = t("fx.tab");
  tab.classList.toggle("open", open);
  tab.classList.toggle("active", active);
  tab.classList.toggle("bypassed", !active && desc.pedals.length > 0);
  tab.setAttribute("aria-expanded", String(open));
  if (open) tab.setAttribute("aria-controls", `fx-panel-${lane}`);
  else tab.removeAttribute("aria-controls");
  const name = displayName(lane);
  tab.setAttribute("aria-label", t(open ? "fx.tab.closeAria" : "fx.tab.openAria", { name }));
  tab.title = active ? t("fx.tab.titleActive") : t("fx.tab.title");
}

function dropTabs(keep) {
  for (const tab of document.querySelectorAll(".eq-fx-slot > .fx-tab")) {
    if (!keep || !keep.includes(tab.dataset.fxLane)) tab.remove();
  }
}

/**
 * The rail's EQ tabs: "fx-on" while the lane's effects change the sound
 * (none when the effects are off), so it shows with the rows closed.
 */
function markRailTabs() {
  for (const tab of document.querySelectorAll(".mx-eq-slot > .eq-tab[data-eq-lane]")) {
    const lane = tab.dataset.eqLane;
    tab.classList.toggle("fx-on", enabled() && _lanes.includes(lane) && fx.isLaneActive(lane));
  }
}

// ─── Rows ───

function dropRows(lane) {
  const r = _rows.get(lane);
  if (!r) return;
  r.parts.destroy();
  r.waveRow.remove();
  r.mixRow.remove();
  _rows.delete(lane);
}

function buildRows(lane) {
  const h = FX_PANEL_HEIGHT;
  const color = STEM_COLORS[lane] || "#a0a0a0";
  const waveRow = el("div", "stem-waveform-row eq-panel-lane fx-panel-lane");
  waveRow.dataset.fxLane = lane;
  waveRow.id = `fx-panel-${lane}`;
  waveRow.setAttribute("role", "region");
  waveRow.style.setProperty("--stem-color", color);
  waveRow.style.flex = `0 0 ${h}px`;
  waveRow.style.height = `${h}px`;
  const body = el("div", "eq-panel-body fx-panel-body");
  body.addEventListener("pointerdown", (e) => e.stopPropagation());
  body.addEventListener("click", (e) => e.stopPropagation());
  body.addEventListener("wheel", (e) => e.stopPropagation(), { passive: true });
  waveRow.append(body);

  const mixRow = el("div", "lane-header mx-row eq-panel-row fx-panel-row");
  mixRow.dataset.fxLane = lane;
  mixRow.style.height = `${h}px`;
  mixRow.style.minHeight = `${h}px`;
  mixRow.style.setProperty("--stem-color", color);
  const side = el("div", "eq-side fx-side");
  mixRow.append(side);
  const parts = mountChain(lane, side, body, waveRow);
  return { waveRow, mixRow, parts };
}

/** Put the tabs and the open rows in place (eqRows.syncEqRows calls it). */
export function syncFxRows() {
  if (!enabled()) {
    for (const lane of [..._rows.keys()]) dropRows(lane);
    dropTabs(null);
    markRailTabs();
    return;
  }
  dropTabs(_lanes);
  for (const lane of _lanes) ensureTab(lane);
  // A lane whose EQ row is closed has no FX tab to paint its mark.
  markRailTabs();
  for (const lane of [..._rows.keys()]) if (!isFxOpen(lane)) dropRows(lane);
  for (const lane of _lanes) {
    if (!isFxOpen(lane)) continue;
    const waveLane = laneWaveRow(lane);
    const mixLane = laneMixRow(lane);
    if (!waveLane || !mixLane) continue;
    let r = _rows.get(lane);
    if (!r) {
      r = buildRows(lane);
      _rows.set(lane, r);
    }
    // After the lane's EQ row when it is open, else right after the lane.
    const waveAfter = waveLane.nextElementSibling?.matches?.(`.eq-panel-lane[data-eq-lane="${CSS.escape(lane)}"]`) ? waveLane.nextElementSibling : waveLane;
    const mixAfter = mixLane.nextElementSibling?.matches?.(`.eq-panel-row[data-eq-lane="${CSS.escape(lane)}"]`) ? mixLane.nextElementSibling : mixLane;
    if (waveAfter.nextElementSibling !== r.waveRow) waveAfter.after(r.waveRow);
    if (mixAfter.nextElementSibling !== r.mixRow) mixAfter.after(r.mixRow);
    r.parts.refresh();
  }
  syncFxRowOrder();
}

/** Each row's CSS order: the lane's + 1, as the EQ row (DOM order breaks the tie). */
export function syncFxRowOrder() {
  for (const [lane, r] of _rows) {
    const wo = Number(laneWaveRow(lane)?.style.order);
    const m = laneMixRow(lane);
    const mo = m?.style.order === "" ? NaN : Number(m?.style.order);
    r.waveRow.style.order = Number.isFinite(wo) ? String(wo + 1) : "";
    r.mixRow.style.order = Number.isFinite(mo) ? String(mo + 1) : "";
  }
}

// ─── One lane's chain ───

/** The lane as a chain source for the shared strip (fx/chainStrip.js). */
function laneSource(lane) {
  let buf = null;
  return {
    key: `lane:${lane}`,
    name: () => displayName(lane),
    color: STEM_COLORS[lane] || "#a0a0a0",
    get: () => fx.getLaneChain(lane),
    set: (desc) => fx.setLaneChain(lane, desc),
    inst: () => fx.laneChainInstrument(lane),
    defaultInst: () => fx.laneDefaultInstrument(lane),
    meterPeak() {
      const an = fx.getChainAnalyser(lane, { fftSize: 2048, smoothing: 0 });
      if (!an) return null;
      if (!buf || buf.length !== an.fftSize) buf = new Float32Array(an.fftSize);
      an.getFloatTimeDomainData(buf);
      let peak = 0;
      for (let i = 0; i < buf.length; i++) { const a = Math.abs(buf[i]); if (a > peak) peak = a; }
      return peak;
    },
    onChange: (cb) => fx.onChange(({ lane: changed }) => { if (changed === null || changed === lane) cb(); }),
    release: () => fx.releaseChainAnalyser(lane),
  };
}

function mountChain(lane, side, body, waveRow) {
  return mountChainStrip(laneSource(lane), side, body, waveRow);
}

// ─── Events ───

fx.onChange(({ lane }) => {
  markRailTabs();
  if (lane === null) { for (const tab of document.querySelectorAll(".fx-tab[data-fx-lane]")) paintTab(tab, tab.dataset.fxLane); return; }
  paintTab(document.querySelector(`.fx-tab[data-fx-lane="${CSS.escape(lane)}"]`), lane);
});

onLanguageChange(() => {
  for (const tab of document.querySelectorAll(".fx-tab[data-fx-lane]")) paintTab(tab, tab.dataset.fxLane);
  for (const r of _rows.values()) r.parts.refresh();
});

// The mix in force changed (another mode): the tabs and rows come or go, and
// the studio lays the lanes out again for their height.
document.addEventListener("stemdeck:mix-switched", () => {
  syncFxRows();
  document.dispatchEvent(new CustomEvent("stemdeck:eq-rows-changed"));
});
