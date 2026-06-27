// StemDeck mobile — vanilla port of the Claude Design prototype
// (design/mobile/StemDeck-Mobile.dc.html). No framework, no build step,
// to match the rest of static/. Library + Mixer are wired to the real API
// (GET /api/jobs, /api/jobs/{id}, the Web Audio engine, mixdown export).
// Extract is still mock pending the SSE/upload wiring (next step).
import { fetchJobs, jobToCard } from "../js/shared/jobs.js";
import { createAudioEngine } from "../js/audioEngine.js";

// Per-stem label + color, keyed by the backend stem name. Unknown names fall
// back to a rotating palette so non-standard models still render sensibly.
const STEM_META = {
  vocals: { label: "Vocals", color: "#f0506e" },
  drums: { label: "Drums", color: "#f5862b" },
  bass: { label: "Bass", color: "#f5c518" },
  guitar: { label: "Guitar", color: "#3fcf6e" },
  piano: { label: "Piano", color: "#9b6cf0" },
  other: { label: "Other", color: "#4a9bf5" },
};
const FALLBACK_COLORS = ["#f0506e", "#f5862b", "#f5c518", "#3fcf6e", "#9b6cf0", "#4a9bf5", "#2bd4c4", "#c44ad0"];
function stemMeta(name, idx) {
  return STEM_META[name] || { label: name.charAt(0).toUpperCase() + name.slice(1), color: FALLBACK_COLORS[idx % FALLBACK_COLORS.length] };
}

// Stem chips shown on the (still-mock) Extract screen.
const EXTRACT_STEMS = Object.entries(STEM_META).map(([id, m]) => ({ id, name: m.label, color: m.color }));

const DEFAULT_GRADIENT = "linear-gradient(150deg,#3a3a42,#202026)";
const FILTERS = ["All", "Favorites"];

// ─── Web Audio engine (reused from desktop: ../js/audioEngine.js) ───
let engine = null;
let engineTrackId = null;
let engineToken = 0; // guards against races when switching tracks mid-decode
let engineReady = false; // true once the current engine has decodable stems
let pendingPlay = false;

// Shared AudioContext, created + resumed inside a user gesture (mobile/iOS
// won't start audio otherwise). Once running it stays running, so later
// programmatic play() calls work even outside a gesture.
let audioCtx = null;
function ensureAudioCtx() {
  const AC = window.AudioContext || window.webkitAudioContext;
  if (!AC) return null;
  if (!audioCtx) audioCtx = new AC();
  if (audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
  return audioCtx;
}

const ICON = {
  chevron: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>',
  dots: '<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></svg>',
  clock: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
  prev: '<svg width="26" height="26" viewBox="0 0 24 24" fill="currentColor"><path d="M7 6v12M19 6l-9 6 9 6z"/></svg>',
  next: '<svg width="26" height="26" viewBox="0 0 24 24" fill="currentColor"><path d="M17 6v12M5 6l9 6-9 6z"/></svg>',
  loop: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 2l4 4-4 4"/><path d="M3 11v-1a4 4 0 0 1 4-4h14"/><path d="M7 22l-4-4 4-4"/><path d="M21 13v1a4 4 0 0 1-4 4H3"/></svg>',
  play: (sz, fill) => `<svg width="${sz}" height="${sz}" viewBox="0 0 24 24" fill="${fill}"><path d="M8 5.5v13l11-6.5z"/></svg>`,
  pause: (sz, fill) => `<svg width="${sz}" height="${sz}" viewBox="0 0 24 24" fill="${fill}"><rect x="6" y="5" width="4" height="14" rx="1.3"/><rect x="14" y="5" width="4" height="14" rx="1.3"/></svg>`,
  download: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="M8 11l4 4 4-4"/><path d="M4 21h16"/></svg>',
  search: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#65656d" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4-4"/></svg>',
  link: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#65656d" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"/><path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/></svg>',
  upload: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V4"/><path d="M8 8l4-4 4 4"/><path d="M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>',
  check: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>',
  scissors: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M20 4L8.12 15.88M14.47 14.48L20 20M8.12 8.12L12 12"/></svg>',
  tabLib: '<svg width="23" height="23" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>',
  tabMix: '<svg width="23" height="23" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 8h16M4 16h16"/><circle cx="9" cy="8" r="2.4" fill="currentColor" stroke="none"/><circle cx="15" cy="16" r="2.4" fill="currentColor" stroke="none"/></svg>',
  tabExt: '<svg width="23" height="23" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="2.6"/><circle cx="6" cy="18" r="2.6"/><path d="M20 4L8.5 15.5M14 14.5L20 20M8.5 8.5L11.5 11.5"/></svg>',
};

const state = {
  tab: "mixer",
  mixerView: "stems",
  playing: false,
  progress: 0,
  vols: {}, // per-stem gain 0..1, keyed by backend stem name
  muted: {},
  solo: {},
  selected: { vocals: true, drums: true, bass: true, guitar: true, piano: true, other: true },
  quality: "High",
  filter: "All",
  // Library data from /api/jobs. libState: "loading" | "ready" | "empty" | "error".
  tracks: [],
  libState: "loading",
  swipedTrackId: null, // track whose swipe-to-delete is revealed
  current: null, // selected track card (with .detail once loaded), or null
  // Extract screen.
  extractUrl: "",
  extractFile: null, // File chosen for upload
  extractJob: null, // { id, title, status, progress, stage } while a job runs
};

let extractES = null; // EventSource for the active extraction
let extractPoll = null; // REST poll fallback timer

const app = document.getElementById("app");

function fmt(sec) {
  if (!isFinite(sec) || sec < 0) sec = 0;
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return m + ":" + String(s).padStart(2, "0");
}
function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}

