// The graphical parametric EQ panel: the Drum EQ, and the general EQ of the
// original, "other" and recording tracks. An original design in the class of
// the big graphical plug-in EQs (FabFilter Pro-Q 3 was the reference): a log
// frequency axis, band handles to drag, a live spectrum behind the curve, and
// an editable band list beside it.
//
// Waveform side: the graph and the band list. Mixer side: undo, redo, A/B,
// reset, presets, output gain, analyser source, and the export-rate note.
//
// Drawing: two canvases. The spectrum canvas is redrawn every frame while
// the analyser is on and the page is visible (requestAnimationFrame, no
// allocations per frame); the curve canvas only when the EQ changes.

import * as eq from "../eqEngine.js";
import { EQ_LIMITS, bandResponseDb, eqResponseDb, normalizeBand } from "../eqMath.js";
import { t } from "../i18n.js";
import {
  DB_RANGE, SPEC_MIN_DB, SPEC_MAX_DB, SPEC_TILT_DB_OCT, GRID_FREQS, gridDbs,
  plotRect, freqToX, xToFreq, dbToY, yToDb, nodeXY, hitTest, dragBand, wheelQ,
  defaultBandAt, resetBand, retypeBand, soloEq, keyStep, hasGain, hasQ, hasSlope,
  fmtFreq, fmtDb, fmtQ, parseFreq, parseNumber, columnBins, spectrumFrame, curveFreqs,
} from "./eqGraphMath.js";
import {
  el, button, readColors, containKeys, createEditState, buildToolbar, bindUndoKeys, exportRateDiffers,
  ANALYZER_MODES, SPEC_FALL_DB, readAnalyzerMode, saveAnalyzerMode,
} from "./eqPanelCommon.js";
import { graphPresets } from "./eqPresets.js";
import { createSuggest } from "./eqSuggest.js";

// One option per type, cuts once per slope: the list's single type menu.
const TYPE_OPTIONS = [
  ["bell", 12], ["lowshelf", 12], ["highshelf", 12],
  ["lowcut", 12], ["lowcut", 24], ["lowcut", 36], ["lowcut", 48],
  ["highcut", 12], ["highcut", 24], ["highcut", 36], ["highcut", 48],
  ["notch", 12], ["bandpass", 12], ["tilt", 12],
];

function typeLabel(band) {
  const name = t(`eq.type.${band.type}`);
  return hasSlope(band.type) ? `${name} ${t("eq.slope", { n: band.slope })}` : name;
}

