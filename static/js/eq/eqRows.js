// The EQ tab on every lane, and the EQ panel row it opens.
//
// Each lane's header in the mixer column carries an "EQ" tab, at the start of
// the fader's line (mixer.js leaves a slot for it), so the waveform lane is
// only the waveform. A click opens that lane's EQ panel as an extra row right
// under the lane, in
// both columns: a row in the mixer column (the panel's title and its global
// switches) and a row in the waveform column (the editor itself), so the two
// columns keep the same rows at the same heights, the way an open take folder
// adds its take lanes. Open or closed is remembered per song and lane.
//
// Which panel a lane gets is fixed by its kind (eqPanelKinds.js). The panel's
// contents are separate modules loaded on first open (dynamic import), so a
// studio that never opens an EQ never downloads one.
//
// The panel rows exist only where the waveform column is the SVG overlay (the
// Web Audio engine path), like take folders: player.js says so.

import { isPractice, onModeChange } from "../mode.js";
import { STEM_COLORS, STEM_DISPLAY, isTakeName } from "../constants.js";
import { mixerEl } from "../state.js";
import { t, onLanguageChange } from "../i18n.js";
import * as eq from "../eqEngine.js";
import { getTracks, trackInput } from "../capture/takeLanes.js";
import {
  panelKindFor, panelHeight, laneHasPanel, eqRowOrder, openPanelsHeight,
  OPEN_STORE_KEY, openKey, parseOpenStore, withOpenChoice,
} from "./eqPanelKinds.js";

// The panel modules, by kind, loaded on first open.
const LOADERS = {
  graph: () => import("./eqGraphPanel.js"),
  drum: () => import("./eqGraphPanel.js"),
  bass: () => import("./eqPedalPanel.js"),
  guitar: () => import("./eqPedalPanel.js"),
  keys: () => import("./eqPedalPanel.js"),
  voice: () => import("./eqVoicePanel.js"),
};

const TITLE_KEYS = {
  graph: "eq.panel.general",
  drum: "eq.panel.drum",
  bass: "eq.panel.bass",
  guitar: "eq.panel.guitar",
  keys: "eq.panel.keys",
  voice: "eq.panel.voice",
};

let _enabled = false;
// What player.js asked for (an engine that can run the EQ) and whether the
// mode allows it (mode.js: Practice has no EQ). Enabled when both say so.
let _want = false;
let _allowed = !isPractice();
let _jobId = null;
let _lanes = [];
let _store = null;
// lane -> { kind, waveRow, mixRow, panel, loading }
const _rows = new Map();
let _viewObs = null;

// ─── Model ───

function loadStore() {
  if (_store) return _store;
  let raw = null;
  try {
    raw = localStorage.getItem(OPEN_STORE_KEY);
  } catch (e) {
    console.warn("[eq] reading the open EQ panels failed:", e);
  }
  _store = parseOpenStore(raw);
  return _store;
}

function saveOpen(lane, open) {
  _store = withOpenChoice(loadStore(), openKey(_jobId, lane), open);
  try {
    localStorage.setItem(OPEN_STORE_KEY, JSON.stringify(_store));
  } catch (e) {
    // Private windows and full storage: the choice holds for this page only.
    console.warn("[eq] saving the open EQ panels failed:", e);
  }
}

export function isPanelOpen(lane) {
  if (!_enabled || !_jobId || !laneHasPanel(lane)) return false;
  return loadStore()[openKey(_jobId, lane)] === true;
}

/** The panel kind of a lane of the loaded song. */
export function laneKind(lane) {
  if (!isTakeName(lane)) return panelKindFor(lane);
  const track = getTracks().find((tr) => tr.id === lane);
  let inputKind = null;
  try {
    inputKind = track ? trackInput(track)?.kind || null : null;
  } catch (e) {
    console.warn("[eq] reading a track's input kind failed:", e);
  }
  return panelKindFor(lane, { isTrack: true, inputKind });
}

/** Height the open panels add to the lane stack (px). player.js uses it. */
export function eqPanelsHeight() {
  if (!_enabled) return 0;
  return openPanelsHeight(_lanes, isPanelOpen, laneKind);
}

