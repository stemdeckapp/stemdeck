// What every EQ panel shares: the edit state of one lane (undo and redo, A/B
// compare, presets, reset), the toolbar in the panel's mixer-side row, a
// small response curve with the lane's live spectrum behind it, the analyser
// choice, the canvas colours from the design tokens, and the
// keyboard containment that keeps panel keys away from the studio's global
// shortcuts (Space plays, Delete moves the song to the Trash).

import * as eq from "../eqEngine.js";
import { eqResponseDb } from "../eqMath.js";
import { t } from "../i18n.js";
import {
  createHistory, fmtDb, SPEC_MIN_DB, SPEC_MAX_DB, SPEC_TILT_DB_OCT, columnBins, spectrumFrame,
} from "./eqGraphMath.js";

const ANALYZER_KEY = "stemdeck.eqAnalyzer";
export const ANALYZER_MODES = ["off", "pre", "post"];
// The spectrum falls back this many dB per frame at most (about 18 dB/s at
// 60 frames a second): a separated drum stem is near silence between hits,
// and a faster fall made it flicker out of sight.
export const SPEC_FALL_DB = 0.3;

/** The analyser choice (Off, Pre, Post), one for every panel; "post" when none is saved. */
export function readAnalyzerMode() {
  try {
    const v = localStorage.getItem(ANALYZER_KEY);
    return ANALYZER_MODES.includes(v) ? v : "post";
  } catch (e) {
    console.warn("[eq] reading the analyser choice failed:", e);
    return "post";
  }
}

export function saveAnalyzerMode(v) {
  try {
    localStorage.setItem(ANALYZER_KEY, v);
  } catch (e) {
    console.warn("[eq] saving the analyser choice failed:", e);
  }
}

export function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

export function button(cls, text, title) {
  const b = el("button", cls, text);
  b.type = "button";
  if (title) b.title = title;
  return b;
}

/** Design tokens the canvases draw with, read once per mount. */
export function readColors(node) {
  const cs = getComputedStyle(node);
  const v = (name, fallback) => cs.getPropertyValue(name).trim() || fallback;
  return {
    bg: v("--eq-graph-bg", "#090c0f"),
    grid: v("--eq-grid", "rgba(148,163,184,0.09)"),
    zero: v("--eq-grid-zero", "rgba(148,163,184,0.26)"),
    label: v("--eq-label", "#6b757e"),
    spectrum: v("--eq-spectrum", "rgba(148,163,184,0.2)"),
    spectrumLine: v("--eq-spectrum-line", "rgba(148,163,184,0.42)"),
    curve: v("--accent", "#f4b740"),
    curveFill: v("--eq-curve-fill", "rgba(244,183,64,0.1)"),
    ghost: v("--eq-ghost", "rgba(125,211,252,0.9)"),
    fg: v("--fg", "#e8ecf0"),
    muted: v("--muted", "#8a939c"),
    stem: v("--stem-color", "#f4b740"),
    bands: [1, 2, 3, 4, 5, 6].map((i) => v(`--eq-band-${i}`, "#f4b740")),
    font: v("--font-mono", "monospace"),
  };
}

/**
 * Keys pressed inside a panel stay inside it when the panel uses them:
 * Delete and Backspace (the library would move the song to the Trash), and
 * Space or Enter on a control (the transport would start playing).
 */
export function containKeys(root) {
  root.addEventListener("keydown", (e) => {
    if (e.key === "Delete" || e.key === "Backspace") { e.stopPropagation(); return; }
    const control = e.target?.closest?.("button, select, input, [role='slider'], [tabindex]");
    if (control && (e.code === "Space" || e.key === "Enter" || e.key.startsWith("Arrow")
      || e.key === "PageUp" || e.key === "PageDown" || e.key === "Home" || e.key === "End")) {
      e.stopPropagation();
    }
  });
}

/** Strip the A/B slot from a panel state blob. */
function withoutAb(ui) {
  if (!ui || typeof ui !== "object") return {};
  const { ab, ...rest } = ui;
  return rest;
}

/**
 * The edit state of one lane's panel. Every change goes through apply(),
 * which records the state before it for undo (unless `record` is false, as
 * for the steps of one drag after its first), and saves through the engine.
 */
