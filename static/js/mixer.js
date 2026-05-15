import {
  STEM_NAMES, TRACK_NAMES, STEM_COLORS, STEM_DISPLAY, LANE_VOLUME_MAX,
} from "./constants.js";
import {
  mixerState, mixerEl, stemListEl, currentJobId, multitrack, trackIndex,
  masterVolume,
} from "./state.js";

function defaultMixerEntry() {
  return { volume: 1, muted: false, soloed: false };
}

export function ensureMixerStateDefaults() {
  for (const name of TRACK_NAMES) {
    if (!mixerState[name]) mixerState[name] = defaultMixerEntry();
  }
}

export function loadMixIntoState(jobId) {
  let stored = {};
  try {
    const raw = localStorage.getItem(`stemdeck:mix:${jobId}`);
    if (raw) stored = JSON.parse(raw);
  } catch (e) { console.warn("[mixer] failed to load mix state:", e); }
  for (const name of TRACK_NAMES) {
    Object.assign(mixerState[name], defaultMixerEntry(), stored[name] || {});
  }
}

export function resetMixerState() {
  for (const name of TRACK_NAMES) {
    Object.assign(mixerState[name], defaultMixerEntry());
  }
}

function saveMix() {
  if (!currentJobId) return;
  try {
    localStorage.setItem(
      `stemdeck:mix:${currentJobId}`,
      JSON.stringify(mixerState),
    );
  } catch (e) { console.warn("[mixer] failed to save mix state:", e); }
}

export function applyMix() {
  if (!multitrack) return;
  const anySolo = TRACK_NAMES.some((name) => trackIndex[name] !== undefined && mixerState[name]?.soloed);
  for (const name of TRACK_NAMES) {
    const s = mixerState[name];
    if (!s) continue;
    let effective = s.volume;
    if (s.muted) effective = 0;
    else if (anySolo && !s.soloed) effective = 0;
    const idx = trackIndex[name];
    if (idx === undefined) continue;

    const targetGain = effective * masterVolume;

    // HTMLAudioElement.volume is capped at [0,1] by the browser spec.
    // Values > 1 throw in Firefox/Safari (breaking the whole loop) and
    // are silently clamped in Chrome (boost never audible). Route each
    // HTMLAudioElement through a Web Audio GainNode instead — gain.value
    // accepts any positive number, enabling real boost and reliable mute.
    const audioEl = multitrack.audios?.[idx];
    if (audioEl instanceof HTMLMediaElement) {
      // Use the HTMLAudioElement's native volume instead of routing through a
      // MediaElementAudioSourceNode. WKWebView (Safari) does not reliably pass
      // audio from MediaElementSource → GainNode → destination to the speakers,
      // so we stay on the browser's default output path and cap gain at 1.0.
      audioEl.volume = Math.min(1, Math.max(0, targetGain));
    } else {
      multitrack.setTrackVolume(idx, targetGain);
    }
  }
}

export function updateLaneKnobVisual(knobEl, v) {
  const frac = Math.max(0, Math.min(1, v / LANE_VOLUME_MAX));
  knobEl.style.setProperty("--lane-pos", frac.toFixed(3));
  knobEl.setAttribute("aria-valuenow", v.toFixed(2));
  const val = knobEl.closest(".lane-header")?.querySelector(".mx-val");
  if (val) val.textContent = String(Math.round(frac * 100));
}

export function setLaneVolume(name, v) {
  const state = mixerState[name];
  if (!state) return;
  state.volume = Math.max(0, Math.min(LANE_VOLUME_MAX, v));
  const knob = mixerEl.querySelector(`.lane-knob[data-stem="${name}"]`);
  if (knob) updateLaneKnobVisual(knob, state.volume);
  applyMix();
  saveMix();
}

