// Performance's small level slider: the Backing track's and the take's volume
// in the timeline's lane labels (streamer.js), and the Monitor level under
// Hear myself (streamerFx.js). The scale is the Studio fader's
// (laneLevel.js): linear in gain, 0 dB at 1.
//
// A role="slider" element, so one control serves the mouse, touch and the
// keyboard: drag or tap along it, arrows step (Shift for bigger steps), Page
// Up and Page Down step big, Home is silence, End the top, and a double-click
// returns it to 0 dB. Its title and aria-valuetext give the level in dB.
//
// Nothing here knows what the level drives: `get()` reads it, `set(v)` is
// called on every move (the caller applies it at once: the engines glide each
// gain, so it does not click), and `commit(v)` once a move ends (a drag let
// go, a key), for whoever stores it less often than every move.

import { t } from "../i18n.js";
import { clampLevel, levelAt, levelFraction, levelKeyStep, levelText } from "../laneLevel.js";

/**
 * Build one slider. `labelKey` names it (aria-label, and the start of its
 * title); `max` is its top; `reset` the level a double-click returns to.
 * Returns { el, paint(v, enabled) }: `paint` shows a level (and whether it
 * can be used now) and does nothing when neither changed.
 */
export function makeLevelSlider({ cls = "", labelKey, max, reset = 1, get, set, commit = () => {} }) {
  const root = document.createElement("div");
  root.className = `sm-level ${cls}`.trim();
  root.setAttribute("role", "slider");
  root.tabIndex = 0;
  root.setAttribute("aria-valuemin", "0");
  root.setAttribute("aria-valuemax", String(max));
  root.setAttribute("aria-orientation", "horizontal");
  const track = document.createElement("span");
  track.className = "sm-level-track";
  const fill = document.createElement("span");
  fill.className = "sm-level-fill";
  // The 0 dB mark, where a double-click puts the level back.
  const unity = document.createElement("span");
  unity.className = "sm-level-unity";
  unity.style.left = `${(levelFraction(reset, max) * 100).toFixed(2)}%`;
  const thumb = document.createElement("span");
  thumb.className = "sm-level-thumb";
  track.append(fill, unity, thumb);
  root.append(track);

  let shown = null;
  let usable = true;
  let lang = null;

  function paint(v, enabled = true) {
    const level = clampLevel(v, max);
    const sig = `${level}|${enabled}`;
    if (sig === shown && lang === t(labelKey)) return;
    shown = sig;
    lang = t(labelKey);
    usable = Boolean(enabled);
    const pct = `${(levelFraction(level, max) * 100).toFixed(2)}%`;
    fill.style.width = pct;
    thumb.style.left = pct;
    const db = levelText(level);
    root.setAttribute("aria-label", t(labelKey));
    root.setAttribute("aria-valuenow", level.toFixed(2));
    root.setAttribute("aria-valuetext", t("streamer.vol.db", { db }));
    root.title = t("streamer.vol.title", { name: t(labelKey), db });
    root.setAttribute("aria-disabled", String(!usable));
    root.tabIndex = usable ? 0 : -1;
    root.classList.toggle("disabled", !usable);
  }

  const move = (v) => {
    if (!usable) return;
    const next = clampLevel(v, max);
    set(next);
    paint(get(), usable);
  };
  const at = (clientX) => {
    const r = track.getBoundingClientRect();
    move(levelAt((clientX - r.left) / (r.width || 1), max));
  };

  root.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || !usable) return;
    e.preventDefault();
    e.stopPropagation();
    root.focus({ preventScroll: true });
    try { root.setPointerCapture(e.pointerId); } catch { /* the pointer is gone */ }
    at(e.clientX);
    const onMove = (ev) => at(ev.clientX);
    const up = () => {
      root.removeEventListener("pointermove", onMove);
      root.removeEventListener("pointerup", up);
      root.removeEventListener("pointercancel", up);
      commit(get());
    };
    root.addEventListener("pointermove", onMove);
    root.addEventListener("pointerup", up);
    root.addEventListener("pointercancel", up);
  });
  // A click on the label around it must not reach the lane (seek, drag).
  root.addEventListener("click", (e) => e.stopPropagation());
  root.addEventListener("dblclick", (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (!usable) return;
    move(reset);
    commit(get());
  });
  root.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const next = levelKeyStep(get(), e.key, { shift: e.shiftKey, max });
    if (next === null) return;
    // The studio's arrows move the playhead; on this slider they move the level.
    e.preventDefault();
    e.stopPropagation();
    move(next);
    commit(get());
  });

  return { el: root, paint };
}
