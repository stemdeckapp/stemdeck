// The Bass EQ and Guitar EQ panels (and, with another layout, the Keys EQ):
// an original pedal-like faceplate of knobs with a small live response curve
// beside it, over the lane's live spectrum. The controls and their ranges follow the reference pedals'
// published manuals (eqPedalMath.js has the sources and the mapping); the
// artwork, names and layout are StemDeck's own.
//
// Every knob is a fixed band of the shared EQ engine, so the sound, the save
// with the song and the export are those of every other EQ panel. The knob
// positions themselves ride along in the descriptor's params.ui.

import * as eq from "../eqEngine.js";
import { t } from "../i18n.js";
import { createKnob } from "./eqKnob.js";
import { fmtDb, fmtFreq, fmtQ } from "./eqGraphMath.js";
import {
  PEDALS, PEDAL_GAIN, SHELF_GAIN, LEVEL_MIN, LEVEL_MAX, KEYS,
  logPos, logVal, linPos, linVal, qPos, qVal,
  controlsFromEq, panelEq, pedalDefaults, keysDefaults, normalizePedal, normalizeKeys,
} from "./eqPedalMath.js";
import { controlPresets } from "./eqPresets.js";
import { createSuggest } from "./eqSuggest.js";
import {
  el, containKeys, createEditState, buildToolbar, bindUndoKeys, createMiniCurve, createLiveSpectrum,
} from "./eqPanelCommon.js";

const round1 = (v) => Math.round(v * 10) / 10;
const round2 = (v) => Math.round(v * 100) / 100;

// Scales: how a knob's 0..1 position maps to its value.
const freqScale = (min, max) => ({ toPos: (v) => logPos(v, min, max), fromPos: (p) => round1(logVal(p, min, max)) });
const gainScale = (range) => ({ toPos: (v) => linPos(v, -range, range), fromPos: (p) => round2(Math.round(linVal(p, -range, range) * 10) / 10) });
const qScale = { toPos: qPos, fromPos: (p) => Math.round(qVal(p) * 100) / 100 };