// ─── Mixer / engine helpers ───
function lanes() {
  return state.current?.detail?.lanes || [];
}
function curDuration() {
  return state.current?.detail?.duration || 0;
}
function anySolo() {
  return Object.values(state.solo).some(Boolean);
}
function laneActive(name) {
  const muted = !!state.muted[name];
  const soloed = !!state.solo[name];
  return !(muted || (anySolo() && !soloed));
}
function effectiveGain(name) {
  return laneActive(name) ? (state.vols[name] ?? 1) : 0;
}
function applyMix() {
  if (!engine) return;
  for (const l of lanes()) engine.setGain(l.name, effectiveGain(l.name));
}

function cap(s) {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}
function drLabel(dr) {
  if (dr < 7) return "Compressed";
  if (dr < 10) return "Moderate";
  if (dr < 14) return "High";
  return "Wide";
}
function stabilityLabel(pct) {
  if (pct >= 90) return "Very stable";
  if (pct >= 70) return "Stable";
  if (pct >= 40) return "Variable";
  return "Unstable";
}

// Build the Analysis tab's stat cards + stem-presence rows from a job detail.
function extractAnalysis(d, laneList) {
  const stats = [];
  if (d.key) stats.push({ k: "KEY", v: d.key, s: d.scale ? cap(d.scale) : "" });
  if (d.bpm) stats.push({ k: "TEMPO", v: String(Math.round(d.bpm)), s: "BPM" });
  if (d.lufs != null) stats.push({ k: "LUFS", v: Number(d.lufs).toFixed(1), s: d.peak_db != null ? `Peak ${Number(d.peak_db).toFixed(1)} dB` : "Integrated" });
  if (d.duration) stats.push({ k: "DURATION", v: fmt(d.duration), s: "Full length" });
  if (d.dynamic_range != null) stats.push({ k: "DYN RANGE", v: Number(d.dynamic_range).toFixed(1), s: drLabel(d.dynamic_range) });
  if (d.tempo_stability != null) {
    const pct = d.tempo_stability > 1 ? d.tempo_stability : d.tempo_stability * 100;
    stats.push({ k: "STABILITY", v: Math.round(pct) + "%", s: stabilityLabel(pct) });
  }
  const sp = d.stem_presence || {};
  const presence = laneList
    .filter((l) => sp[l.name] != null)
    .map((l) => ({ name: l.label, color: l.color, val: Math.round(sp[l.name]) }));
  return { stats, presence };
}

function onEngineTime(t) {
  const dur = curDuration() || 1;
  state.progress = Math.max(0, Math.min(1, t / dur));
  const head = app.querySelector(".playhead");
  const cur = app.querySelector(".wave-times .cur");
  if (head) head.style.left = state.progress * 100 + "%";
  if (cur) cur.textContent = fmt(t);
}