/**
 * The loaded song and its lanes, in stack order. Called by player.js before
 * it lays the lanes out, so the stack height counts the open panels.
 */
export function setEqContext({ jobId, lanes, enabled }) {
  if (jobId !== _jobId) {
    for (const lane of [..._rows.keys()]) dropRows(lane);
  }
  _jobId = jobId || null;
  _lanes = Array.isArray(lanes) ? lanes.filter(laneHasPanel) : [];
  _want = !!enabled;
  _enabled = _want && _allowed && !!_jobId;
  if (!_enabled) for (const lane of [..._rows.keys()]) dropRows(lane);
}

// Practice and DAW: the tabs and the open panels go or come back, and the
// studio lays the lanes out again for the panels' height.
onModeChange(() => {
  _allowed = !isPractice();
  _enabled = _want && _allowed && !!_jobId;
  syncEqRows();
  document.dispatchEvent(new CustomEvent("stemdeck:eq-rows-changed"));
});

/** Open or close a lane's panel, and ask the studio to lay the lanes out again. */
export function setPanelOpen(lane, open) {
  if (!_enabled || !_lanes.includes(lane)) return;
  saveOpen(lane, !!open);
  if (!open) dropRows(lane);
  document.dispatchEvent(new CustomEvent("stemdeck:eq-rows-changed", { detail: { lane, open: !!open } }));
}

// ─── Rows ───