export function refreshMixerVisuals() {
  for (const name of TRACK_NAMES) {
    const state = mixerState[name];
    if (!state) continue;
    // Mixer-column lane header
    const row = mixerEl.querySelector(`.lane-header[data-stem="${name}"]`);
    if (row) {
      const muteBtn = row.querySelector(".mute");
      const soloBtn = row.querySelector(".solo");
      if (soloBtn) soloBtn.classList.toggle("active", state.soloed);
      const iconToggle = row.querySelector(".lane-icon-toggle");
      if (iconToggle) {
        iconToggle.classList.toggle("active", !state.muted);
        iconToggle.setAttribute("aria-pressed", String(!state.muted));
      }
      row.classList.toggle("muted", state.muted);
      const knob = row.querySelector(".lane-knob");
      if (knob) updateLaneKnobVisual(knob, state.volume);
    }
    // Stems-list panel row (mirrors the mixer column buttons)
    if (stemListEl) {
      const slRow = stemListEl.querySelector(`span[data-stem="${name}"]`);
      if (slRow) {
        const m = slRow.querySelector(".stem-mute");
        const s = slRow.querySelector(".stem-solo");
        const mon = slRow.querySelector(".stem-monitor");
        if (m) {
          m.classList.toggle("active", state.muted);
          m.setAttribute("aria-pressed", String(state.muted));
        }
        if (s) {
          s.classList.toggle("active", state.soloed);
          s.setAttribute("aria-pressed", String(state.soloed));
        }
        if (mon) {
          // Active when this stem is THE lone solo (the "monitor" target).
          const others = TRACK_NAMES.filter((n) => n !== name);
          const lone = state.soloed
            && others.every((n) => !mixerState[n]?.soloed);
          mon.classList.toggle("active", lone);
        }
        slRow.classList.toggle("muted", state.muted);
      }
    }
  }
}

export function setLaneControlsEnabled(enabled) {
  for (const b of mixerEl.querySelectorAll(".ms-btn")) b.disabled = !enabled;
  for (const b of mixerEl.querySelectorAll(".lane-icon-toggle")) b.disabled = !enabled;
  for (const a of mixerEl.querySelectorAll(".lane-dl")) {
    a.classList.toggle("disabled", !enabled);
    if (!enabled) {
      a.setAttribute("aria-disabled", "true");
      a.setAttribute("tabindex", "-1");
    } else {
      a.removeAttribute("aria-disabled");
      a.removeAttribute("tabindex");
    }
  }
  for (const k of mixerEl.querySelectorAll(".lane-knob")) {
    k.classList.toggle("disabled", !enabled);
    k.setAttribute("aria-disabled", String(!enabled));
    k.setAttribute("tabindex", enabled ? "0" : "-1");
  }
}

const MINI_WAVE_BARS = 40;
const MINI_WAVE_VIEWBOX_H = 26;

function emptyMiniWaveSvg(stemName) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "lane-mini-wave");
  svg.dataset.stem = stemName;
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("viewBox", `0 0 ${MINI_WAVE_BARS * 2} ${MINI_WAVE_VIEWBOX_H}`);
  return svg;
}

function makeMiniWaveSvg(stemName, color) {
  // Seeded placeholder bars used while real peaks haven't loaded yet.
  let s = 0;
  for (const c of stemName) s = (s * 31 + c.charCodeAt(0)) >>> 0;
  const rng = () => { s = (s * 9301 + 49297) % 233280; return s / 233280; };
  const svg = emptyMiniWaveSvg(stemName);
  for (let i = 0; i < MINI_WAVE_BARS; i++) {
    const env = Math.sin((i / MINI_WAVE_BARS) * Math.PI) * 0.7 + 0.3;
    const h = Math.max(2, env * (rng() * 0.6 + 0.25) * MINI_WAVE_VIEWBOX_H);
    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rect.setAttribute("x", `${i * 2}`);
    rect.setAttribute("y", `${(MINI_WAVE_VIEWBOX_H - h) / 2}`);
    rect.setAttribute("width", "1");
    rect.setAttribute("height", `${h}`);
    rect.setAttribute("fill", color);
    rect.setAttribute("opacity", "0.6");
    svg.appendChild(rect);
  }
  return svg;
}

export function renderRealMiniWave(stemName, audioBuffer, color) {
  const svg = mixerEl.querySelector(`.lane-mini-wave[data-stem="${stemName}"]`);
  if (!svg || !audioBuffer || typeof audioBuffer.getChannelData !== "function") return;
  const ch = audioBuffer.getChannelData(0);
  if (!ch || !ch.length) return;
  const binSize = Math.max(1, Math.floor(ch.length / MINI_WAVE_BARS));
  const peaks = new Array(MINI_WAVE_BARS);
  let max = 0;
  for (let i = 0; i < MINI_WAVE_BARS; i++) {
    const start = i * binSize;
    const end = i === MINI_WAVE_BARS - 1 ? ch.length : start + binSize;
    let p = 0;
    for (let j = start; j < end; j++) {
      const v = Math.abs(ch[j]);
      if (v > p) p = v;
    }
    peaks[i] = p;
    if (p > max) max = p;
  }
  const norm = max > 0 ? 1 / max : 0;
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  for (let i = 0; i < MINI_WAVE_BARS; i++) {
    const h = Math.max(1.5, peaks[i] * norm * MINI_WAVE_VIEWBOX_H);
    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rect.setAttribute("x", `${i * 2}`);
    rect.setAttribute("y", `${(MINI_WAVE_VIEWBOX_H - h) / 2}`);
    rect.setAttribute("width", "1");
    rect.setAttribute("height", `${h}`);
    rect.setAttribute("fill", color);
    rect.setAttribute("opacity", "0.95");
    svg.appendChild(rect);
  }
}

