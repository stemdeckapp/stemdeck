// The chain strip: one chain's controls, for a lane (fx/fxRows.js, Advanced
// mode) and for a live input (capture/liveFxUi.js, the recording panel).
// Both get the same strip, so a pedal looks and works the same on a stem
// and on the guitar plugged in now:
//
//   side: Effects (name) [On]  Instrument [Auto]  Trim (knob)  meter [Clip]
//   body: In > [Delay  On  x] > [Delay 2  On  x] > [+ Add effect] > Out
//         the selected pedal: preset, its selects and its knobs
//
// Reorder: drag a card (pointer or touch), or Alt+Left and Alt+Right on a
// focused card; Delete removes it. Focus stays on the moved card and a live
// region says where it went. On and off are a light and a word.
//
// Nothing here touches audio: every change is a new chain descriptor handed
// to the source's set(), which saves it and tells the engine. A source is:
//
//   { key,                       unique per chain (the selected pedal is kept per key)
//     name(), color,             for the labels and the title's colour
//     get(), set(desc),          the normalised chain and its setter
//     inst(), defaultInst(),     the instrument it is voiced for, and Auto's choice
//     meterPeak(),               linear peak after the trim now, or null (no signal)
//     clipPeak?(),               a live input's raw peak: shows the Clip light
//     onChange(cb) -> off,       cb() after any change of this chain
//     release?() }               called once when the strip goes

import { t } from "../i18n.js";
import { MAX_PEDALS, TRIM_MIN, TRIM_MAX, delaySeconds } from "./chainModel.js";
import { pedalSpec, controlsFor, presetsFor, typesFor, INSTRUMENTS } from "./pedalSpecs.js";
import * as fx from "../fxEngine.js";
import { createKnob } from "../eq/eqKnob.js";
import { logPos, logVal, linPos, linVal } from "../eq/eqPedalMath.js";
import { fmtFreq } from "../eq/eqGraphMath.js";
import { placeMenu } from "./menuPlace.js";

// source key -> selected pedal iid
const _selected = new Map();
// How long the Clip light stays lit after the last full-scale peak.
const CLIP_HOLD_MS = 1500;
// At or above this the input is at full scale: -0.09 dBFS.
const CLIP_PEAK = 0.99;

const instName = (inst) => t(`fx.inst.${inst}`);