// Load a track: fetch detail, build lanes, spin up the Web Audio engine.
async function openTrack(card, { autoplay = false } = {}) {
  state.tab = "mixer";
  state.current = { ...card, detail: null, loading: true, error: null };
  state.playing = false;
  state.progress = 0;
  render();

  if (engine) { engine.destroy(); engine = null; engineTrackId = null; }
  engineReady = false;
  const token = ++engineToken;
  pendingPlay = autoplay;

  let detail;
  try {
    const res = await fetch(`/api/jobs/${card.id}`, { cache: "no-store" });
    if (!res.ok) throw new Error(`GET /api/jobs/${card.id} -> ${res.status}`);
    detail = await res.json();
  } catch (e) {
    if (token !== engineToken) return;
    console.warn("[mobile] track detail failed:", e);
    state.current.loading = false;
    state.current.error = "Couldn't load this track.";
    render();
    return;
  }
  if (token !== engineToken) return;

  // Use the MP3 stem variant on mobile: ~10-20x smaller than WAV, so it
  // downloads in seconds and decodes in far less memory (full-song WAV stems
  // can be hundreds of MB and exhaust a phone's RAM).
  const laneList = (detail.stems || [])
    .filter((s) => s && s.name !== "original" && s.url)
    .map((s, i) => ({ name: s.name, url: s.url.replace(/\.wav(\?|$)/, ".mp3$1"), ...stemMeta(s.name, i) }));

  state.vols = {};
  state.muted = {};
  state.solo = {};
  for (const l of laneList) state.vols[l.name] = 1;

  state.current.detail = {
    duration: detail.duration || 0,
    lanes: laneList,
    hasVideo: !!detail.has_video,
    analysis: extractAnalysis(detail, laneList),
  };
  state.current.loading = false;
  state.current.stemCount = laneList.length;

  if (!laneList.length) {
    state.current.error = "This track has no playable stems yet.";
    render();
    return;
  }

  engine = createAudioEngine(laneList.map((l) => ({ name: l.name, url: l.url })), {
    onTime: onEngineTime,
    onEnded: () => { state.playing = false; render(); },
    context: ensureAudioCtx(),
  });
  engineTrackId = card.id;
  render();

  const ok = await engine.ready;
  if (token !== engineToken) return;
  if (!ok) {
    state.current.error = "Couldn't load this track's audio.";
    pendingPlay = false;
    toast("Couldn't load this track's audio.");
    render();
    return;
  }
  engineReady = true;
  applyMix();
  render();
  if (pendingPlay) { pendingPlay = false; engine.play(); state.playing = engine.isPlaying(); render(); }
}

function togglePlay() {
  if (!state.current) return;
  ensureAudioCtx(); // unlock audio within this gesture
  // Engine not loaded for this track yet → load then autoplay.
  if (!engine || engineTrackId !== state.current.id) {
    openTrack(state.current, { autoplay: true });
    return;
  }
  // Decode still in flight → remember the intent and play once ready.
  if (!engineReady) {
    pendingPlay = true;
    return;
  }
  if (engine.isPlaying()) engine.pause();
  else engine.play();
  state.playing = engine.isPlaying();
  render();
}

function seekToFraction(frac) {
  if (!engine) return;
  engine.seek(Math.max(0, Math.min(1, frac)) * curDuration());
}