/** The knobs of a panel, by section: [{section, knobs: [{key, name, size, scale, format, bipolar, offAt}]}]. */
function layoutFor(kind) {
  if (kind === "keys") {
    const k = KEYS;
    const db = (range) => ({ ...gainScale(range), format: fmtDb, bipolar: true, def: 0 });
    return [
      { section: "eq.keys.cut", knobs: [
        { key: "lowcut", name: "eq.keys.lowcut", small: true, ...freqScale(k.lowcut.min, k.lowcut.max), def: k.lowcut.min, offAt: 0,
          format: (v) => (v <= k.lowcut.min + 0.05 ? t("eq.pedal.off") : fmtFreq(v)) },
      ] },
      { section: "eq.keys.body", knobs: [{ key: "body", name: "eq.keys.body", ...db(k.body.range) }] },
      { section: "eq.keys.warmth", knobs: [{ key: "warmth", name: "eq.keys.warmth", ...db(k.warmth.range) }] },
      { section: "eq.keys.presence", knobs: [
        { key: "presence", name: "eq.keys.presence", ...db(k.presence.range) },
        { key: "presF", name: "eq.keys.focus", small: true, ...freqScale(k.presence.min, k.presence.max), def: 3000, format: fmtFreq },
      ] },
      { section: "eq.keys.air", knobs: [{ key: "air", name: "eq.keys.air", ...db(k.air.range) }] },
      { section: "eq.keys.tone", knobs: [{ key: "tone", name: "eq.keys.tone", ...db(k.tone.range) }] },
      { section: "eq.pedal.level", knobs: [{ key: "lvl", name: "eq.pedal.level", ...gainScale(k.level.max), def: 0, format: fmtDb, bipolar: true, hideName: true }] },
    ];
  }
  const p = PEDALS[kind];
  const d = pedalDefaults(kind);
  const band = (sec, f, g, q, range) => ({
    section: sec,
    knobs: [
      // Its value reads "Q 2.5": the name would say Q twice.
      { key: q, name: "eq.pedal.q", small: true, ...qScale, def: d[q], format: (v) => `Q ${fmtQ(v)}`, hideName: true },
      { key: f, name: "eq.pedal.freq", ...freqScale(range.min, range.max), def: d[f], format: fmtFreq },
      { key: g, name: "eq.pedal.gain", ...gainScale(PEDAL_GAIN), def: 0, format: fmtDb, bipolar: true },
    ],
  });
  return [
    band("eq.pedal.low", "lf", "lg", "lq", p.low),
    band("eq.pedal.mid", "mf", "mg", "mq", p.mid),
    band("eq.pedal.high", "hf", "hg", "hq", p.high),
    { section: "eq.pedal.filters", grid: true, knobs: [
      { key: "hpf", name: "eq.pedal.hpf", small: true, ...freqScale(p.hpf.min, p.hpf.max), def: p.hpf.min, offAt: 0,
        format: (v) => (v <= p.hpf.min + 0.05 ? t("eq.pedal.off") : fmtFreq(v)) },
      { key: "ls", name: "eq.pedal.lowShelf", small: true, ...gainScale(SHELF_GAIN), def: 0, format: fmtDb, bipolar: true },
      { key: "hs", name: "eq.pedal.highShelf", small: true, ...gainScale(SHELF_GAIN), def: 0, format: fmtDb, bipolar: true },
      { key: "lpf", name: "eq.pedal.lpf", small: true, ...freqScale(p.lpf.min, p.lpf.max), def: p.lpf.max, offAt: 1, fillFrom: 1,
        format: (v) => (v >= p.lpf.max - 0.05 ? t("eq.pedal.off") : fmtFreq(v)) },
    ] },
    { section: "eq.pedal.level", knobs: [
      { key: "lvl", name: "eq.pedal.level", ...{ toPos: (v) => linPos(v, LEVEL_MIN, LEVEL_MAX), fromPos: (pp) => Math.round(linVal(pp, LEVEL_MIN, LEVEL_MAX) * 10) / 10 },
        def: 0, format: fmtDb, bipolar: true, hideName: true },
    ] },
  ];
}

const TITLE_KEYS = { bass: "eq.panel.bass", guitar: "eq.panel.guitar", keys: "eq.panel.keys" };

