// A rotary knob for the pedal and keys panels: SVG, 270 degrees of travel
// (seven o'clock to five o'clock), a value arc from the left stop or, for a
// boost/cut control, from noon. It is a slider to assistive technology.
//
// Pointer: drag up or right to turn it up (Shift: finer). Wheel: a notch at a
// time. Keys: arrows (Shift: finer), Page Up/Down (coarse), Home/End.
// Double-click: back to its default.
//
// The knob works in positions 0..1; `toPos`/`fromPos` map them to values.

const NS = "http://www.w3.org/2000/svg";
const START = (225 * Math.PI) / 180; // seven o'clock, measured from three o'clock, counter-clockwise
const SWEEP = (270 * Math.PI) / 180;

function svg(tag, attrs) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, String(v));
  return e;
}

/** A point on the knob's circle at position p (0..1). */
function at(cx, cy, rad, p) {
  const a = START - p * SWEEP;
  return [cx + rad * Math.cos(a), cy - rad * Math.sin(a)];
}

function arcPath(cx, cy, rad, p0, p1) {
  const a = Math.min(p0, p1);
  const b = Math.max(p0, p1);
  if (b - a < 0.0005) return "";
  const [x0, y0] = at(cx, cy, rad, a);
  const [x1, y1] = at(cx, cy, rad, b);
  const large = (b - a) * SWEEP > Math.PI ? 1 : 0;
  return `M${x0.toFixed(2)} ${y0.toFixed(2)} A${rad} ${rad} 0 ${large} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
}

/**
 * opts: {
 *   size (px), label, ariaLabel, value, def, toPos(v), fromPos(p),
 *   format(v) -> text, bipolar (arc from noon), onInput(v, {first}),
 *   step (fine key step, in position), offAt (position at which it reads "off"),
 *   fillFrom (where the lit arc starts, 0..1)
 * }
 * Returns {el, set(value), relabel(label, ariaLabel), value()}.
 */
export function createKnob(opts) {
  const size = opts.size || 40;
  const cx = size / 2;
  const cy = size / 2;
  const ring = size / 2 - 3;
  const capR = ring - 5;
  let pos = opts.toPos(opts.value);
  let value = opts.value;

  const root = document.createElement("div");
  root.className = `eq-knob${opts.small ? " small" : ""}`;
  const dial = document.createElement("div");
  dial.className = "eq-knob-dial";
  dial.tabIndex = 0;
  dial.setAttribute("role", "slider");
  dial.setAttribute("aria-valuemin", "0");
  dial.setAttribute("aria-valuemax", "100");
  const s = svg("svg", { width: size, height: size, viewBox: `0 0 ${size} ${size}`, "aria-hidden": "true" });
  const track = svg("path", { class: "eq-knob-track", d: arcPath(cx, cy, ring, 0, 1) });
  const fill = svg("path", { class: "eq-knob-fill" });
  const cap = svg("circle", { class: "eq-knob-cap", cx, cy, r: capR });
  const pointer = svg("line", { class: "eq-knob-pointer", x1: cx, y1: cy - capR + 3, x2: cx, y2: cy - capR * 0.35 });
  s.append(track, fill, cap, pointer);
  dial.append(s);
  const name = document.createElement("span");
  name.className = "eq-knob-name";
  const val = document.createElement("span");
  val.className = "eq-knob-val";
  root.append(dial, name, val);

  function paint() {
    // The lit arc: from noon for a boost/cut, from the right stop for a
    // control that is off at its top (a high cut), from the left otherwise.
    const from = opts.fillFrom ?? (opts.bipolar ? 0.5 : 0);
    fill.setAttribute("d", arcPath(cx, cy, ring, from, pos));
    const deg = -135 + pos * 270;
    pointer.setAttribute("transform", `rotate(${deg.toFixed(2)} ${cx} ${cy})`);
    const text = opts.format(value);
    val.textContent = text;
    dial.setAttribute("aria-valuenow", String(Math.round(pos * 100)));
    dial.setAttribute("aria-valuetext", text);
    root.classList.toggle("is-off", opts.offAt !== undefined && Math.abs(pos - opts.offAt) < 0.0005);
  }

  function setPos(p, first) {
    const np = Math.min(1, Math.max(0, p));
    const nv = opts.fromPos(np);
    if (nv === value && np === pos) return;
    pos = np;
    value = nv;
    paint();
    opts.onInput?.(value, { first });
  }

  // Pointer: vertical travel (up = more), horizontal counts too.
  let drag = null;
  dial.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    dial.focus({ preventScroll: true });
    drag = { id: e.pointerId, x: e.clientX, y: e.clientY, p: pos, first: true };
    dial.setPointerCapture(e.pointerId);
    root.classList.add("turning");
  });
  dial.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const travel = e.shiftKey ? 800 : 180;
    const d = (drag.y - e.clientY + (e.clientX - drag.x)) / travel;
    if (Math.abs(d) < 0.001) return;
    setPos(drag.p + d, drag.first);
    drag.first = false;
  });
  const end = (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    drag = null;
    root.classList.remove("turning");
  };
  dial.addEventListener("pointerup", end);
  dial.addEventListener("pointercancel", end);

  let wheelTimer = 0;
  let wheelFirst = true;
  dial.addEventListener("wheel", (e) => {
    e.preventDefault();
    e.stopPropagation();
    const step = e.shiftKey ? 0.005 : 0.025;
    setPos(pos + (e.deltaY < 0 ? step : -step), wheelFirst);
    wheelFirst = false;
    clearTimeout(wheelTimer);
    wheelTimer = setTimeout(() => { wheelFirst = true; }, 500);
  }, { passive: false });

  dial.addEventListener("keydown", (e) => {
    const fine = opts.step || 0.01;
    let p = null;
    if (e.key === "ArrowUp" || e.key === "ArrowRight") p = pos + (e.shiftKey ? fine / 4 : fine);
    else if (e.key === "ArrowDown" || e.key === "ArrowLeft") p = pos - (e.shiftKey ? fine / 4 : fine);
    else if (e.key === "PageUp") p = pos + 0.1;
    else if (e.key === "PageDown") p = pos - 0.1;
    else if (e.key === "Home") p = 0;
    else if (e.key === "End") p = 1;
    if (p === null) return;
    e.preventDefault();
    setPos(p, !e.repeat);
  });

  dial.addEventListener("dblclick", () => {
    setPos(opts.toPos(opts.def), true);
  });

  function relabel(label, ariaLabel) {
    name.textContent = label;
    dial.setAttribute("aria-label", ariaLabel || label);
  }

  relabel(opts.label || "", opts.ariaLabel);
  paint();

  return {
    el: root,
    set(v) {
      value = v;
      pos = opts.toPos(v);
      paint();
    },
    relabel,
    value: () => value,
  };
}