function triggerDownload(url) {
  const a = document.createElement("a");
  a.href = url;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function exportMix(kind) {
  const id = state.current?.id;
  if (!id || !lanes().length) return;
  const names = [];
  const gains = [];
  for (const l of lanes()) {
    const g = effectiveGain(l.name);
    if (g > 0) { names.push(l.name); gains.push(g); }
  }
  if (!names.length) { toast("Every stem is muted — nothing to export."); return; }
  const q = new URLSearchParams({ stems: names.join(","), gains: gains.map((g) => g.toFixed(3)).join(",") });
  const path = kind === "mp4" ? `video.mp4` : `mixdown.wav`;
  triggerDownload(`/api/jobs/${id}/${path}?${q}`);
}

let _toastTimer = null;
function toast(msg) {
  let el = document.getElementById("m-toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "m-toast";
    el.className = "m-toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove("show"), 2400);
}

function mainWaveform() {
  const N = 56;
  const played = Math.round(state.progress * N);
  let bars = "";
  for (let i = 0; i < N; i++) {
    let h = 24 + Math.abs(Math.sin(i * 0.7) + Math.sin(i * 0.21) * 0.7) * 46 + ((i * 37) % 11);
    h = Math.max(14, Math.min(98, h));
    const bg = i <= played ? "#f5b417" : "#34343c";
    bars += `<i style="height:${h}%;background:${bg}"></i>`;
  }
  return bars;
}

function stemWave(idx, color, vol, eff) {
  const wn = 28;
  const wplayed = Math.round(state.progress * wn);
  let out = "";
  for (let i = 0; i < wn; i++) {
    let wh = 16 + Math.abs(Math.sin(i * (0.55 + idx * 0.13)) + Math.sin(i * 0.33 + idx) * 0.6) * 62 + ((i * (7 + idx * 3)) % 9);
    wh = Math.max(7, Math.min(98, wh)) * (0.3 + 0.7 * vol);
    const wc = eff ? "#34343c" : i <= wplayed ? color : color + "3d";
    out += `<i style="height:${wh}%;background:${wc}"></i>`;
  }
  return out;
}

function stemsBody() {
  const c = state.current;
  if (!c) return `<div class="lib-note">Pick a track from your Library to start mixing.</div>`;
  if (c.loading) return `<div class="lib-note">Loading stems…</div>`;
  if (c.error) return `<div class="lib-note">${esc(c.error)}</div>`;
  const ll = lanes();
  if (!ll.length) return `<div class="lib-note">No stems available.</div>`;
  return `<div class="stems-grid">${ll.map((l, idx) => {
    const vol = state.vols[l.name] ?? 1;
    const muted = !!state.muted[l.name];
    const soloed = !!state.solo[l.name];
    const eff = !laneActive(l.name);
    const pct = Math.round(vol * 100) + "%";
    const mOn = muted ? `background:${l.color};color:#15100a;border-color:${l.color};` : "";
    const sOn = soloed ? "background:#f5b417;color:#1a1206;border-color:#f5b417;" : "";
    return `<div class="stem">
      <div class="stem-top">
        <div class="stem-dot" style="background:${l.color};box-shadow:0 0 8px ${l.color}88;opacity:${eff ? 0.4 : 1}"></div>
        <div class="stem-name" style="color:${eff ? "#6a6a72" : l.color}">${esc(l.label)}</div>
        <button class="ms-btn" style="${mOn}" data-action="mute" data-id="${l.name}">M</button>
        <button class="ms-btn" style="${sOn}" data-action="solo" data-id="${l.name}">S</button>
      </div>
      <div class="stem-wave">${stemWave(idx, l.color, vol, eff)}</div>
      <div class="fader" data-fader data-id="${l.name}">
        <div class="fader-track"></div>
        <div class="fader-fill" style="width:${pct};background:${eff ? "#46464e" : l.color}"></div>
        <div class="fader-knob" style="left:${pct};background:${eff ? "#8a8a90" : "#fff"}"></div>
      </div>
    </div>`;
  }).join("")}</div>`;
}

function analysisBody() {
  const a = state.current?.detail?.analysis;
  const hasVideo = state.current?.detail?.hasVideo;
  const statsHtml = a && a.stats.length
    ? `<div class="stats">${a.stats.map((x) => `<div class="stat"><div class="stat-k">${x.k}</div><div class="stat-v">${esc(x.v)}</div><div class="stat-s">${esc(x.s)}</div></div>`).join("")}</div>`
    : `<div class="lib-note" style="margin-top:8px">Analysis not available for this track.</div>`;
  const presenceHtml = a && a.presence.length
    ? `<div class="eyebrow">STEM PRESENCE</div>${a.presence.map((p) => `<div class="presence-row"><div class="presence-name">${esc(p.name)}</div><div class="presence-bar"><i style="width:${p.val}%;background:${p.color}"></i></div><span class="presence-val">${p.val}%</span></div>`).join("")}`
    : "";
  const exportBtns = `<button class="cta sm" data-action="export" data-kind="wav">${ICON.download}Export mix</button>${
    hasVideo ? `<button class="cta sm" data-action="export" data-kind="mp4" style="margin-top:10px">${ICON.download}Export MP4 (with video)</button>` : ""
  }`;
  return `<div class="pad" style="padding-top:16px">${statsHtml}${presenceHtml}${exportBtns}</div>`;
}

function mixerScreen() {
  const c = state.current || { title: "No track selected", sub: "Pick one from your Library", initial: "♪", gradient: DEFAULT_GRADIENT, stemCount: 0 };
  const bg = c.gradient || DEFAULT_GRADIENT;
  const sourceTag = c.sub || "—";
  const stemTag = c.stemCount ? `${c.stemCount} stems` : "";
  const dur = curDuration();
  const body = state.mixerView === "stems" ? stemsBody() : analysisBody();
  const ready = engineReady && engineTrackId === state.current?.id;
  const preparing = !!state.current && !state.current.loading && !state.current.error && !ready;
  const canPlay = ready;

  return `<div class="screen scrl">
    <div class="pad">
      <div class="mx-head">
        <button class="icon-btn" data-action="tab" data-tab="library">${ICON.chevron}</button>
        <span class="now-playing">NOW PLAYING</span>
        <button class="icon-btn">${ICON.dots}</button>
      </div>
      <div class="cover-wrap">
        <div class="cover" style="background:${bg}"><span>${esc(c.initial)}</span></div>
        <div class="track-title">${esc(c.title)}</div>
        <div class="track-sub">${esc(c.sub)}</div>
        <div class="tags"><span class="tag">${esc(sourceTag)}</span>${stemTag ? `<span class="tag">${stemTag}</span>` : ""}</div>
      </div>
      <div class="wave">
        <div class="wave-bars" data-seek>${mainWaveform()}<div class="playhead" style="left:${state.progress * 100}%"></div></div>
        <div class="wave-times"><span class="cur">${fmt(state.progress * dur)}</span><span class="dur">${fmt(dur)}</span></div>
      </div>
      <div class="transport">
        <button class="t-ghost">${ICON.clock}.75x</button>
        <button class="t-step">${ICON.prev}</button>
        <button class="t-play" data-action="play" ${canPlay ? "" : "disabled style=opacity:.45"}>${state.playing ? ICON.pause(26, "#1a1206") : ICON.play(28, "#1a1206")}</button>
        <button class="t-step">${ICON.next}</button>
        <button class="t-ghost" style="color:#85858d">${ICON.loop}</button>
      </div>
      ${preparing ? '<div class="mx-prep">Preparing audio…</div>' : ""}
      <div class="segmented">
        <button class="${state.mixerView === "stems" ? "on" : ""}" data-action="mixview" data-view="stems">Stems</button>
        <button class="${state.mixerView === "analysis" ? "on" : ""}" data-action="mixview" data-view="analysis">Analysis</button>
      </div>
    </div>
    ${body}
  </div>`;
}

function libraryBody() {
  if (state.libState === "loading") {
    return `<div class="lib-note">Loading your library…</div>`;
  }
  if (state.libState === "error") {
    return `<div class="lib-note">Couldn't reach the server. <button class="lib-retry" data-action="reload">Retry</button></div>`;
  }
  if (state.libState === "empty") {
    return `<div class="lib-note">No tracks yet. Head to <b>Extract</b> to split your first song.</div>`;
  }
  return `<div class="eyebrow">RECENT</div>
    ${state.tracks.map((t) => `<div class="track-wrap${state.swipedTrackId === t.id ? " swiped" : ""}">
      <button class="track-delete" data-action="delete" data-id="${esc(t.id)}">Delete</button>
      <div class="track" data-action="open" data-id="${esc(t.id)}">
        <div class="track-art" style="background:${t.gradient}">${esc(t.initial)}</div>
        <div class="track-info"><div class="t">${esc(t.title)}</div><div class="s">${esc(t.sub)}</div><div class="m">${esc(t.meta)}</div></div>
        <div class="track-dot ${t.status}"></div>
        <button class="track-load" data-action="open" data-id="${esc(t.id)}">Load</button>
      </div>
    </div>`).join("")}`;
}