/** "Delay", or "Delay 2" for the second delay of a chain. */
export function pedalLabel(desc, pedal) {
  const same = desc.pedals.filter((p) => p.type === pedal.type);
  const base = t(pedalSpec(pedal.type)?.name || "fx.pedal.delay");
  return same.length > 1 ? `${base} ${same.indexOf(pedal) + 1}` : base;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

const fmtTime = (s) => (s < 1 ? `${Math.round(s * 1000)} ms` : `${s.toFixed(2)} s`);
const fmtPct = (v) => `${Math.round(v * 100)} %`;
const fmtDb = (v) => `${v > 0 ? "+" : ""}${(Math.round(v * 10) / 10).toFixed(1)} dB`;
const fmtMs = (v) => `${v.toFixed(1)} ms`;

function scaleFor(c) {
  if (c.taper === "log") return { toPos: (v) => logPos(v, c.min, c.max), fromPos: (p) => logVal(p, c.min, c.max) };
  return { toPos: (v) => linPos(v, c.min, c.max), fromPos: (p) => linVal(p, c.min, c.max) };
}

function formatFor(c) {
  // A control whose lowest setting means off (a crossover) says so.
  if (c.offAtMin) {
    const f = formatFor({ ...c, offAtMin: false });
    return (v) => (v <= c.min + 1e-9 ? t("eq.pedal.off") : f(v));
  }
  if (c.unit === "rateHz") return (v) => `${v < 1 ? v.toFixed(2) : v.toFixed(1)} Hz`;
  if (c.unit === "oct") return (v) => t("fx.unit.oct", { v: v.toFixed(1) });
  if (c.unit === "s") return fmtTime;
  if (c.unit === "%") return fmtPct;
  if (c.unit === "Hz") return (v) => fmtFreq(v);
  if (c.unit === "dB") return fmtDb;
  if (c.unit === "ms") return fmtMs;
  return (v) => String(v);
}

/**
 * Mount one chain's strip: the controls in `side`, the cards and the pedal
 * panel in `body`; `region` (optional) gets the region's label. With
 * `vertical`, the cards stand top to bottom (a narrow panel: Performance's)
 * and a drag moves them up and down. Returns {refresh, destroy}.
 */
export function mountChainStrip(source, side, body, region = null, { vertical = false } = {}) {
  const key = source.key;
  const get = () => source.get();
  const apply = (desc) => source.set(desc);

  // ── Side ──
  const head = el("div", "eq-side-head");
  const titles = el("div", "eq-side-titles");
  const titleEl = el("span", "eq-side-title");
  titleEl.style.color = source.color || "#a0a0a0";
  const nameEl = el("span", "eq-side-lane");
  titles.append(titleEl, nameEl);
  const power = el("button", "eq-power fx-power");
  power.type = "button";
  power.addEventListener("click", () => {
    const d = get();
    apply({ ...d, on: d.on === false });
  });
  head.append(titles, power);

  const instWrap = el("label", "fx-inst-wrap");
  const instText = el("span", "fx-inst-label");
  const inst = el("select", "fx-inst");
  instWrap.append(instText, inst);
  inst.addEventListener("change", () => {
    const v = inst.value === "auto" ? null : inst.value;
    apply(fx.setInstrument(get(), v, source.defaultInst()));
  });

  const trimRow = el("div", "fx-trim-row");
  const trim = createKnob({
    size: 30,
    small: true,
    value: get().trim,
    def: 0,
    toPos: (v) => linPos(v, TRIM_MIN, TRIM_MAX),
    fromPos: (p) => Math.round(linVal(p, TRIM_MIN, TRIM_MAX) * 2) / 2,
    format: fmtDb,
    bipolar: true,
    onInput: (v) => apply({ ...get(), trim: v }),
  });
  trim.el.classList.add("fx-trim");
  const meter = el("div", "fx-meter");
  meter.setAttribute("role", "meter");
  meter.setAttribute("aria-valuemin", "-60");
  meter.setAttribute("aria-valuemax", "6");
  const meterBar = el("span", "fx-meter-bar");
  const meterText = el("span", "fx-meter-text", "-inf");
  meter.append(meterBar, meterText);
  trimRow.append(trim.el, meter);
  // The Clip light (a live input only): the raw input at full scale, which
  // no trim can undo in the recording, or the trimmed feed past full scale.
  let clip = null;
  let clipUntil = 0;
  if (typeof source.clipPeak === "function") {
    clip = el("span", "fx-clip");
    clip.setAttribute("role", "status");
    trimRow.append(clip);
  }
  side.append(head, instWrap, trimRow);

  // ── Body ──
  const strip = el("div", vertical ? "fx-strip fx-strip-vertical" : "fx-strip");
  strip.setAttribute("role", "list");
  if (vertical) strip.setAttribute("aria-orientation", "vertical");
  const live = el("div", "fx-live");
  live.setAttribute("aria-live", "polite");
  const panel = el("div", "fx-pedal");
  body.append(strip, panel, live);

  let addMenu = null;

  function announce(text) {
    live.textContent = "";
    // A fresh text node each time, so the same words are read again.
    requestAnimationFrame(() => { live.textContent = text; });
  }

  const chainInst = () => source.inst();

  // ── Cards ──
  let drag = null;

  function cardFor(desc, pedal, index) {
    const label = pedalLabel(desc, pedal);
    const item = el("div", "fx-card");
    item.setAttribute("role", "listitem");
    item.dataset.iid = pedal.iid;
    item.classList.toggle("selected", _selected.get(key) === pedal.iid);
    item.classList.toggle("off", !pedal.on);
    const grip = el("span", "fx-card-grip");
    grip.setAttribute("aria-hidden", "true");
    const main = el("button", "fx-card-main", label);
    main.type = "button";
    main.setAttribute("aria-pressed", String(_selected.get(key) === pedal.iid));
    main.setAttribute("aria-label", t("fx.card.aria", { pedal: label, pos: index + 1, count: desc.pedals.length }));
    main.addEventListener("click", () => { _selected.set(key, pedal.iid); render(); });
    main.addEventListener("keydown", (e) => onCardKey(e, pedal.iid, label));
    const pw = el("button", `fx-card-power${pedal.on ? " on" : ""}`, pedal.on ? t("fx.on") : t("fx.off"));
    pw.type = "button";
    pw.setAttribute("aria-pressed", String(pedal.on));
    pw.setAttribute("aria-label", t("fx.card.powerAria", { pedal: label }));
    pw.addEventListener("click", () => apply(fx.setPedalOn(get(), pedal.iid, !pedal.on)));
    const rm = el("button", "fx-card-remove", "×");
    rm.type = "button";
    rm.setAttribute("aria-label", t("fx.card.remove", { pedal: label }));
    rm.title = t("fx.card.remove", { pedal: label });
    rm.addEventListener("click", () => removeCard(pedal.iid, label));
    item.append(grip, main, pw, rm);
    item.addEventListener("pointerdown", (e) => startDrag(e, item, pedal.iid));
    return item;
  }

  function removeCard(iid, label) {
    const desc = get();
    const at = desc.pedals.findIndex((p) => p.iid === iid);
    apply(fx.removePedal(desc, iid));
    const next = get().pedals;
    _selected.set(key, next[Math.min(at, next.length - 1)]?.iid || null);
    render();
    announce(t("fx.live.removed", { pedal: label }));
    const focusIid = _selected.get(key);
    (focusIid ? strip.querySelector(`.fx-card[data-iid="${focusIid}"] .fx-card-main`) : strip.querySelector(".fx-add"))?.focus();
  }

  function moveTo(iid, to, label) {
    const desc = get();
    const from = desc.pedals.findIndex((p) => p.iid === iid);
    if (from < 0 || to === from || to < 0 || to >= desc.pedals.length) return;
    apply(fx.movePedal(desc, iid, to));
    render();
    strip.querySelector(`.fx-card[data-iid="${iid}"] .fx-card-main`)?.focus();
    announce(t("fx.live.moved", { pedal: label, pos: to + 1, count: desc.pedals.length }));
  }

  // Earlier and later in the chain: left and right, and in a vertical strip
  // up and down as well.
  const stepOf = (key) => {
    if (key === "ArrowLeft" || (vertical && key === "ArrowUp")) return -1;
    if (key === "ArrowRight" || (vertical && key === "ArrowDown")) return 1;
    return 0;
  };

  function onCardKey(e, iid, label) {
    const desc = get();
    const at = desc.pedals.findIndex((p) => p.iid === iid);
    const step = stepOf(e.key);
    if (e.altKey && step) {
      e.preventDefault();
      moveTo(iid, at + step, label);
    } else if (e.key === "Delete" || e.key === "Backspace") {
      // The card's own Delete: nothing else (the studio's "trash the open
      // song") may act on it.
      e.preventDefault();
      e.stopPropagation();
      removeCard(iid, label);
    } else if (!e.altKey && step) {
      e.preventDefault();
      const cards = [...strip.querySelectorAll(".fx-card-main")];
      cards[Math.max(0, Math.min(cards.length - 1, at + step))]?.focus();
    }
  }

  // Pointer drag: a card follows by order; the drop shows as a gap. Along x,
  // or along y in a vertical strip.
  const along = (e) => (vertical ? e.clientY : e.clientX);
  function startDrag(e, item, iid) {
    if (e.button !== 0 || e.target.closest(".fx-card-power, .fx-card-remove")) return;
    // No pointer capture yet: a capture on press sends the click to the card
    // instead of its button, and a plain click must select the pedal.
    drag = { iid, item, x0: along(e), id: e.pointerId, moving: false, to: -1 };
  }
  function dropIndex(x) {
    const cards = [...strip.querySelectorAll(".fx-card")].filter((c) => c !== drag.item);
    let to = cards.length;
    for (let i = 0; i < cards.length; i++) {
      const r = cards[i].getBoundingClientRect();
      const mid = vertical ? r.top + r.height / 2 : r.left + r.width / 2;
      if (x < mid) { to = i; break; }
    }
    return to;
  }
  strip.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const pos = along(e);
    if (!drag.moving && Math.abs(pos - drag.x0) < 5) return;
    // A drag has started: the card follows the pointer even off the strip.
    if (!drag.moving) drag.item.setPointerCapture?.(e.pointerId);
    drag.moving = true;
    drag.item.classList.add("dragging");
    drag.item.style.transform = vertical ? `translateY(${pos - drag.x0}px)` : `translateX(${pos - drag.x0}px)`;
    drag.to = dropIndex(pos);
    const others = [...strip.querySelectorAll(".fx-card")].filter((c) => c !== drag.item);
    others.forEach((c, i) => {
      c.classList.toggle("gap-before", i === drag.to);
      c.classList.toggle("gap-after", drag.to === others.length && i === others.length - 1);
    });
  });
  const endDrag = (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const d = drag;
    drag = null;
    if (!d.moving) return;
    const desc = get();
    const pedal = desc.pedals.find((p) => p.iid === d.iid);
    if (pedal) moveTo(d.iid, d.to, pedalLabel(desc, pedal));
    render();
  };
  strip.addEventListener("pointerup", endDrag);
  strip.addEventListener("pointercancel", endDrag);

  // The add menu is a popup anchored to "+ Add effect": in the top layer
  // where the browser has one (popover), so no scrolling or clipping
  // ancestor (the strip scrolls sideways, a lane row hides its overflow) and
  // no size container (Performance's view) can cut it; placed from the
  // button's rect (placeMenu), and closed on a scroll, a resize, a press
  // outside it and Escape. It stays inside `body` in the document, so a
  // press on it is a press inside the panel that holds the strip.
  let menuBtn = null;
  // Where the button was when the menu was placed: a scroll that moves it
  // closes the menu (it would float away from its button). Scroll events
  // arrive a frame late, so one from a scroll that ended before the menu
  // opened (the button brought into view) leaves the button where it was.
  let menuAnchor = null;
  const onMenuOutside = (e) => {
    if (!addMenu || addMenu.contains(e.target) || menuBtn?.contains(e.target)) return;
    closeAddMenu();
  };
  const onMenuScroll = (e) => {
    if (!addMenu || (e.target instanceof Node && addMenu.contains(e.target))) return;
    const r = menuBtn?.getBoundingClientRect();
    if (!r || !menuAnchor || Math.abs(r.top - menuAnchor.top) > 0.5 || Math.abs(r.left - menuAnchor.left) > 0.5) closeAddMenu();
  };
  const onMenuResize = () => closeAddMenu();

  function placeAddMenu() {
    if (!addMenu || !menuBtn) return;
    addMenu.style.maxHeight = "";
    const r = menuBtn.getBoundingClientRect();
    menuAnchor = { top: r.top, left: r.left };
    const at = placeMenu(r, addMenu.offsetWidth, addMenu.offsetHeight, window.innerWidth, window.innerHeight);
    addMenu.style.left = `${Math.round(at.left)}px`;
    addMenu.style.top = `${Math.round(at.top)}px`;
    addMenu.style.maxHeight = at.maxHeight === null ? "" : `${Math.floor(at.maxHeight)}px`;
  }

  function openAddMenu(btn) {
    closeAddMenu();
    menuBtn = btn;
    addMenu = el("div", "fx-add-menu");
    addMenu.setAttribute("role", "menu");
    addMenu.setAttribute("aria-label", t("fx.add.menuAria"));
    const popover = typeof addMenu.showPopover === "function";
    if (popover) addMenu.popover = "manual";
    for (const type of typesFor(chainInst())) {
      const it = el("button", "fx-add-item", t(pedalSpec(type).name));
      it.type = "button";
      it.setAttribute("role", "menuitem");
      it.dataset.type = type;
      it.addEventListener("click", () => {
        const before = get();
        const next = fx.addPedal(before, type, chainInst());
        closeAddMenu();
        if (!next) return;
        const added = next.pedals.find((p) => !before.pedals.some((q) => q.iid === p.iid));
        apply(next);
        if (added) _selected.set(key, added.iid);
        render();
        const desc = get();
        const pos = desc.pedals.findIndex((p) => p.iid === added?.iid);
        if (added) {
          announce(t("fx.live.added", { pedal: pedalLabel(desc, desc.pedals[pos]), pos: pos + 1, count: desc.pedals.length }));
          strip.querySelector(`.fx-card[data-iid="${added.iid}"] .fx-card-main`)?.focus();
        }
      });
      addMenu.append(it);
    }
    addMenu.addEventListener("keydown", (e) => {
      const items = [...addMenu.querySelectorAll(".fx-add-item")];
      const at = items.indexOf(document.activeElement);
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const step = e.key === "ArrowDown" ? 1 : -1;
        items[(at + step + items.length) % items.length]?.focus();
      } else if (e.key === "Home" || e.key === "End") {
        e.preventDefault();
        (e.key === "Home" ? items[0] : items.at(-1))?.focus();
      } else if (e.key === "Escape") {
        // The menu's own Escape: the panel it sits in (the recording panel)
        // must not close with it.
        e.preventDefault();
        e.stopPropagation();
        closeAddMenu();
        btn.focus();
      } else if (e.key === "Tab") {
        closeAddMenu();
      }
    });
    // Out of the strip's row (it scrolls sideways and is redrawn on every
    // change), beside it in the strip's own panel.
    body.append(addMenu);
    if (popover) {
      try { addMenu.showPopover(); } catch (err) { console.warn("[fx] the add menu did not open as a popup:", err); }
    }
    placeAddMenu();
    btn.setAttribute("aria-expanded", "true");
    document.addEventListener("pointerdown", onMenuOutside, true);
    window.addEventListener("scroll", onMenuScroll, true);
    window.addEventListener("resize", onMenuResize);
    addMenu.querySelector("button")?.focus();
  }
  function closeAddMenu() {
    document.removeEventListener("pointerdown", onMenuOutside, true);
    window.removeEventListener("scroll", onMenuScroll, true);
    window.removeEventListener("resize", onMenuResize);
    if (addMenu) {
      try { if (addMenu.matches?.(":popover-open")) addMenu.hidePopover(); } catch (err) { console.warn("[fx] closing the add menu failed:", err); }
      addMenu.remove();
    }
    if (menuBtn?.isConnected) menuBtn.setAttribute("aria-expanded", "false");
    addMenu = null;
    menuBtn = null;
    menuAnchor = null;
  }

  // ── The selected pedal ──
  function renderPanel(desc) {
    panel.replaceChildren();
    const pedal = desc.pedals.find((p) => p.iid === _selected.get(key));
    if (!pedal) {
      delete panel.dataset.type;
      panel.append(el("p", "fx-empty", desc.pedals.length ? "" : t("fx.empty")));
      return;
    }
    const inst2 = chainInst();
    const label = pedalLabel(desc, pedal);
    // The Guide tells one pedal's "Rate" from another's by this.
    panel.dataset.type = pedal.type;
    const top = el("div", "fx-pedal-head");
    top.append(el("span", "fx-pedal-name", label));
    const presetWrap = el("label", "fx-field");
    presetWrap.append(el("span", "fx-field-name", t("fx.preset.label")));
    const preset = el("select", "fx-preset");
    preset.append(new Option(t("fx.preset.custom"), ""));
    for (const p of presetsFor(pedal.type, inst2)) preset.append(new Option(t(p.name), p.id));
    preset.value = pedal.preset || "";
    preset.addEventListener("change", () => {
      if (!preset.value) return;
      apply(fx.applyPreset(get(), pedal.iid, preset.value, inst2));
      render();
    });
    presetWrap.append(preset);
    top.append(presetWrap);
    const controls = controlsFor(pedal.type, inst2);
    for (const c of controls.filter((x) => x.kind === "select")) {
      const w = el("label", "fx-field");
      w.append(el("span", "fx-field-name", t(c.name)));
      const s = el("select", "fx-select");
      s.dataset.ctl = c.id;
      for (const o of c.options) s.append(new Option(t(o.name), o.v));
      s.value = pedal.p[c.id];
      s.addEventListener("change", () => {
        apply(fx.setPedalParam(get(), pedal.iid, c.id, s.value, inst2));
        render();
      });
      w.append(s);
      top.append(w);
    }
    const knobRow = el("div", "fx-knobs");
    const bpm = fx.chainTempo();
    for (const c of controls.filter((x) => x.kind === "knob")) {
      const synced = c.id === "time" && pedal.p.sync && pedal.p.sync !== "off" && bpm;
      const fmt = formatFor(c);
      const knob = createKnob({
        size: 38,
        value: pedal.p[c.id],
        def: pedal.p[c.id],
        ...scaleFor(c),
        format: synced ? () => t("fx.synced", { time: fmtTime(delaySeconds(pedal.p, bpm)), bpm: Math.round(bpm) }) : fmt,
        onInput: (v) => apply(fx.setPedalParam(get(), pedal.iid, c.id, v, inst2)),
      });
      knob.el.classList.add("fx-knob");
      knob.el.dataset.ctl = c.id;
      knob.el.classList.toggle("is-synced", !!synced);
      knob.relabel(t(c.name), t("fx.knob.aria", { pedal: label, name: t(c.name) }));
      knobRow.append(knob.el);
    }
    panel.append(top, knobRow);
  }

  function render() {
    // The strip is drawn again with a new "+ Add effect": a menu open on the
    // old one closes (it was removed with the row before it was a popup).
    closeAddMenu();
    const desc = get();
    if (!desc.pedals.some((p) => p.iid === _selected.get(key))) _selected.set(key, desc.pedals[0]?.iid || null);
    const name = source.name();
    titleEl.textContent = t("fx.panel.title");
    nameEl.textContent = name;
    region?.setAttribute("aria-label", t("fx.panel.regionAria", { name }));
    const on = desc.on !== false;
    power.classList.toggle("on", on);
    power.setAttribute("aria-pressed", String(on));
    power.textContent = on ? t("fx.on") : t("fx.off");
    power.title = t(on ? "fx.power.bypassTitle" : "fx.power.enableTitle");
    power.setAttribute("aria-label", t("fx.power.aria", { name }));
    instText.textContent = t("fx.inst.label");
    inst.replaceChildren(new Option(t("fx.inst.auto", { inst: instName(source.defaultInst()) }), "auto"));
    for (const i of INSTRUMENTS) inst.append(new Option(instName(i), i));
    inst.value = desc.inst || "auto";
    inst.setAttribute("aria-label", t("fx.inst.aria", { name }));
    trim.set(desc.trim);
    trim.relabel(t("fx.trim"), t("fx.trim.aria", { name }));
    meter.setAttribute("aria-label", t("fx.meter.aria", { name }));
    meter.title = t("fx.meter");
    if (clip) {
      clip.textContent = t("rec.liveFx.clip");
      clip.title = t("rec.liveFx.clipTitle");
    }

    strip.replaceChildren();
    strip.setAttribute("aria-label", t("fx.strip.aria", { name }));
    // The signal's direction, drawn for the eye; the list order says it.
    const arrow = () => {
      const a = el("span", "fx-arrow");
      a.setAttribute("aria-hidden", "true");
      return a;
    };
    strip.append(el("span", "fx-io", t("fx.in")));
    desc.pedals.forEach((pedal, i) => {
      strip.append(arrow());
      strip.append(cardFor(desc, pedal, i));
    });
    strip.append(arrow());
    const add = el("button", "fx-add", `+ ${t("fx.add")}`);
    add.type = "button";
    const full = desc.pedals.length >= MAX_PEDALS;
    add.disabled = full;
    add.title = full ? t("fx.add.full", { n: MAX_PEDALS }) : t("fx.add");
    add.setAttribute("aria-haspopup", "menu");
    add.setAttribute("aria-expanded", "false");
    add.addEventListener("click", () => (addMenu ? closeAddMenu() : openAddMenu(add)));
    strip.append(add, arrow(), el("span", "fx-io", t("fx.out")));
    renderPanel(desc);
  }

  // ── Meter ──
  let raf = 0;
  function meterFrame() {
    raf = requestAnimationFrame(meterFrame);
    const peak = source.meterPeak();
    const db = peak > 0 ? 20 * Math.log10(peak) : -Infinity;
    const shown = Number.isFinite(db) ? Math.max(-60, Math.min(6, db)) : -60;
    meterBar.style.width = `${((shown + 60) / 66) * 100}%`;
    meter.classList.toggle("hot", db > -1);
    meterText.textContent = Number.isFinite(db) ? `${db.toFixed(1)} dB` : "-inf";
    meter.setAttribute("aria-valuenow", String(Math.round(shown)));
    if (clip) {
      const now = performance.now();
      if ((source.clipPeak() || 0) >= CLIP_PEAK || (peak || 0) > 1) clipUntil = now + CLIP_HOLD_MS;
      clip.classList.toggle("on", now < clipUntil);
    }
  }
  raf = requestAnimationFrame(meterFrame);

  const off = source.onChange(() => render());
  render();
  return {
    refresh: render,
    destroy() {
      off();
      cancelAnimationFrame(raf);
      source.release?.();
      closeAddMenu();
    },
  };
}