export function createEditState(lane, { onApplied } = {}) {
  const history = createHistory(100);
  const state = {
    lane,
    history,
    current() {
      return eq.getLaneEq(lane);
    },
    /** `desc` replaces the lane's EQ (normalised and saved by the engine). */
    apply(desc, { record = true } = {}) {
      if (record) history.push(eq.getLaneEq(lane));
      const out = eq.setLaneEq(lane, desc);
      onApplied?.(out);
      return out;
    },
    /** Change the params, keeping bypass and anything not given. */
    applyParams(patch, opts) {
      const cur = eq.getLaneEq(lane);
      return state.apply({ ...cur, params: { ...cur.params, ...patch } }, opts);
    },
    undo() {
      const prev = history.undo(eq.getLaneEq(lane));
      if (!prev) return null;
      const out = eq.setLaneEq(lane, prev);
      onApplied?.(out);
      return out;
    },
    redo() {
      const next = history.redo(eq.getLaneEq(lane));
      if (!next) return null;
      const out = eq.setLaneEq(lane, next);
      onApplied?.(out);
      return out;
    },
    /** Flat: no bands, 0 dB out; the panel's own state is cleared too. */
    reset() {
      const cur = eq.getLaneEq(lane);
      return state.apply({ id: "eq", v: 1, bypass: cur.bypass, params: { out: 0, bands: [] } });
    },
    /** Which A/B slot is playing ("A" until the user switches). */
    abSlot() {
      return eq.getLaneEq(lane).params.ui?.ab?.cur === "B" ? "B" : "A";
    },
    /**
     * Swap to the other A/B slot. The first switch makes B a copy of A, so
     * the user edits B and compares it with A; each slot keeps its own bands,
     * output and panel controls, saved with the song.
     */
    toggleAb() {
      const cur = eq.getLaneEq(lane);
      const ui = cur.params.ui || {};
      const mine = { out: cur.params.out, bands: cur.params.bands, ui: withoutAb(ui) };
      const other = ui.ab?.other || mine;
      const slot = ui.ab?.cur === "B" ? "A" : "B";
      return state.apply({
        ...cur,
        params: {
          out: other.out,
          bands: other.bands,
          ui: { ...withoutAb(other.ui), ab: { cur: slot, other: mine } },
        },
      });
    },
  };
  return state;
}

/**
 * The panel's toolbar in its mixer-side row: undo, redo, A/B, reset, and a
 * preset menu. `presets`: [{key, apply()}] with key an i18n key.
 * Returns {paint()} to refresh the buttons after a change.
 */
export function buildToolbar(container, state, { presets = [], onChange } = {}) {
  const row = el("div", "eq-toolbar");
  const undo = button("eq-btn eq-undo", "↶");
  const redo = button("eq-btn eq-redo", "↷");
  const ab = button("eq-btn eq-ab", "A");
  const reset = button("eq-btn eq-reset", "");
  row.append(undo, redo, ab, reset);
  container.append(row);

  let select = null;
  if (presets.length) {
    select = el("select", "eq-preset");
    container.append(select);
    select.addEventListener("change", () => {
      const p = presets.find((x) => x.key === select.value);
      select.value = "";
      if (p) { p.apply(); onChange?.(); }
    });
  }

  undo.addEventListener("click", () => { state.undo(); onChange?.(); });
  redo.addEventListener("click", () => { state.redo(); onChange?.(); });
  ab.addEventListener("click", () => { state.toggleAb(); onChange?.(); });
  reset.addEventListener("click", () => { state.reset(); onChange?.(); });

  function relabel() {
    undo.title = t("eq.undo");
    undo.setAttribute("aria-label", t("eq.undo"));
    redo.title = t("eq.redo");
    redo.setAttribute("aria-label", t("eq.redo"));
    ab.title = t("eq.abTitle");
    reset.textContent = t("eq.reset");
    reset.title = t("eq.resetTitle");
    if (select) {
      select.replaceChildren();
      const head = el("option", null, t("eq.presets"));
      head.value = "";
      select.append(head);
      for (const p of presets) {
        const o = el("option", null, t(p.key));
        o.value = p.key;
        select.append(o);
      }
      select.value = "";
      select.setAttribute("aria-label", t("eq.presets"));
    }
  }

  function paint() {
    undo.disabled = !state.history.canUndo;
    redo.disabled = !state.history.canRedo;
    const slot = state.abSlot();
    ab.textContent = slot === "B" ? "B" : "A";
    ab.setAttribute("aria-pressed", String(slot === "B"));
    ab.setAttribute("aria-label", t(slot === "B" ? "eq.abOnB" : "eq.abOnA"));
  }

  relabel();
  paint();
  return { paint, relabel };
}