function libraryScreen() {
  return `<div class="screen scrl">
    <div class="pad">
      <div class="lib-head">
        <span class="h1">Library</span>
        <div class="avatar">JS</div>
      </div>
      <div class="search">${ICON.search}<span>Search your library</span></div>
      <div class="filters scrl">${FILTERS.map((f) => `<span class="filter ${f === state.filter ? "on" : ""}" data-action="filter" data-filter="${f}">${f}</span>`).join("")}</div>
      ${libraryBody()}
    </div>
  </div>`;
}

function extractProgressCard() {
  const j = state.extractJob;
  if (!j) return "";
  const pct = Math.round((j.progress || 0) * 100);
  const failed = j.status === "error";
  const cancelled = j.status === "cancelled";
  const done = j.status === "done";
  const line = failed ? "Failed" : cancelled ? "Cancelled" : done ? "Done" : `${j.stage || j.status}…`;
  return `<div class="eyebrow">IN PROGRESS</div>
    <div class="progress-card">
      <div class="progress-top">
        <div class="progress-art"></div>
        <div style="flex:1;min-width:0">
          <div style="font-size:14px;font-weight:600;color:#eaeaec;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(j.title || "Track")}</div>
          <div style="font-size:12px;color:${failed ? "#f0506e" : "#7a7a82"};margin-top:1px">${esc(line)}</div>
        </div>
        <span class="progress-pct">${done ? "100" : pct}%</span>
      </div>
      <div class="progress-bar"><i style="width:${done ? 100 : pct}%"></i></div>
    </div>`;
}