function downloadIcon() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "18");
  svg.setAttribute("height", "18");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML =
    '<path d="M12 3v11"></path>' +
    '<path d="m7.5 9.5 4.5 4.5 4.5-4.5"></path>' +
    '<rect x="5" y="17" width="14" height="4" rx="1.5"></rect>';
  return svg;
}

function makeVolumeKnob(stemName, color) {
  const wrap = document.createElement("div");
  wrap.className = "lane-knob mx-fader";
  wrap.dataset.stem = stemName;
  wrap.tabIndex = 0;
  wrap.setAttribute("role", "slider");
  wrap.setAttribute("aria-label", `${STEM_DISPLAY[stemName] || stemName} gain`);
  wrap.setAttribute("aria-valuemin", "0");
  wrap.setAttribute("aria-valuemax", String(LANE_VOLUME_MAX));
  wrap.setAttribute("aria-valuenow", "1");
  wrap.title = "Drag to adjust \u00b7 double-click to reset to 0\u00a0dB \u00b7 scroll to nudge";

  const track = document.createElement("div");
  track.className = "mx-fader-track";
  const fill = document.createElement("div");
  fill.className = "mx-fader-fill";
  fill.style.background = color;
  const knob = document.createElement("div");
  knob.className = "mx-fader-knob";
  knob.style.background = color;
  track.append(fill, knob);
  wrap.appendChild(track);

  // Legacy indicator kept for CSS compat (display:none via widgets.css)
  const indicator = document.createElement("div");
  indicator.className = "lane-knob-indicator";
  wrap.appendChild(indicator);

  let dragging = false;
  let startX = 0;
  let startVolume = 1;

  const onMove = (e) => {
    if (!dragging) return;
    const dx = e.clientX - startX;
    const travelPx = Math.max(1, wrap.getBoundingClientRect().width);
    setLaneVolume(stemName, startVolume + dx * (LANE_VOLUME_MAX / travelPx));
  };
  const onUp = () => {
    dragging = false;
    wrap.classList.remove("dragging");
    document.removeEventListener("pointermove", onMove);
    document.removeEventListener("pointerup", onUp);
  };
  wrap.addEventListener("pointerdown", (e) => {
    dragging = true;
    wrap.classList.add("dragging");
    startX = e.clientX;
    startVolume = mixerState[stemName]?.volume ?? 1;
    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", onUp);
    e.preventDefault();
  });
  wrap.addEventListener("dblclick", () => setLaneVolume(stemName, 1));
  wrap.addEventListener("wheel", (e) => {
    e.preventDefault();
    const cur = mixerState[stemName]?.volume ?? 1;
    const step = e.shiftKey ? 0.2 : 0.04;
    setLaneVolume(stemName, cur - Math.sign(e.deltaY) * step);
  }, { passive: false });
  wrap.addEventListener("keydown", (e) => {
    const cur = mixerState[stemName]?.volume ?? 1;
    let next = cur;
    if (e.code === "ArrowUp" || e.code === "ArrowRight") next = cur + 0.1;
    else if (e.code === "ArrowDown" || e.code === "ArrowLeft") next = cur - 0.1;
    else if (e.code === "Home") next = 1;
    else return;
    e.preventDefault();
    setLaneVolume(stemName, next);
  });

  return wrap;
}