function displayName(lane) {
  if (isTakeName(lane)) {
    return getTracks().find((tr) => tr.id === lane)?.name || STEM_DISPLAY[lane] || lane;
  }
  return STEM_DISPLAY[lane] || lane;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function waveLayer() {
  return document.querySelector(".stem-waveform-layer");
}

function laneWaveRow(lane) {
  return waveLayer()?.querySelector(`.stem-waveform-row[data-stem="${CSS.escape(lane)}"]`) || null;
}

function laneMixRow(lane) {
  return mixerEl?.querySelector(`.lane-header[data-stem="${CSS.escape(lane)}"]`) || null;
}

/**
 * The tab in a lane's header, in the slot mixer.js leaves for it. A recording
 * track's row is rebuilt when the tracks change, so this runs after every
 * redraw and adds the tab where it is missing.
 */
function ensureTab(lane) {
  const slot = laneMixRow(lane)?.querySelector(".mx-eq-slot");
  if (!slot) return;
  let tab = slot.querySelector(":scope > .eq-tab");
  if (!tab) {
    tab = el("button", "eq-tab");
    tab.type = "button";
    tab.dataset.eqLane = lane;
    tab.append(el("span", "eq-tab-label", "EQ"), el("span", "eq-tab-dot"));
    tab.querySelector(".eq-tab-dot").setAttribute("aria-hidden", "true");
    tab.addEventListener("click", (e) => {
      e.stopPropagation();
      setPanelOpen(lane, !isPanelOpen(lane));
    });
    slot.append(tab);
  }
  paintTab(tab, lane);
}

/** Tabs whose lane is no longer on screen, or every tab when the EQ is off. */
function dropTabs(keep) {
  for (const tab of document.querySelectorAll(".mx-eq-slot > .eq-tab")) {
    if (!keep || !keep.includes(tab.dataset.eqLane)) tab.remove();
  }
}

function paintTab(tab, lane) {
  if (!tab) return;
  const open = isPanelOpen(lane);
  const cur = eq.getLaneEq(lane);
  const active = eq.isLaneActive(lane);
  const kind = laneKind(lane);
  const name = displayName(lane);
  tab.classList.toggle("open", open);
  tab.classList.toggle("active", active);
  tab.classList.toggle("bypassed", !!cur.bypass && cur.params.bands.length > 0);
  tab.setAttribute("aria-expanded", String(open));
  const rowId = `eq-panel-${lane}`;
  if (open) tab.setAttribute("aria-controls", rowId);
  else tab.removeAttribute("aria-controls");
  const title = t(TITLE_KEYS[kind]);
  tab.setAttribute("aria-label", t(open ? "eq.tab.closeAria" : "eq.tab.openAria", { panel: title, name }));
  tab.title = active ? t("eq.tab.titleActive", { panel: title }) : (cur.bypass ? t("eq.tab.titleBypassed", { panel: title }) : title);
}

function paintAllTabs() {
  for (const tab of document.querySelectorAll(".eq-tab[data-eq-lane]")) paintTab(tab, tab.dataset.eqLane);
}

function dropRows(lane) {
  const r = _rows.get(lane);
  if (!r) return;
  try {
    r.panel?.destroy?.();
  } catch (e) {
    console.warn("[eq] closing a panel failed:", e);
  }
  r.waveRow?.remove();
  r.mixRow?.remove();
  _rows.delete(lane);
}

function buildRows(lane, kind) {
  const h = panelHeight(kind);
  const color = STEM_COLORS[lane] || "#a0a0a0";
  const name = displayName(lane);
  const title = t(TITLE_KEYS[kind]);

  const waveRow = el("div", "stem-waveform-row eq-panel-lane");
  waveRow.dataset.eqLane = lane;
  waveRow.dataset.eqKind = kind;
  waveRow.id = `eq-panel-${lane}`;
  waveRow.setAttribute("role", "region");
  waveRow.setAttribute("aria-label", t("eq.panel.regionAria", { panel: title, name }));
  waveRow.style.setProperty("--stem-color", color);
  waveRow.style.flex = `0 0 ${h}px`;
  waveRow.style.height = `${h}px`;
  const body = el("div", "eq-panel-body");
  // Clicks, drags and wheel turns inside the panel are the panel's own: the
  // waves column would start a seek or a loop drag on pointerdown.
  body.addEventListener("pointerdown", (e) => e.stopPropagation());
  body.addEventListener("click", (e) => e.stopPropagation());
  // A wheel turn over the lanes zooms them; over a panel it belongs to the
  // panel (a band's Q), or scrolls the stack as usual.
  body.addEventListener("wheel", (e) => e.stopPropagation(), { passive: true });
  waveRow.append(body);

  const mixRow = el("div", "lane-header mx-row eq-panel-row");
  mixRow.dataset.eqLane = lane;
  mixRow.dataset.eqKind = kind;
  mixRow.style.height = `${h}px`;
  mixRow.style.minHeight = `${h}px`;
  mixRow.style.setProperty("--stem-color", color);
  const side = el("div", "eq-side");
  const head = el("div", "eq-side-head");
  const titleEl = el("span", "eq-side-title", title);
  titleEl.style.color = color;
  const laneEl = el("span", "eq-side-lane", name);
  laneEl.title = name;
  const titles = el("div", "eq-side-titles");
  // "Bass EQ" already names its lane; a general or voice panel says which.
  titles.append(titleEl);
  if (kind === "graph" || kind === "voice") titles.append(laneEl);
  head.append(titles);
  const controls = el("div", "eq-side-controls");
  if (kind !== "voice") {
    const power = el("button", "eq-power");
    power.type = "button";
    power.addEventListener("click", () => {
      eq.setBypass(lane, !eq.getLaneEq(lane).bypass);
    });
    head.append(power);
  }
  side.append(head, controls);
  mixRow.append(side);
  return { kind, waveRow, mixRow, body, controls, panel: null, loading: false };
}

function paintPower(lane) {
  const r = _rows.get(lane);
  const power = r?.mixRow.querySelector(".eq-power");
  if (!power) return;
  const on = !eq.getLaneEq(lane).bypass;
  power.classList.toggle("on", on);
  power.setAttribute("aria-pressed", String(on));
  power.textContent = on ? t("eq.power.on") : t("eq.power.bypassed");
  power.title = t(on ? "eq.power.bypassTitle" : "eq.power.enableTitle");
  power.setAttribute("aria-label", t("eq.power.aria", { name: displayName(lane) }));
}

async function mountPanel(lane) {
  const r = _rows.get(lane);
  if (!r || r.panel || r.loading) return;
  const load = LOADERS[r.kind];
  if (!load) return;
  r.loading = true;
  try {
    const mod = await load();
    // Closed (or replaced) while the module loaded.
    if (_rows.get(lane) !== r) return;
    r.panel = mod.mountPanel({
      lane,
      kind: r.kind,
      body: r.body,
      controls: r.controls,
      name: displayName(lane),
      color: STEM_COLORS[lane] || "#a0a0a0",
    });
  } catch (e) {
    console.warn("[eq] loading the EQ panel failed:", e);
  } finally {
    r.loading = false;
  }
}

/**
 * Put the tabs in the lane headers and the open panels under the lanes, in
 * both columns. player.js calls it after every redraw of the lanes and after
 * the stack changes.
 */
export function syncEqRows() {
  ensureViewWidth();
  if (!_enabled) {
    for (const lane of [..._rows.keys()]) dropRows(lane);
    dropTabs(null);
    return;
  }
  dropTabs(_lanes);
  for (const lane of _lanes) ensureTab(lane);
  for (const lane of [..._rows.keys()]) {
    if (!_lanes.includes(lane) || !isPanelOpen(lane) || _rows.get(lane).kind !== laneKind(lane)) dropRows(lane);
  }
  for (const lane of _lanes) {
    if (!isPanelOpen(lane)) continue;
    const waveParent = laneWaveRow(lane);
    const mixParent = laneMixRow(lane);
    if (!waveParent || !mixParent) continue;
    let r = _rows.get(lane);
    if (!r) {
      r = buildRows(lane, laneKind(lane));
      _rows.set(lane, r);
    }
    // DOM order follows the stack too: anything reading rows in document
    // order (the alignment tests do) sees them where they are drawn.
    if (waveParent.nextElementSibling !== r.waveRow) waveParent.after(r.waveRow);
    if (mixParent.nextElementSibling !== r.mixRow) mixParent.after(r.mixRow);
    paintPower(lane);
    mountPanel(lane);
  }
  syncEqRowOrder();
}

/** A mounted panel's own measurements (tests read the spectrum's cost). */
export function eqPanelStats(lane) {
  return _rows.get(lane)?.panel?.stats?.() || null;
}

/** Give each panel row its place: right under its lane, in both columns. */
export function syncEqRowOrder() {
  for (const [lane, r] of _rows) {
    const w = laneWaveRow(lane);
    const m = laneMixRow(lane);
    const wo = Number(w?.style.order);
    const mo = m?.style.order === "" ? NaN : Number(m?.style.order);
    r.waveRow.style.order = Number.isFinite(wo) ? String(eqRowOrder(wo)) : "";
    r.mixRow.style.order = Number.isFinite(mo) ? String(eqRowOrder(mo)) : "";
  }
}

/** The visible width of the waveform area, for panels that span it at any zoom. */
function ensureViewWidth() {
  const scroll = document.getElementById("wave-scroll");
  if (!scroll || _viewObs) return;
  const apply = () => scroll.style.setProperty("--eq-view-w", `${scroll.clientWidth}px`);
  apply();
  if (typeof ResizeObserver === "function") {
    _viewObs = new ResizeObserver(apply);
    _viewObs.observe(scroll);
  }
}

// ─── Events ───

eq.onChange(({ laneId }) => {
  if (laneId === null) {
    paintAllTabs();
    for (const lane of _rows.keys()) paintPower(lane);
    for (const r of _rows.values()) r.panel?.refresh?.();
    return;
  }
  const tab = document.querySelector(`.eq-tab[data-eq-lane="${CSS.escape(laneId)}"]`);
  paintTab(tab, laneId);
  paintPower(laneId);
});

onLanguageChange(() => {
  paintAllTabs();
  for (const [lane, r] of _rows) {
    const kind = r.kind;
    const titleEl = r.mixRow.querySelector(".eq-side-title");
    if (titleEl) titleEl.textContent = t(TITLE_KEYS[kind]);
    paintPower(lane);
    r.panel?.relabel?.();
  }
});

// A track's input moved between a microphone and an instrument: its panel kind
// may have changed with it.
document.addEventListener("stemdeck:tracks-armed", () => {
  if (!_enabled) return;
  for (const [lane, r] of _rows) {
    if (r.kind !== laneKind(lane)) {
      document.dispatchEvent(new CustomEvent("stemdeck:eq-rows-changed", { detail: { lane, open: true } }));
      return;
    }
  }
});