function extractScreen() {
  const fileName = state.extractFile?.name;
  return `<div class="screen scrl">
    <div class="pad">
      <div class="h1">Extract stems</div>
      <div class="sub">Paste a link or upload audio to split into stems.</div>
      <div class="paste">${ICON.link}<input id="ext-url" class="ext-input" type="url" inputmode="url" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Paste YouTube or audio URL" value="${esc(state.extractUrl || "")}"></div>
      <button class="upload" data-action="pick-file">${ICON.upload}${fileName ? esc(fileName) : "Upload audio file"}</button>
      <input id="ext-file" type="file" accept="audio/*,video/mp4,.mp3,.wav,.flac,.m4a,.ogg" style="display:none">
      <div class="eyebrow">STEMS TO EXTRACT</div>
      <div class="chips">${EXTRACT_STEMS.map((s) => {
        const on = !!state.selected[s.id];
        const onStyle = on ? `border-color:${s.color};background:${s.color}1c;` : "";
        return `<button class="chip-btn ${on ? "on" : ""}" style="${onStyle}" data-action="chip" data-id="${s.id}"><div class="dot" style="background:${s.color}"></div><span class="nm">${s.name}</span>${on ? ICON.check : ""}</button>`;
      }).join("")}</div>
      <button class="cta" style="margin-top:22px" data-action="split">${ICON.scissors}Split stems</button>
      ${extractProgressCard()}
    </div>
  </div>`;
}

// Submit an extraction (URL or file) and follow it to completion.
async function startExtraction() {
  const stems = Object.keys(state.selected).filter((k) => state.selected[k]);
  if (!stems.length) { toast("Pick at least one stem."); return; }
  const file = state.extractFile;
  const url = (state.extractUrl || "").trim();
  if (!file && !url) { toast("Paste a URL or choose a file first."); return; }

  let init, title;
  if (file) {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("stems", JSON.stringify(stems));
    init = { method: "POST", body: fd };
    title = file.name;
  } else {
    init = { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url, stems }) };
    title = url;
  }

  state.extractJob = { id: null, title, status: "queued", progress: 0, stage: "Queued" };
  render();

  let jobId;
  try {
    const res = await fetch("/api/jobs", init);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || res.statusText);
    jobId = data.job_id;
  } catch (e) {
    console.warn("[mobile] extraction submit failed:", e);
    state.extractJob = null;
    toast(`Couldn't start: ${e.message}`);
    render();
    return;
  }

  state.extractJob.id = jobId;
  state.extractFile = null;
  state.extractUrl = "";
  followExtraction(jobId);
  render();
}

function _onExtractState(jobId, s) {
  if (!state.extractJob || state.extractJob.id !== jobId) return false;
  state.extractJob.status = s.status;
  state.extractJob.progress = s.progress || 0;
  state.extractJob.stage = s.stage || s.status;
  if (state.tab === "extract") render();
  if (s.status === "done" || s.status === "error" || s.status === "cancelled") {
    if (s.status === "done") { toast("Stems ready!"); loadLibrary(); }
    else if (s.status === "error") toast("Extraction failed.");
    const finishedId = jobId;
    setTimeout(() => {
      if (state.extractJob && state.extractJob.id === finishedId) {
        state.extractJob = null;
        if (state.tab === "extract") render();
      }
    }, 4000);
    return true; // terminal
  }
  return false;
}

// SSE with a REST-poll fallback (mirrors the desktop's resilience, simplified).
function followExtraction(jobId) {
  if (extractES) { extractES.close(); extractES = null; }
  if (extractPoll) { clearInterval(extractPoll); extractPoll = null; }

  const startPolling = () => {
    if (extractPoll) return;
    extractPoll = setInterval(async () => {
      try {
        const r = await fetch(`/api/jobs/${jobId}`, { cache: "no-store" });
        if (!r.ok) throw new Error(String(r.status));
        if (_onExtractState(jobId, await r.json())) { clearInterval(extractPoll); extractPoll = null; }
      } catch { /* keep polling */ }
    }, 2500);
  };

  try {
    const es = new EventSource(`/api/jobs/${jobId}/events`);
    extractES = es;
    es.onmessage = (ev) => {
      let s;
      try { s = JSON.parse(ev.data); } catch { return; }
      if (_onExtractState(jobId, s)) { es.close(); extractES = null; }
    };
    es.onerror = () => { es.close(); extractES = null; startPolling(); };
  } catch {
    startPolling();
  }
}