export function mountPanel({ lane, kind, body, controls }) {
  const state = createEditState(lane);
  const panelTitle = t(kind === "drum" ? "eq.panel.drum" : "eq.panel.general");

  // ─── DOM ───
  const root = el("div", "eq-graph-panel");
  const wrap = el("div", "eq-graph-wrap");
  const specCv = el("canvas", "eq-spectrum-canvas");
  const curveCv = el("canvas", "eq-curve-canvas");
  specCv.setAttribute("aria-hidden", "true");
  curveCv.setAttribute("aria-hidden", "true");
  const handles = el("div", "eq-handles");
  const readout = el("div", "eq-readout");
  readout.hidden = true;
  const hint = el("div", "eq-graph-hint");
  wrap.append(specCv, curveCv, handles, readout, hint);
  wrap.setAttribute("role", "group");

  const listBox = el("div", "eq-band-list");
  const listHead = el("div", "eq-band-head");
  const listRows = el("div", "eq-band-rows");
  const addBtn = button("eq-btn eq-band-add", "+");
  listBox.append(listHead, listRows);
  root.append(wrap, listBox);
  body.append(root);
  containKeys(body);

  // Mixer side.
  const toolbar = buildToolbar(controls, state, {
    presets: graphPresets(kind).map((p) => ({ key: p.key, apply: () => applyPreset(p) })),
    onChange: () => paintSide(),
  });
  // Suggest: a card in place of the band list, the proposal as a ghost curve.
  let ghost = null;
  const suggest = createSuggest({
    lane, kind, state,
    toolbar: controls.querySelector(".eq-toolbar"),
    cardHost: root,
    onShow: (on) => { root.classList.toggle("is-suggesting", on); },
    onGhost: (d) => { ghost = d; scheduleRedraw(); },
    onApplied: () => { selected = -1; paintSide(); },
  });
  const outRow = el("label", "eq-out");
  const outName = el("span", "eq-out-name");
  const outInput = el("input", "eq-out-range");
  outInput.type = "range";
  outInput.min = String(EQ_LIMITS.outMin);
  outInput.max = String(EQ_LIMITS.outMax);
  outInput.step = "0.1";
  const outVal = el("span", "eq-out-val");
  outRow.append(outName, outInput, outVal);
  const anaRow = el("div", "eq-analyzer");
  const anaName = el("span", "eq-analyzer-name");
  const anaGroup = el("div", "eq-seg");
  anaGroup.setAttribute("role", "group");
  const anaBtns = ANALYZER_MODES.map((m) => {
    const b = button("eq-seg-btn", "");
    b.dataset.mode = m;
    b.addEventListener("click", () => setAnalyzerMode(m));
    anaGroup.append(b);
    return b;
  });
  anaRow.append(anaName, anaGroup);
  const note = el("p", "eq-note");
  note.hidden = true;
  controls.append(outRow, anaRow, note);
  containKeys(controls);
  bindUndoKeys(body, state, () => paintSide());
  bindUndoKeys(controls, state, () => paintSide());

  // ─── State ───
  let destroyed = false;
  let selected = -1;
  let hover = -1;
  let drag = null;
  let soloIndex = -1;
  let wheelTimer = 0;
  let wheelRecorded = false;
  let outRecorded = false;
  let colors = null;
  let w = 0;
  let h = 0;
  let r = plotRect(1, 1);
  let freqs = new Float64Array(2);
  let redrawQueued = 0;
  let listSig = "";
  let popover = null;

  const bands = () => state.current().params.bands;

  // ─── Applying changes ───

  function setBands(next, opts) {
    return state.applyParams({ bands: next }, opts);
  }

  function updateBand(i, band, opts) {
    const next = bands().slice();
    next[i] = normalizeBand(band);
    setBands(next, opts);
    if (soloIndex === i) eq.previewLaneEq(lane, soloEq(next[i]));
  }

  function removeBand(i) {
    const next = bands().slice();
    next.splice(i, 1);
    if (soloIndex === i) stopSolo();
    else if (soloIndex > i) soloIndex -= 1;
    selected = Math.min(selected, next.length - 1);
    setBands(next);
    paintSide();
  }

  function addBand(band) {
    const cur = bands();
    if (cur.length >= EQ_LIMITS.maxBands) {
      flashHint(t("eq.band.full"));
      return -1;
    }
    setBands([...cur, band]);
    selected = cur.length;
    paintSide();
    return selected;
  }

  function applyPreset(p) {
    stopSolo();
    const cur = state.current();
    state.apply({ ...cur, params: { out: p.out || 0, bands: p.bands.map(normalizeBand), ui: cur.params.ui?.ab ? { ab: cur.params.ui.ab } : undefined } });
    selected = -1;
  }

  function startSolo(i) {
    soloIndex = i;
    eq.previewLaneEq(lane, soloEq(bands()[i]));
    scheduleRedraw();
  }

  function stopSolo() {
    if (soloIndex < 0) return;
    soloIndex = -1;
    eq.previewLaneEq(lane, null);
    scheduleRedraw();
  }

  // ─── Geometry ───

  function resize() {
    const box = wrap.getBoundingClientRect();
    const nw = Math.max(1, Math.round(box.width));
    const nh = Math.max(1, Math.round(box.height));
    if (nw === w && nh === h) return;
    w = nw;
    h = nh;
    const dpr = window.devicePixelRatio || 1;
    for (const cv of [specCv, curveCv]) {
      cv.width = Math.round(w * dpr);
      cv.height = Math.round(h * dpr);
      cv.style.width = `${w}px`;
      cv.style.height = `${h}px`;
    }
    r = plotRect(w, h);
    freqs = curveFreqs(r, 2);
    spec.bins = null;
    redraw();
  }

  const localXY = (e) => {
    const box = curveCv.getBoundingClientRect();
    return { x: e.clientX - box.left, y: e.clientY - box.top };
  };

  // ─── Drawing: curve, grid, handles ───

  function scheduleRedraw() {
    if (redrawQueued || destroyed) return;
    redrawQueued = requestAnimationFrame(() => {
      redrawQueued = 0;
      redraw();
    });
  }

  function redraw() {
    if (destroyed || !w || !h) return;
    colors ||= readColors(wrap);
    const dpr = window.devicePixelRatio || 1;
    const ctx = curveCv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const cur = state.current();
    const list = cur.params.bands;
    const rate = eq.liveSampleRate();

    // Grid.
    ctx.lineWidth = 1;
    ctx.font = `9px ${colors.font}`;
    ctx.textBaseline = "alphabetic";
    for (const [f, label] of GRID_FREQS) {
      const x = Math.round(freqToX(f, r)) + 0.5;
      ctx.strokeStyle = colors.grid;
      ctx.globalAlpha = label ? 1 : 0.55;
      ctx.beginPath(); ctx.moveTo(x, r.y); ctx.lineTo(x, r.y + r.h); ctx.stroke();
      ctx.globalAlpha = 1;
      if (label) {
        ctx.fillStyle = colors.label;
        ctx.textAlign = f === 20 ? "left" : f === 20000 ? "right" : "center";
        ctx.fillText(label, f === 20 ? x + 2 : x, h - 5);
      }
    }
    ctx.textAlign = "left";
    for (const d of gridDbs(DB_RANGE)) {
      const y = Math.round(dbToY(d, r)) + 0.5;
      ctx.strokeStyle = d === 0 ? colors.zero : colors.grid;
      ctx.beginPath(); ctx.moveTo(r.x, y); ctx.lineTo(r.x + r.w, y); ctx.stroke();
      ctx.fillStyle = colors.label;
      ctx.fillText(d > 0 ? `+${d}` : String(d), r.x + r.w + 4, y + 3);
    }

    // The selected band's own shape, shaded.
    if (selected >= 0 && list[selected]) {
      const b = list[selected];
      const own = bandResponseDb({ ...b, on: true }, freqs, rate);
      ctx.beginPath();
      const y0 = dbToY(0, r);
      ctx.moveTo(freqToX(freqs[0], r), y0);
      for (let i = 0; i < freqs.length; i++) ctx.lineTo(freqToX(freqs[i], r), clampY(dbToY(own[i], r)));
      ctx.lineTo(freqToX(freqs[freqs.length - 1], r), y0);
      ctx.closePath();
      ctx.fillStyle = colors.bands[selected % colors.bands.length];
      ctx.globalAlpha = 0.16;
      ctx.fill();
      ctx.globalAlpha = 1;
    }

    // The whole EQ. Bypassed: drawn faint, so the setting is still visible.
    const db = eqResponseDb({ ...cur, bypass: false }, freqs, rate);
    const y0 = dbToY(0, r);
    ctx.beginPath();
    for (let i = 0; i < freqs.length; i++) {
      const x = freqToX(freqs[i], r);
      const y = clampY(dbToY(db[i], r));
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.globalAlpha = cur.bypass ? 0.3 : 1;
    ctx.strokeStyle = colors.curve;
    ctx.lineWidth = 2;
    ctx.stroke();
    ctx.lineTo(freqToX(freqs[freqs.length - 1], r), y0);
    ctx.lineTo(freqToX(freqs[0], r), y0);
    ctx.closePath();
    ctx.fillStyle = colors.curveFill;
    ctx.fill();
    ctx.globalAlpha = 1;

    // A suggestion under review: its whole curve, dashed, over the current one.
    if (ghost) {
      const gdb = eqResponseDb(ghost, freqs, rate);
      ctx.beginPath();
      for (let i = 0; i < freqs.length; i++) {
        const x = freqToX(freqs[i], r);
        const y = clampY(dbToY(gdb[i], r));
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.setLineDash([5, 4]);
      ctx.strokeStyle = colors.ghost;
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // Handles.
    ctx.font = `bold 8px ${colors.font}`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    for (let i = 0; i < list.length; i++) {
      const b = list[i];
      const { x, y } = nodeXY(b, r);
      const col = colors.bands[i % colors.bands.length];
      const big = i === selected || i === hover;
      ctx.beginPath();
      ctx.arc(x, y, big ? 7.5 : 6.5, 0, Math.PI * 2);
      ctx.globalAlpha = b.on ? 1 : 0.45;
      ctx.fillStyle = b.on ? col : colors.bg;
      ctx.fill();
      ctx.lineWidth = i === selected ? 2 : 1.25;
      ctx.strokeStyle = i === selected ? colors.fg : col;
      ctx.stroke();
      ctx.fillStyle = b.on ? colors.bg : col;
      ctx.fillText(String(i + 1), x, y + 0.5);
      if (i === soloIndex) {
        ctx.beginPath();
        ctx.arc(x, y, 11, 0, Math.PI * 2);
        ctx.strokeStyle = col;
        ctx.lineWidth = 1;
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }
    placeHandles(list);
    paintList(list);
    paintSideValues(cur);
  }

  function clampY(y) {
    return Math.max(r.y - 4, Math.min(r.y + r.h + 4, y));
  }

  // ─── Keyboard handles: one focusable element per band, over its node ───

  function placeHandles(list) {
    while (handles.children.length > list.length) handles.lastElementChild.remove();
    while (handles.children.length < list.length) {
      const hEl = el("div", "eq-handle");
      hEl.tabIndex = 0;
      hEl.setAttribute("role", "slider");
      hEl.addEventListener("focus", () => {
        const i = Number(hEl.dataset.i);
        if (selected !== i) { selected = i; scheduleRedraw(); }
      });
      hEl.addEventListener("keydown", onHandleKey);
      handles.append(hEl);
    }
    list.forEach((b, i) => {
      const hEl = handles.children[i];
      const { x, y } = nodeXY(b, r);
      hEl.dataset.i = String(i);
      hEl.style.transform = `translate(${Math.round(x - 9)}px, ${Math.round(y - 9)}px)`;
      hEl.setAttribute("aria-label", t("eq.band.n", { n: i + 1 }));
      hEl.setAttribute("aria-valuemin", String(EQ_LIMITS.freqMin));
      hEl.setAttribute("aria-valuemax", String(EQ_LIMITS.freqMax));
      hEl.setAttribute("aria-valuenow", String(b.f));
      const parts = [typeLabel(b), fmtFreq(b.f)];
      if (hasGain(b.type)) parts.push(fmtDb(b.g));
      if (hasQ(b.type)) parts.push(`Q ${fmtQ(b.q)}`);
      if (!b.on) parts.push(t("eq.band.offWord"));
      hEl.setAttribute("aria-valuetext", parts.join(", "));
    });
  }

  function onHandleKey(e) {
    const i = Number(e.currentTarget.dataset.i);
    const b = bands()[i];
    if (!b) return;
    if (e.key === "Delete" || e.key === "Backspace") {
      e.preventDefault();
      removeBand(i);
      handles.children[Math.min(i, handles.children.length - 1)]?.focus();
      return;
    }
    const next = keyStep(b, e.key, e.shiftKey);
    if (next === b) return;
    e.preventDefault();
    // One undo step per run of key presses on the same band.
    updateBand(i, next, { record: !e.repeat });
  }

  // ─── Band list ───

  function listSignature(list) {
    return `${list.length}|${list.map((b) => `${b.type}${b.slope}${b.on ? 1 : 0}`).join(",")}|${selected}|${soloIndex}`;
  }

  function buildListHead() {
    listHead.replaceChildren(
      el("span", "eq-col-num", "#"),
      el("span", "eq-col-on"),
      el("span", "eq-col-type", t("eq.col.type")),
      el("span", "eq-col-f", t("eq.col.freq")),
      el("span", "eq-col-g", t("eq.col.gain")),
      el("span", "eq-col-q", t("eq.col.q")),
      addBtn,
    );
    addBtn.title = t("eq.band.addTitle");
    addBtn.setAttribute("aria-label", t("eq.band.addTitle"));
  }

  function numInput(cls, aria, onCommit) {
    const inp = el("input", `eq-num ${cls}`);
    inp.type = "text";
    inp.inputMode = "decimal";
    inp.autocomplete = "off";
    inp.spellcheck = false;
    inp.setAttribute("aria-label", aria);
    const commit = () => onCommit(inp.value, inp);
    inp.addEventListener("change", commit);
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); commit(); inp.select(); }
      if (e.key === "Escape") { inp.blur(); scheduleRedraw(); }
    });
    inp.addEventListener("focus", () => inp.select());
    return inp;
  }

  function buildList(list) {
    listRows.replaceChildren();
    if (!list.length) {
      listRows.append(el("p", "eq-band-empty", t("eq.band.empty")));
      return;
    }
    list.forEach((b, i) => {
      const row = el("div", "eq-band-row");
      row.dataset.i = String(i);
      row.classList.toggle("selected", i === selected);
      row.classList.toggle("off", !b.on);
      row.style.setProperty("--band-color", (colors || readColors(wrap)).bands[i % 6]);
      row.addEventListener("pointerdown", () => { if (selected !== i) { selected = i; scheduleRedraw(); } });

      const num = el("span", "eq-band-num", String(i + 1));
      const on = el("input", "eq-band-on");
      on.type = "checkbox";
      on.checked = b.on;
      on.setAttribute("aria-label", t("eq.band.onAria", { n: i + 1 }));
      on.addEventListener("change", () => updateBand(i, { ...bands()[i], on: on.checked }));

      const type = el("select", "eq-band-type");
      type.setAttribute("aria-label", t("eq.band.typeAria", { n: i + 1 }));
      for (const [tp, slope] of TYPE_OPTIONS) {
        const o = el("option", null, typeLabel({ type: tp, slope }));
        o.value = `${tp}:${slope}`;
        type.append(o);
      }
      type.value = `${b.type}:${hasSlope(b.type) ? b.slope : 12}`;
      type.addEventListener("change", () => {
        const [tp, slope] = type.value.split(":");
        const next = retypeBand(bands()[i], tp);
        if (hasSlope(tp)) next.slope = Number(slope);
        updateBand(i, next);
      });

      const f = numInput("eq-band-f", t("eq.band.freqAria", { n: i + 1 }), (v, inp) => {
        const hz = parseFreq(v);
        if (!Number.isFinite(hz)) { inp.value = fmtFreq(bands()[i].f); return; }
        updateBand(i, { ...bands()[i], f: hz });
      });
      const g = numInput("eq-band-g", t("eq.band.gainAria", { n: i + 1 }), (v, inp) => {
        const db = parseNumber(v.replace(/db/i, ""));
        if (!Number.isFinite(db)) { inp.value = fmtDb(bands()[i].g); return; }
        updateBand(i, { ...bands()[i], g: db });
      });
      g.disabled = !hasGain(b.type);
      const q = numInput("eq-band-q", t("eq.band.qAria", { n: i + 1 }), (v, inp) => {
        const n = parseNumber(v);
        if (!Number.isFinite(n) || n <= 0) { inp.value = fmtQ(bands()[i].q); return; }
        updateBand(i, { ...bands()[i], q: n });
      });
      q.disabled = !hasQ(b.type);

      const solo = button("eq-band-solo", "S");
      solo.setAttribute("aria-pressed", String(i === soloIndex));
      solo.title = t("eq.band.soloTitle");
      solo.setAttribute("aria-label", t("eq.band.soloAria", { n: i + 1 }));
      solo.addEventListener("click", () => {
        if (soloIndex === i) stopSolo(); else startSolo(i);
        listSig = "";
        scheduleRedraw();
      });
      const del = button("eq-band-del", "×");
      del.title = t("eq.band.remove", { n: i + 1 });
      del.setAttribute("aria-label", t("eq.band.remove", { n: i + 1 }));
      del.addEventListener("click", () => removeBand(i));

      row.append(num, on, type, f, g, q, solo, del);
      listRows.append(row);
    });
  }

  function paintList(list) {
    const sig = listSignature(list);
    if (sig !== listSig) {
      listSig = sig;
      buildList(list);
    }
    // Values in place, except in the field being typed into.
    const active = document.activeElement;
    list.forEach((b, i) => {
      const row = listRows.children[i];
      if (!row) return;
      const f = row.querySelector(".eq-band-f");
      const g = row.querySelector(".eq-band-g");
      const q = row.querySelector(".eq-band-q");
      if (f && f !== active) f.value = fmtFreq(b.f);
      if (g && g !== active) g.value = hasGain(b.type) ? fmtDb(b.g) : "";
      if (q && q !== active) q.value = hasQ(b.type) ? fmtQ(b.q) : "";
    });
    addBtn.disabled = list.length >= EQ_LIMITS.maxBands;
  }

  // ─── Mixer side ───

  function paintSideValues(cur) {
    if (document.activeElement !== outInput) outInput.value = String(cur.params.out);
    outVal.textContent = fmtDb(cur.params.out);
    outInput.setAttribute("aria-valuetext", fmtDb(cur.params.out));
    toolbar.paint();
  }

  function paintSide() {
    toolbar.paint();
    for (const b of anaBtns) b.setAttribute("aria-pressed", String(b.dataset.mode === analyzer.mode));
    const differs = exportRateDiffers();
    note.hidden = !differs;
    if (differs) note.textContent = t("eq.rateNote", { rate: (eq.liveSampleRate() / 1000).toFixed(1) });
    scheduleRedraw();
  }

  function relabel() {
    wrap.setAttribute("aria-label", t("eq.graphAria", { panel: panelTitle }));
    hint.textContent = t("eq.hint");
    outName.textContent = t("eq.out");
    outInput.setAttribute("aria-label", t("eq.outAria"));
    anaName.textContent = t("eq.analyzer");
    anaGroup.setAttribute("aria-label", t("eq.analyzer"));
    for (const b of anaBtns) {
      b.textContent = t(`eq.analyzer.${b.dataset.mode}`);
      b.title = t("eq.analyzer.title");
    }
    toolbar.relabel();
    suggest.relabel();
    buildListHead();
    listSig = "";
    paintSide();
  }

  outInput.addEventListener("input", () => {
    state.applyParams({ out: Number(outInput.value) }, { record: !outRecorded });
    outRecorded = true;
  });
  outInput.addEventListener("change", () => { outRecorded = false; });
  outInput.addEventListener("dblclick", () => { state.applyParams({ out: 0 }); });

  addBtn.addEventListener("click", () => {
    // A bell where the graph has the most room: 1 kHz, or the widest gap.
    const fs = bands().map((b) => b.f).sort((a, b) => a - b);
    let f = 1000;
    if (fs.length) {
      let best = 0;
      const pts = [40, ...fs, 16000];
      for (let k = 0; k < pts.length - 1; k++) {
        const gap = Math.log(pts[k + 1] / pts[k]);
        if (gap > best) { best = gap; f = Math.sqrt(pts[k] * pts[k + 1]); }
      }
    }
    const i = addBand(normalizeBand({ type: "bell", f, g: 0, q: 1 }));
    if (i >= 0) requestAnimationFrame(() => handles.children[i]?.focus());
  });

  // ─── Pointer ───

  function showReadout(i) {
    const b = bands()[i];
    if (!b) { readout.hidden = true; return; }
    const parts = [fmtFreq(b.f)];
    if (hasGain(b.type)) parts.push(fmtDb(b.g));
    if (hasQ(b.type)) parts.push(`Q ${fmtQ(b.q)}`);
    readout.textContent = parts.join("  ");
    const { x, y } = nodeXY(b, r);
    readout.hidden = false;
    const rw = readout.offsetWidth;
    readout.style.transform = `translate(${Math.round(Math.max(2, Math.min(w - rw - 30, x - rw / 2)))}px, ${Math.round(y < 40 ? y + 14 : y - 30)}px)`;
  }

  curveCv.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    closePopover();
    const { x, y } = localXY(e);
    let i = hitTest(bands(), x, y, r);
    let recorded = false;
    if (i >= 0 && e.altKey) { removeBand(i); return; }
    if (i < 0) {
      if (y > r.y + r.h || x > r.x + r.w) return;
      i = addBand(defaultBandAt(xToFreq(x, r), yToDb(y, r)));
      if (i < 0) return;
      recorded = true;
    }
    selected = i;
    drag = { i, start: { ...bands()[i] }, x0: x, y0: y, recorded, id: e.pointerId };
    curveCv.setPointerCapture(e.pointerId);
    handles.children[i]?.focus({ preventScroll: true });
    showReadout(i);
    scheduleRedraw();
  });

  curveCv.addEventListener("pointermove", (e) => {
    const { x, y } = localXY(e);
    if (drag && e.pointerId === drag.id) {
      const dx = x - drag.x0;
      const dy = y - drag.y0;
      if (!drag.recorded && Math.abs(dx) + Math.abs(dy) < 2) return;
      const next = dragBand(drag.start, dx, dy, r, { fine: e.shiftKey });
      updateBand(drag.i, next, { record: !drag.recorded });
      drag.recorded = true;
      showReadout(drag.i);
      return;
    }
    const i = hitTest(bands(), x, y, r);
    curveCv.style.cursor = i >= 0 ? "grab" : (x <= r.x + r.w && y <= r.y + r.h ? "crosshair" : "default");
    if (i !== hover) {
      hover = i;
      if (i >= 0) showReadout(i); else readout.hidden = true;
      scheduleRedraw();
    }
  });

  const endDrag = (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    drag = null;
    if (hover < 0) readout.hidden = true;
  };
  curveCv.addEventListener("pointerup", endDrag);
  curveCv.addEventListener("pointercancel", endDrag);
  curveCv.addEventListener("pointerleave", () => {
    if (drag) return;
    hover = -1;
    readout.hidden = true;
    scheduleRedraw();
  });

  curveCv.addEventListener("dblclick", (e) => {
    const { x, y } = localXY(e);
    const i = hitTest(bands(), x, y, r);
    if (i < 0) return;
    updateBand(i, resetBand(bands()[i]));
    showReadout(i);
  });

  curveCv.addEventListener("wheel", (e) => {
    const { x, y } = localXY(e);
    const i = hitTest(bands(), x, y, r, DB_RANGE, 14);
    if (i < 0 || !hasQ(bands()[i].type)) return;
    e.preventDefault();
    clearTimeout(wheelTimer);
    wheelTimer = setTimeout(() => { wheelRecorded = false; }, 500);
    const b = bands()[i];
    updateBand(i, { ...b, q: wheelQ(b.q, e.deltaY, e.shiftKey) }, { record: !wheelRecorded });
    wheelRecorded = true;
    selected = i;
    showReadout(i);
  }, { passive: false });

  curveCv.addEventListener("contextmenu", (e) => {
    const { x, y } = localXY(e);
    const i = hitTest(bands(), x, y, r);
    e.preventDefault();
    if (i < 0) return;
    selected = i;
    openPopover(i);
    scheduleRedraw();
  });

  // ─── Type popover (right-click on a handle) ───

  function openPopover(i) {
    closePopover();
    const b = bands()[i];
    const pop = el("div", "eq-popover");
    pop.setAttribute("role", "menu");
    pop.setAttribute("aria-label", t("eq.band.n", { n: i + 1 }));
    for (const tp of ["bell", "lowshelf", "highshelf", "lowcut", "highcut", "notch", "bandpass", "tilt"]) {
      const item = button("eq-pop-item", t(`eq.type.${tp}`));
      item.setAttribute("role", "menuitemradio");
      item.setAttribute("aria-checked", String(b.type === tp));
      item.addEventListener("click", () => { updateBand(i, retypeBand(bands()[i], tp)); openPopover(i); });
      pop.append(item);
    }
    if (hasSlope(b.type)) {
      const slopes = el("div", "eq-pop-slopes");
      for (const s of [12, 24, 36, 48]) {
        const item = button("eq-pop-slope", String(s));
        item.setAttribute("role", "menuitemradio");
        item.setAttribute("aria-checked", String(b.slope === s));
        item.title = t("eq.slope", { n: s });
        item.addEventListener("click", () => { updateBand(i, { ...bands()[i], slope: s }); openPopover(i); });
        slopes.append(item);
      }
      pop.append(slopes);
    }
    const onOff = button("eq-pop-item eq-pop-on", t(b.on ? "eq.menu.disable" : "eq.menu.enable"));
    onOff.setAttribute("role", "menuitem");
    onOff.addEventListener("click", () => { updateBand(i, { ...bands()[i], on: !bands()[i].on }); closePopover(); });
    const del = button("eq-pop-item eq-pop-del", t("eq.menu.delete"));
    del.setAttribute("role", "menuitem");
    del.addEventListener("click", () => { closePopover(); removeBand(i); });
    pop.append(onOff, del);
    wrap.append(pop);
    const { x, y } = nodeXY(b, r);
    const pw = pop.offsetWidth;
    const ph = pop.offsetHeight;
    pop.style.left = `${Math.round(Math.max(4, Math.min(w - pw - 4, x + 12)))}px`;
    pop.style.top = `${Math.round(Math.max(4, Math.min(h - ph - 4, y - ph / 2)))}px`;
    popover = pop;
    pop.querySelector("[aria-checked='true']")?.focus();
    pop.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") { ev.stopPropagation(); closePopover(); handles.children[i]?.focus(); }
    });
  }

  function closePopover() {
    popover?.remove();
    popover = null;
  }

  const onDocDown = (e) => {
    if (popover && !popover.contains(e.target)) closePopover();
  };
  document.addEventListener("pointerdown", onDocDown, true);

  // ─── Spectrum ───

  const analyzer = { mode: readAnalyzerMode(), node: null, which: null, data: null, smooth: null, raf: 0, lastTry: -1e9, ms: 0, frames: 0 };
  const spec = { bins: null, rate: 0 };

  function acquire() {
    analyzer.node = eq.getAnalyser(lane, analyzer.mode, { fftSize: 8192, smoothing: 0.8 });
    analyzer.which = analyzer.node ? analyzer.mode : null;
    if (analyzer.node) {
      analyzer.data = new Float32Array(analyzer.node.frequencyBinCount);
      spec.bins = null;
    }
  }

  function release() {
    if (analyzer.node && analyzer.which) eq.releaseAnalyser(lane, analyzer.which);
    analyzer.node = null;
    analyzer.which = null;
  }

  function setAnalyzerMode(m) {
    if (m === analyzer.mode) return;
    release();
    analyzer.mode = m;
    saveAnalyzerMode(m);
    clearSpectrum();
    paintSide();
    startLoop();
  }

  function clearSpectrum() {
    const ctx = specCv.getContext("2d");
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, specCv.width, specCv.height);
  }

  function startLoop() {
    if (analyzer.raf || destroyed || analyzer.mode === "off" || document.hidden) return;
    analyzer.raf = requestAnimationFrame(frame);
  }

  function stopLoop() {
    if (analyzer.raf) cancelAnimationFrame(analyzer.raf);
    analyzer.raf = 0;
  }

  function frame(ts) {
    analyzer.raf = 0;
    if (destroyed || analyzer.mode === "off" || document.hidden) return;
    const t0 = performance.now();
    if (!analyzer.node && ts - analyzer.lastTry > 1000) {
      analyzer.lastTry = ts;
      acquire();
    }
    if (analyzer.node && w > 1) drawSpectrum();
    analyzer.ms += performance.now() - t0;
    analyzer.frames += 1;
    analyzer.raf = requestAnimationFrame(frame);
  }

  function drawSpectrum() {
    const node = analyzer.node;
    colors ||= readColors(wrap);
    const cols = Math.max(2, Math.round(r.w));
    if (!spec.bins || spec.bins.lo.length !== cols || spec.rate !== node.context.sampleRate) {
      spec.rate = node.context.sampleRate;
      spec.bins = columnBins(cols, node.frequencyBinCount, spec.rate, r, SPEC_TILT_DB_OCT);
      analyzer.smooth = new Float32Array(cols).fill(SPEC_MIN_DB);
    }
    node.getFloatFrequencyData(analyzer.data);
    const vals = spectrumFrame(spec.bins, analyzer.data, analyzer.smooth, SPEC_FALL_DB);
    const dpr = window.devicePixelRatio || 1;
    const ctx = specCv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const bottom = r.y + r.h;
    const span = SPEC_MAX_DB - SPEC_MIN_DB;
    const step = r.w / (cols - 1);
    ctx.beginPath();
    ctx.moveTo(r.x, bottom);
    for (let c = 0; c < cols; c++) {
      const v = (vals[c] - SPEC_MIN_DB) / span;
      ctx.lineTo(r.x + c * step, bottom - Math.max(0, Math.min(1, v)) * r.h);
    }
    ctx.lineTo(r.x + r.w, bottom);
    ctx.closePath();
    ctx.fillStyle = colors.spectrum;
    ctx.fill();
    ctx.strokeStyle = colors.spectrumLine;
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  const onVisibility = () => {
    if (document.hidden) stopLoop(); else startLoop();
  };
  document.addEventListener("visibilitychange", onVisibility);

  // ─── Lifecycle ───

  let hintTimer = 0;
  function flashHint(text) {
    hint.textContent = text;
    hint.classList.add("flash");
    clearTimeout(hintTimer);
    hintTimer = setTimeout(() => {
      hint.classList.remove("flash");
      hint.textContent = t("eq.hint");
    }, 2500);
  }

  const offChange = eq.onChange(({ laneId }) => {
    if (laneId === null) {
      // A song (re)load rebuilt the engine: its analyser taps are new.
      analyzer.node = null;
      analyzer.which = null;
      state.history.clear();
      selected = -1;
      listSig = "";
      paintSide();
      return;
    }
    if (laneId === lane) {
      toolbar.paint();
      scheduleRedraw();
    }
  });

  const ro = typeof ResizeObserver === "function" ? new ResizeObserver(() => resize()) : null;
  ro?.observe(wrap);
  relabel();
  resize();
  startLoop();

  return {
    refresh() { paintSide(); },
    relabel,
    suggest,
    destroy() {
      destroyed = true;
      suggest.destroy();
      stopLoop();
      stopSolo();
      release();
      closePopover();
      offChange();
      ro?.disconnect();
      clearTimeout(wheelTimer);
      clearTimeout(hintTimer);
      if (redrawQueued) cancelAnimationFrame(redrawQueued);
      document.removeEventListener("pointerdown", onDocDown, true);
      document.removeEventListener("visibilitychange", onVisibility);
    },
    // For measurements (tests): mean time per spectrum frame, in ms.
    stats() { return { frames: analyzer.frames, msPerFrame: analyzer.frames ? analyzer.ms / analyzer.frames : 0, peakDb: analyzer.smooth ? Math.max(...analyzer.smooth) : null }; },
  };
}