function stemIconMarkup(stemName) {
  const common = 'class="lane-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" aria-hidden="true"';
  const icons = {
    vocals: `<svg ${common}><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"></path><path d="M19 10v2a7 7 0 0 1-14 0v-2"></path><path d="M12 19v3"></path></svg>`,
    drums: `<svg ${common}><path d="M7 13.5a5 5 0 0 0 10 0"></path><path d="M7 13.5h10"></path><circle cx="9" cy="10" r="2.5"></circle><circle cx="15" cy="10" r="2.5"></circle><path d="M4 6.5h5"></path><path d="M15 6.5h5"></path><path d="M6.5 6.5v5"></path><path d="M17.5 6.5v5"></path><path d="M10 18l-2 3"></path><path d="M14 18l2 3"></path><path d="M4 18l16-8"></path></svg>`,
    bass: `<svg ${common}><path d="M16.5 3h4v5h-3"></path><path d="M17.5 5.5 9.8 13.2"></path><path d="M10 13c1.6 2.2 1.1 5.1-1.2 6.5-2.1 1.3-5 .5-6-1.6-.9-1.9-.1-4.1 1.8-5 .9-.4 1.8-.4 2.8-.1.1-1.1.6-2.1 1.6-2.6 1.2-.6 2.6-.1 3.2 1.1"></path><path d="M6.7 16.4h.01"></path><path d="M13.5 9.5l3 3"></path><path d="M18.2 3v4.6"></path><path d="M20.5 3v4"></path></svg>`,
    guitar: `<svg ${common}><path d="M16 4.5 20 2l2 2-2.5 4"></path><path d="M18.2 5.8 10.2 13.8"></path><path d="M10.5 13.5c1.1 1.7.5 4.2-1.5 5.5-2.2 1.5-5.3.8-6.3-1.3-.8-1.7.1-3.6 1.9-4.2 1-.3 1.8-.1 2.7.5.1-1.1.6-2.1 1.6-2.6 1.4-.7 2.7.2 1.6 2.1Z"></path><path d="M6.5 15.1c1.3.6 2.2 1.5 2.9 2.8"></path><circle cx="7" cy="16.4" r="1.4"></circle><path d="M14 8l3 3"></path></svg>`,
    piano: `<svg ${common}><rect x="3" y="5" width="18" height="14" rx="2"></rect><path d="M7 5v14"></path><path d="M12 5v14"></path><path d="M17 5v14"></path><path d="M9.5 5v7"></path><path d="M14.5 5v7"></path></svg>`,
    other: `<svg ${common}><path d="M4 13v-2"></path><path d="M8 17V7"></path><path d="M12 21V3"></path><path d="M16 17V7"></path><path d="M20 13v-2"></path></svg>`,
    original: `<svg ${common}><path d="M9 18V5l12-2v13"></path><circle cx="6" cy="18" r="3"></circle><circle cx="18" cy="16" r="3"></circle></svg>`,
  };
  return icons[stemName] || icons.other;
}

export function renderMixerRow(stem) {
  const state = mixerState[stem.name];
  const color = STEM_COLORS[stem.name] || "#a0a0a0";
  const display = STEM_DISPLAY[stem.name] || stem.name;

  const row = document.createElement("div");
  row.className = "lane-header mx-row";
  row.dataset.stem = stem.name;

  // Col 1: stem icon
  const iconCell = document.createElement("div");
  iconCell.className = "mx-icon";
  iconCell.style.color = color;
  iconCell.innerHTML = stemIconMarkup(stem.name);

  // Hidden lane-stripe kept for CSS compat
  const stripe = document.createElement("div");
  stripe.className = "lane-stripe";
  stripe.style.background = color;
  row.appendChild(stripe);

  // Col 2: name
  const nameEl = document.createElement("span");
  nameEl.className = "mx-name";
  nameEl.style.color = color;
  nameEl.textContent = display;

  // Col 3: horizontal fader
  const fader = makeVolumeKnob(stem.name, color);

  // Col 4: VU meter
  const vu = document.createElement("div");
  vu.className = "lane-vu mx-meter";
  vu.dataset.stem = stem.name;
  vu.innerHTML = '<div class="lane-vu-bar mx-meter-fill"></div><div class="lane-vu-bar"></div>';

  // Col 5: value label
  const val = document.createElement("span");
  val.className = "mx-val";
  const initFrac = Math.max(0, Math.min(1, (state?.volume ?? 1) / LANE_VOLUME_MAX));
  val.textContent = String(Math.round(initFrac * 100));

  // Col 6: M button
  const muteBtn = document.createElement("button");
  muteBtn.type = "button";
  muteBtn.className = "lane-icon-toggle mx-btn mute";
  muteBtn.textContent = "M";
  muteBtn.setAttribute("aria-label", `Mute ${display}`);
  muteBtn.setAttribute("aria-pressed", String(state?.muted ?? false));
  if (!state?.muted) muteBtn.classList.add("active");

  // Col 7: S button
  const soloBtn = document.createElement("button");
  soloBtn.type = "button";
  soloBtn.className = "solo ms-btn mx-btn";
  soloBtn.textContent = "S";
  soloBtn.setAttribute("aria-label", `Solo ${display}`);
  soloBtn.setAttribute("aria-pressed", String(state?.soloed ?? false));
  if (state?.soloed) soloBtn.classList.add("active");

  // Col 8: download
  const dl = document.createElement("a");
  dl.className = "lane-dl mx-btn";
  dl.href = stem.url;
  dl.download = `${stem.name}.wav`;
  dl.title = `Download ${display}`;
  dl.appendChild(downloadIcon());

  row.append(iconCell, nameEl, fader, vu, val, muteBtn, soloBtn, dl);

  muteBtn.addEventListener("click", () => toggleStemMute(stem.name));
  soloBtn.addEventListener("click", () => toggleStemSolo(stem.name));

  row.classList.toggle("muted", state?.muted ?? false);
  if (state) updateLaneKnobVisual(fader, state.volume);

  return { row, vuEl: vu };
}