function miniPlayer() {
  if (state.tab === "mixer" || !state.current) return "";
  const c = state.current;
  const bg = c.gradient || DEFAULT_GRADIENT;
  return `<div class="mini" data-action="tab" data-tab="mixer">
    <div class="mini-art" style="background:${bg}">${esc(c.initial)}</div>
    <div class="mini-info"><div class="t">${esc(c.title)}</div><div class="s">${esc(c.sub)}</div></div>
    <button class="mini-play" data-action="play-mini">${state.playing ? ICON.pause(17, "#1a1206") : ICON.play(18, "#1a1206")}</button>
  </div>`;
}

function tabBar() {
  const t = (tab, icon, label) => `<button class="tab ${state.tab === tab ? "on" : ""}" data-action="tab" data-tab="${tab}">${icon}${label}</button>`;
  return `<div class="tabbar">${t("extract", ICON.tabExt, "Extract")}${t("mixer", ICON.tabMix, "Mixer")}${t("library", ICON.tabLib, "Library")}</div>`;
}

function render() {
  let screen = mixerScreen();
  if (state.tab === "library") screen = libraryScreen();
  else if (state.tab === "extract") screen = extractScreen();
  app.innerHTML = screen + miniPlayer() + tabBar();
  wireFaders();
  wireSwipe();
}

// Suppresses the synthetic click that follows a swipe gesture, so swiping a
// row open doesn't immediately count as a tap.
let suppressClick = false;

// Swipe a library row left to reveal its Delete button (iOS-style). Wired
// imperatively after each render.
function wireSwipe() {
  app.querySelectorAll(".track-wrap .track").forEach((row) => {
    const id = row.dataset.id;
    let x0 = 0, y0 = 0, active = false, horizontal = false;
    const base = () => (state.swipedTrackId === id ? -84 : 0);

    row.addEventListener("pointerdown", (e) => {
      x0 = e.clientX; y0 = e.clientY; active = true; horizontal = false;
      row.style.transition = "none";
    });
    row.addEventListener("pointermove", (e) => {
      if (!active) return;
      const dx = e.clientX - x0;
      const dy = e.clientY - y0;
      if (!horizontal && Math.abs(dx) > 8 && Math.abs(dx) > Math.abs(dy)) horizontal = true;
      if (horizontal) {
        e.preventDefault();
        const tx = Math.max(-84, Math.min(0, base() + dx));
        row.style.transform = `translateX(${tx}px)`;
      }
    });
    const end = (e) => {
      if (!active) return;
      active = false;
      if (!horizontal) { row.style.transition = ""; row.style.transform = ""; return; }
      suppressClick = true;
      setTimeout(() => { suppressClick = false; }, 60);
      const dx = e.clientX - x0;
      const open = dx < -40 ? true : dx > 40 ? false : state.swipedTrackId === id;
      // Animate to the snap position by toggling the class (no DOM rebuild, so
      // it transitions smoothly from wherever the finger let go).
      closeOtherSwipes(row.closest(".track-wrap"));
      row.style.transition = "transform 0.22s cubic-bezier(0.22,1,0.36,1)";
      row.closest(".track-wrap").classList.toggle("swiped", open);
      row.style.transform = "";
      state.swipedTrackId = open ? id : null;
    };
    row.addEventListener("pointerup", end);
    row.addEventListener("pointercancel", end);
  });
}

// Smoothly close any revealed row (optionally except `keep`), without a full
// re-render so the close animates.
function closeOtherSwipes(keep) {
  app.querySelectorAll(".track-wrap.swiped").forEach((w) => {
    if (w === keep) return;
    const r = w.querySelector(".track");
    if (r) { r.style.transition = "transform 0.22s cubic-bezier(0.22,1,0.36,1)"; r.style.transform = ""; }
    w.classList.remove("swiped");
  });
}

function closeSwipe() {
  state.swipedTrackId = null;
  closeOtherSwipes(null);
}

async function deleteTrack(id) {
  state.swipedTrackId = null;
  try {
    await fetch(`/api/jobs/${id}`, { method: "DELETE" });
  } catch (e) {
    console.warn("[mobile] delete failed:", e);
  }
  state.tracks = state.tracks.filter((t) => t.id !== id);
  if (!state.tracks.length) state.libState = "empty";
  if (state.current?.id === id) {
    if (engine) { engine.destroy(); engine = null; engineTrackId = null; engineReady = false; }
    state.current = null;
    state.playing = false;
  }
  toast("Track deleted");
  render();
}