/** Ctrl/Cmd+Z, Ctrl/Cmd+Shift+Z and Ctrl/Cmd+Y inside a panel. */
export function bindUndoKeys(root, state, after) {
  root.addEventListener("keydown", (e) => {
    if (!(e.ctrlKey || e.metaKey)) return;
    if (e.target instanceof HTMLInputElement && e.target.type === "text") return;
    if (e.code === "KeyZ" || e.code === "KeyY") {
      e.preventDefault();
      e.stopPropagation();
      if (e.code === "KeyY" || e.shiftKey) state.redo();
      else state.undo();
      after?.();
    }
  });
}

/**
 * A small response curve (pedal and keys panels): the lane's EQ drawn from
 * the descriptor at the live rate, with a +-15 dB scale, over the lane's
 * live spectrum when one is given (createLiveSpectrum redraws it each frame).
 */
export function createMiniCurve(canvas, lane) {
  const freqs = new Float64Array(160);
  for (let i = 0; i < freqs.length; i++) freqs[i] = 20 * 1000 ** (i / (freqs.length - 1));
  let colors = null;
  let ghost = null;
  let spectrum = null;
  return {
    /** A descriptor to draw dashed over the lane's curve (a suggestion), or null. */
    setGhost(d) { ghost = d; },
    /** dB per pixel column across the width (createLiveSpectrum), or null for none. */
    setSpectrum(vals) { spectrum = vals; },
    draw() {
      const w = canvas.clientWidth;
      const h = canvas.clientHeight;
      if (!w || !h) return;
      const dpr = window.devicePixelRatio || 1;
      if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
        canvas.width = Math.round(w * dpr);
        canvas.height = Math.round(h * dpr);
      }
      colors ||= readColors(canvas);
      const ctx = canvas.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = colors.bg;
      ctx.fillRect(0, 0, w, h);
      const range = 15;
      const y = (db) => h / 2 - (Math.max(-range, Math.min(range, db)) / range) * (h / 2 - 4);
      ctx.strokeStyle = colors.grid;
      ctx.lineWidth = 1;
      for (const f of [100, 1000, 10000]) {
        const x = Math.round((Math.log(f / 20) / Math.log(1000)) * w) + 0.5;
        ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
      }
      if (spectrum?.length > 1) {
        const span = SPEC_MAX_DB - SPEC_MIN_DB;
        const step = w / (spectrum.length - 1);
        ctx.beginPath();
        ctx.moveTo(0, h);
        for (let c = 0; c < spectrum.length; c++) {
          const v = (spectrum[c] - SPEC_MIN_DB) / span;
          ctx.lineTo(c * step, h - Math.max(0, Math.min(1, v)) * h);
        }
        ctx.lineTo(w, h);
        ctx.closePath();
        ctx.fillStyle = colors.spectrum;
        ctx.fill();
        ctx.strokeStyle = colors.spectrumLine;
        ctx.stroke();
      }
      ctx.strokeStyle = colors.zero;
      ctx.beginPath(); ctx.moveTo(0, Math.round(h / 2) + 0.5); ctx.lineTo(w, Math.round(h / 2) + 0.5); ctx.stroke();
      ctx.fillStyle = colors.label;
      ctx.font = `9px ${colors.font}`;
      ctx.textAlign = "center";
      for (const [f, label] of [[100, "100"], [1000, "1k"], [10000, "10k"]]) {
        ctx.fillText(label, (Math.log(f / 20) / Math.log(1000)) * w, h - 4);
      }
      ctx.textAlign = "left";
      ctx.fillText(`+${range}`, 4, 11);
      ctx.fillText(`-${range}`, 4, h - 4);
      const cur = eq.getLaneEq(lane);
      const shown = { ...cur, bypass: false };
      const db = eqResponseDb(shown, freqs, eq.liveSampleRate());
      ctx.globalAlpha = cur.bypass ? 0.35 : 1;
      ctx.beginPath();
      for (let i = 0; i < freqs.length; i++) {
        const x = (i / (freqs.length - 1)) * w;
        if (i === 0) ctx.moveTo(x, y(db[i])); else ctx.lineTo(x, y(db[i]));
      }
      ctx.strokeStyle = colors.curve;
      ctx.lineWidth = 1.6;
      ctx.stroke();
      ctx.globalAlpha = 1;
      if (ghost) {
        const gdb = eqResponseDb(ghost, freqs, eq.liveSampleRate());
        ctx.beginPath();
        for (let i = 0; i < freqs.length; i++) {
          const x = (i / (freqs.length - 1)) * w;
          if (i === 0) ctx.moveTo(x, y(gdb[i])); else ctx.lineTo(x, y(gdb[i]));
        }
        ctx.setLineDash([4, 3]);
        ctx.strokeStyle = colors.ghost;
        ctx.lineWidth = 1.6;
        ctx.stroke();
        ctx.setLineDash([]);
      }
      canvas.setAttribute("aria-label", t("eq.curveAria", { out: fmtDb(cur.params.out) }));
    },
  };
}