// ─── Stem-list panel (Stems sidebar) ───
//
// The stems-list panel renders a parallel set of M / S / Monitor controls
// that share state with the mixer-column lane-header buttons. Both UIs
// drive `mixerState`; either one updates the audio mix and both visuals
// re-render via refreshMixerVisuals().

export function toggleStemMute(name) {
  const state = mixerState[name];
  if (!state) return;
  state.muted = !state.muted;
  refreshMixerVisuals();
  applyMix();
  saveMix();
}

export function toggleStemSolo(name) {
  const state = mixerState[name];
  if (!state) return;
  state.soloed = !state.soloed;
  refreshMixerVisuals();
  applyMix();
  saveMix();
}

// "Monitor" = solo only this stem. If already the lone solo, clear all
// solos (toggle-style behavior, like Logic's "Solo Safe" / Reaper's
// solo-exclusive). Also clears mute on the target so it's audible.
export function soloOnlyStem(name) {
  const state = mixerState[name];
  if (!state) return;
  const others = TRACK_NAMES.filter((n) => n !== name);
  const isAlreadyAlone = state.soloed && others.every((n) => !mixerState[n]?.soloed);
  if (isAlreadyAlone) {
    state.soloed = false;
  } else {
    for (const n of TRACK_NAMES) {
      if (!mixerState[n]) continue;
      mixerState[n].soloed = (n === name);
    }
    state.muted = false;
  }
  refreshMixerVisuals();
  applyMix();
  saveMix();
}

export function resetMixer() {
  for (const name of TRACK_NAMES) {
    const s = mixerState[name];
    if (!s) continue;
    s.volume = 1;
    s.muted = false;
    s.soloed = false;
  }
  refreshMixerVisuals();
  applyMix();
  saveMix();
}

export function muteAll() {
  // Toggle: if every stem is muted, un-mute all; otherwise mute all.
  const allMuted = STEM_NAMES.every((n) => mixerState[n]?.muted);
  for (const name of TRACK_NAMES) {
    const s = mixerState[name];
    if (!s) continue;
    s.muted = !allMuted;
  }
  refreshMixerVisuals();
  applyMix();
  saveMix();
}

export function clearAllSolos() {
  for (const name of TRACK_NAMES) {
    const s = mixerState[name];
    if (!s) continue;
    s.soloed = false;
  }
  refreshMixerVisuals();
  applyMix();
  saveMix();
}

export function wireMixerToolbar() {
  document.getElementById("mixer-reset")?.addEventListener("click", resetMixer);
  document.getElementById("mixer-mute-all")?.addEventListener("click", muteAll);
  document.getElementById("mixer-clear-solo")?.addEventListener("click", clearAllSolos);
}

export function wireStemListControls() {
  if (!stemListEl) return;
  for (const btn of stemListEl.querySelectorAll(".stem-mute")) {
    btn.addEventListener("click", () => toggleStemMute(btn.dataset.stem));
    btn.addEventListener("keydown", (e) => {
      if (e.code === "Space" || e.code === "Enter") {
        e.preventDefault();
        toggleStemMute(btn.dataset.stem);
      }
    });
  }
  for (const btn of stemListEl.querySelectorAll(".stem-solo")) {
    btn.addEventListener("click", () => toggleStemSolo(btn.dataset.stem));
    btn.addEventListener("keydown", (e) => {
      if (e.code === "Space" || e.code === "Enter") {
        e.preventDefault();
        toggleStemSolo(btn.dataset.stem);
      }
    });
  }
  for (const btn of stemListEl.querySelectorAll(".stem-monitor")) {
    btn.addEventListener("click", () => soloOnlyStem(btn.dataset.stem));
  }
}