export function mountPanel({ lane, kind, body, controls }) {
  const state = createEditState(lane);
  let c = controlsFromEq(kind, eq.getLaneEq(lane));
  const knobs = new Map();
  const sectionEls = [];

  // ─── Faceplate ───
  const root = el("div", `eq-pedal-panel eq-pedal-${kind}`);
  const plate = el("div", "eq-plate");
  const plateHead = el("div", "eq-plate-head");
  const plateTitle = el("span", "eq-plate-title");
  const led = el("span", "eq-plate-led");
  led.setAttribute("aria-hidden", "true");
  plateHead.append(plateTitle, led);
  const plateBody = el("div", "eq-plate-body");
  plate.append(plateHead, plateBody);

  for (const sec of layoutFor(kind)) {
    const box = el("div", `eq-plate-sec${sec.grid ? " grid" : ""}`);
    const head = el("span", "eq-plate-sec-name");
    box.append(head);
    const knobBox = el("div", "eq-plate-knobs");
    box.append(knobBox);
    for (const k of sec.knobs) {
      const knob = createKnob({
        size: k.small ? 28 : (kind === "keys" ? 44 : 36),
        small: !!k.small,
        value: c[k.key],
        def: k.def,
        toPos: k.toPos,
        fromPos: k.fromPos,
        format: k.format,
        bipolar: !!k.bipolar,
        offAt: k.offAt,
        fillFrom: k.fillFrom,
        onInput: (v, { first }) => change(k.key, v, first),
      });
      knob.spec = k;
      knob.sectionKey = sec.section;
      knobs.set(k.key, knob);
      knobBox.append(knob.el);
    }
    sectionEls.push({ head, key: sec.section });
    plateBody.append(box);
  }

  const curveBox = el("div", "eq-pedal-curve");
  const curveCv = el("canvas", "eq-mini-curve");
  curveCv.setAttribute("role", "img");
  curveBox.append(curveCv);
  root.append(plate, curveBox);
  body.append(root);
  containKeys(body);
  const curve = createMiniCurve(curveCv, lane);
  const spectrum = createLiveSpectrum(lane, {
    columns: () => curveCv.clientWidth,
    onFrame: (vals) => { curve.setSpectrum(vals); curve.draw(); },
  });

  // ─── Mixer side ───
  const toolbar = buildToolbar(controls, state, {
    presets: controlPresets(kind).map((p) => ({ key: p.key, apply: () => applyPreset(p) })),
    onChange: () => syncFromEq(),
  });
  const suggest = createSuggest({
    lane, kind, state,
    toolbar: controls.querySelector(".eq-toolbar"),
    cardHost: root,
    onShow: (on) => { root.classList.toggle("is-suggesting", on); },
    onGhost: (d) => { curve.setGhost(d); paint(); },
    onApplied: () => syncFromEq(),
  });
  containKeys(controls);
  bindUndoKeys(body, state, () => syncFromEq());
  bindUndoKeys(controls, state, () => syncFromEq());

  // ─── Changes ───
  function uiExtras() {
    const ab = eq.getLaneEq(lane).params.ui?.ab;
    return ab ? { ab } : {};
  }

  function change(key, v, first) {
    c = { ...c, [key]: v };
    const cur = eq.getLaneEq(lane);
    state.apply(panelEq(kind, c, { bypass: cur.bypass, ui: uiExtras() }), { record: first });
  }

  function applyPreset(p) {
    const base = kind === "keys" ? keysDefaults() : pedalDefaults(kind);
    const next = kind === "keys" ? normalizeKeys({ ...base, ...p.c }) : normalizePedal(kind, { ...base, ...p.c });
    c = next;
    const cur = eq.getLaneEq(lane);
    state.apply(panelEq(kind, c, { bypass: cur.bypass, ui: uiExtras() }));
  }

  /** The knobs follow the saved EQ (undo, A/B, reset, a song load). */
  function syncFromEq() {
    c = controlsFromEq(kind, eq.getLaneEq(lane));
    for (const [key, knob] of knobs) knob.set(c[key]);
    paint();
  }

  let queued = 0;
  function paint() {
    if (queued) return;
    queued = requestAnimationFrame(() => {
      queued = 0;
      curve.draw();
      const on = !eq.getLaneEq(lane).bypass;
      led.classList.toggle("on", on);
      toolbar.paint();
    });
  }

  function relabel() {
    plateTitle.textContent = t(TITLE_KEYS[kind]);
    for (const { head, key } of sectionEls) head.textContent = t(key);
    for (const knob of knobs.values()) {
      const name = t(knob.spec.name);
      const sec = t(knob.sectionKey);
      knob.relabel(name, sec === name ? name : t("eq.knob.aria", { section: sec, name }));
      // "Body" under "BODY" says it twice: one name per knob is enough.
      knob.el.classList.toggle("no-name", sec === name || !!knob.spec.hideName);
      knob.set(knob.value());
    }
    toolbar.relabel();
    suggest.relabel();
    paint();
  }

  const offChange = eq.onChange(({ laneId }) => {
    if (laneId === null) {
      state.history.clear();
      syncFromEq();
      return;
    }
    if (laneId !== lane) return;
    // Our own knob turns already hold these values; anything else (bypass,
    // undo from a shortcut, another tab of the page) is read back.
    const saved = controlsFromEq(kind, eq.getLaneEq(lane));
    if (JSON.stringify(saved) !== JSON.stringify(c)) syncFromEq();
    else paint();
  });

  const ro = typeof ResizeObserver === "function" ? new ResizeObserver(() => paint()) : null;
  ro?.observe(curveBox);
  relabel();

  return {
    refresh() { syncFromEq(); },
    relabel,
    suggest,
    destroy() {
      suggest.destroy();
      spectrum.destroy();
      offChange();
      ro?.disconnect();
      if (queued) cancelAnimationFrame(queued);
    },
  };
}