// Faders + waveform seek need live pointer drag, so they're wired imperatively
// after each render (the rest of the UI uses click delegation below).
function wireFaders() {
  app.querySelectorAll("[data-fader]").forEach((el) => {
    el.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      const id = el.dataset.id;
      const fill = el.querySelector(".fader-fill");
      const knob = el.querySelector(".fader-knob");
      const rect = el.getBoundingClientRect();
      const set = (cx) => {
        let v = (cx - rect.left) / rect.width;
        v = Math.max(0, Math.min(1, v));
        state.vols[id] = v;
        const pct = Math.round(v * 100) + "%";
        fill.style.width = pct;
        knob.style.left = pct;
        if (engine && laneActive(id)) engine.setGain(id, v);
      };
      set(e.clientX);
      el.setPointerCapture(e.pointerId);
      const mv = (ev) => set(ev.clientX);
      const up = () => {
        el.removeEventListener("pointermove", mv);
        el.removeEventListener("pointerup", up);
      };
      el.addEventListener("pointermove", mv);
      el.addEventListener("pointerup", up);
    });
  });

  const bars = app.querySelector("[data-seek]");
  if (bars) {
    bars.addEventListener("pointerdown", (e) => {
      if (!engine || !curDuration()) return;
      e.preventDefault();
      const rect = bars.getBoundingClientRect();
      const head = bars.querySelector(".playhead");
      const seek = (cx) => {
        const frac = Math.max(0, Math.min(1, (cx - rect.left) / rect.width));
        if (head) head.style.left = frac * 100 + "%";
        const cur = app.querySelector(".wave-times .cur");
        if (cur) cur.textContent = fmt(frac * curDuration());
        seekToFraction(frac);
      };
      seek(e.clientX);
      bars.setPointerCapture(e.pointerId);
      const mv = (ev) => seek(ev.clientX);
      const up = () => {
        bars.removeEventListener("pointermove", mv);
        bars.removeEventListener("pointerup", up);
      };
      bars.addEventListener("pointermove", mv);
      bars.addEventListener("pointerup", up);
    });
  }
}

app.addEventListener("click", (e) => {
  if (suppressClick) { suppressClick = false; return; }
  const t = e.target.closest("[data-action]");
  if (!t) return;
  ensureAudioCtx(); // any in-app tap unlocks audio on mobile
  const a = t.dataset.action;
  // A row is swiped open: any tap other than its Delete just closes it.
  if (state.swipedTrackId && a !== "delete") {
    closeSwipe();
    return;
  }
  switch (a) {
    case "delete":
      deleteTrack(t.dataset.id);
      return;
    case "tab":
      state.tab = t.dataset.tab;
      break;
    case "mixview":
      state.mixerView = t.dataset.view;
      break;
    case "play":
    case "play-mini":
      e.stopPropagation();
      togglePlay();
      return;
    case "mute":
      state.muted[t.dataset.id] = !state.muted[t.dataset.id];
      applyMix();
      break;
    case "solo":
      state.solo[t.dataset.id] = !state.solo[t.dataset.id];
      applyMix();
      break;
    case "chip":
      state.selected[t.dataset.id] = !state.selected[t.dataset.id];
      break;
    case "qual":
      state.quality = t.dataset.q;
      break;
    case "filter":
      state.filter = t.dataset.filter;
      break;
    case "export":
      exportMix(t.dataset.kind || "wav");
      return;
    case "split":
      startExtraction();
      return;
    case "pick-file":
      document.getElementById("ext-file")?.click();
      return;
    case "open": {
      const track = state.tracks.find((x) => x.id === t.dataset.id);
      if (track) openTrack(track);
      return;
    }
    case "reload":
      loadLibrary();
      return;
    default:
      return;
  }
  render();
});

// URL field is uncontrolled-but-tracked: store keystrokes in state without
// re-rendering (which would steal focus), so the value survives later renders.
app.addEventListener("input", (e) => {
  if (e.target.id === "ext-url") state.extractUrl = e.target.value;
});
app.addEventListener("change", (e) => {
  if (e.target.id === "ext-file") {
    state.extractFile = e.target.files?.[0] || null;
    render();
  }
});

// Load the real library from /api/jobs (newest first). On success, seed the
// Mixer with the most recent track if nothing is selected yet.
async function loadLibrary() {
  state.libState = "loading";
  render();
  try {
    const jobs = await fetchJobs();
    state.tracks = jobs.map(jobToCard).sort((a, b) => b.createdAt - a.createdAt);
    state.libState = state.tracks.length ? "ready" : "empty";
    if (!state.current && state.tracks.length) state.current = state.tracks[0];
  } catch (e) {
    console.warn("[mobile] failed to load library:", e);
    state.libState = "error";
  }
  render();
}

render();
loadLibrary();