/**
 * The lane's live spectrum for a mini curve: one dB value per pixel column
 * from 20 Hz to 20 kHz, handed to `onFrame` every animation frame while the
 * page is visible. It follows the analyser choice of the graph panels (Off,
 * Pre, Post) and draws nothing when that is Off. `columns()` is the curve's
 * width in px. The analyser tap is taken lazily, retried once a second until
 * the audio engine is up, and given back on destroy.
 */
export function createLiveSpectrum(lane, { columns, onFrame }) {
  const mode = readAnalyzerMode();
  let node = null;
  let data = null;
  let smooth = null;
  let bins = null;
  let rate = 0;
  let raf = 0;
  let lastTry = -1e9;
  let destroyed = false;

  function frame(ts) {
    raf = 0;
    if (destroyed || document.hidden) return;
    if (!node && ts - lastTry > 1000) {
      lastTry = ts;
      node = eq.getAnalyser(lane, mode, { fftSize: 8192, smoothing: 0.8 });
      if (node) { data = new Float32Array(node.frequencyBinCount); bins = null; }
    }
    const cols = Math.round(columns());
    if (node && cols > 1) {
      if (!bins || bins.lo.length !== cols || rate !== node.context.sampleRate) {
        rate = node.context.sampleRate;
        bins = columnBins(cols, node.frequencyBinCount, rate, { x: 0, y: 0, w: cols, h: 1 }, SPEC_TILT_DB_OCT);
        smooth = new Float32Array(cols).fill(SPEC_MIN_DB);
      }
      node.getFloatFrequencyData(data);
      onFrame(spectrumFrame(bins, data, smooth, SPEC_FALL_DB));
    }
    raf = requestAnimationFrame(frame);
  }

  function start() {
    if (raf || destroyed || mode === "off" || document.hidden) return;
    raf = requestAnimationFrame(frame);
  }

  const onVisibility = () => {
    if (document.hidden) { cancelAnimationFrame(raf); raf = 0; } else start();
  };
  document.addEventListener("visibilitychange", onVisibility);
  // A song (re)load rebuilt the engine: its analyser taps are new.
  const offChange = eq.onChange(({ laneId }) => { if (laneId === null) node = null; });
  start();

  return {
    destroy() {
      destroyed = true;
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
      document.removeEventListener("visibilitychange", onVisibility);
      offChange();
      if (node) eq.releaseAnalyser(lane, mode);
      node = null;
    },
  };
}

/** Whether exports can differ from what plays (the live rate is not 44.1 kHz). */
export function exportRateDiffers() {
  const rate = eq.liveSampleRate();
  return Math.abs(rate - 44100) > 1;
}
