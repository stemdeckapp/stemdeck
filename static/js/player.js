import Multitrack from "/vendor/multitrack.js";
import { fmtTime } from "./utils.js";
import {
  STEM_NAMES, TRACK_NAMES, STEM_COLORS, PROGRESS_COLOR,
  LOOP_DEFAULT_START_FRAC, LOOP_DEFAULT_END_FRAC,
} from "./constants.js";
import {
  mixerEl, multitrackContainer, bpmChip, keyChip, stemsChip, timeEl,
  titleEl, npThumb, rulerTime, wavesGrid, playBtn,
  stopBtn, loopBtn, loopRegionEl,
  multitrack, currentJobId, trackIndex, totalDuration, loopEnabled,
  loopStart, loopEnd, trackAnalysers,
  masterVolume, masterFader, mixerState,
  setMultitrack, setCurrentJobId, setTrackIndex, setTotalDuration,
  setLoopEnabled, setLoopStart, setLoopEnd, setMasterVolume,
  setWaveZoom, waveScroll, selectedStems,
} from "./state.js";
import {
  loadMixIntoState, resetMixerState, refreshMixerVisuals,
  setLaneControlsEnabled, ensureMixerStateDefaults, applyMix,
  renderRealMiniWave,
} from "./mixer.js";
import { renderMixerRow } from "./mixer.js";
import {
  buildRuler, updatePlayheadMarker, updateLoopRegionVisual,
  applyWaveZoom, buildPresenceRuler, updateFooterTimes,
  updatePresencePlayhead,
} from "./transport.js";
import { stopVuLoop } from "./audio.js";

// Stem-selection filter: the import-page stem-choice toggles set
// selectedStems (state.js). Backend always processes all 6 -- we
// hide the rows for unselected stems in the studio dashboard so the
// user "only sees what they selected to extract".
const _STEM_ROW_SELECTORS = [
  ".stem-list span[data-stem]",
  ".presence-bars i[data-stem]",
  ".presence-labels span",
  ".stem-waveform-row[data-stem]",
];

function applyStemSelectionFilter(presentNames) {
  const visibleTrackCount = Math.max(1, presentNames.size || TRACK_NAMES.length);
  const app = document.querySelector(".app");
  app?.style.setProperty("--visible-track-count", String(visibleTrackCount));
  app?.style.setProperty(
    "--wave-widget-track-stack-h",
    `${(visibleTrackCount * WAVEFORM_LANE_HEIGHT) + ((visibleTrackCount - 1) * WAVEFORM_SEPARATOR_HEIGHT)}px`,
  );
  for (const sel of _STEM_ROW_SELECTORS) {
    for (const el of document.querySelectorAll(sel)) {
      const stem = el.dataset.stem
        || el.classList[0];  // .presence-labels span has no data-stem, use class
      el.classList.toggle("hidden", !presentNames.has(stem));
    }
  }
  const visibleMixerNames = [];
  if (presentNames.has("original")) visibleMixerNames.push("original");
  for (const name of STEM_NAMES) {
    if (presentNames.has(name)) visibleMixerNames.push(name);
  }
  for (const name of STEM_NAMES) {
    if (visibleMixerNames.length >= STEM_NAMES.length) break;
    if (!visibleMixerNames.includes(name)) visibleMixerNames.push(name);
  }
  const visibleMixerSet = new Set(visibleMixerNames);

  for (const row of document.querySelectorAll(".mixer-column .lane-header[data-stem]")) {
    const stem = row.dataset.stem;
    const available = presentNames.has(stem);
    row.classList.toggle("hidden", !visibleMixerSet.has(stem));
    row.classList.toggle("unavailable", !available);
    row.setAttribute("aria-disabled", String(!available));
    for (const el of row.querySelectorAll("button, .lane-knob, .lane-dl")) {
      el.classList.toggle("disabled", !available);
      if (available) {
        el.removeAttribute("aria-disabled");
        if (el.matches(".lane-knob")) el.setAttribute("tabindex", "0");
        else el.removeAttribute("tabindex");
      } else {
        el.setAttribute("aria-disabled", "true");
        el.setAttribute("tabindex", "-1");
      }
      if ("disabled" in el) el.disabled = !available;
    }
  }
  for (const row of document.querySelectorAll(".energy-row[data-stem]")) {
    const available = presentNames.has(row.dataset.stem);
    row.classList.toggle("unavailable", !available);
    row.classList.remove("hidden");
  }
}

function clearStemSelectionFilter() {
  const app = document.querySelector(".app");
  app?.style.setProperty("--visible-track-count", String(TRACK_NAMES.length));
  app?.style.setProperty(
    "--wave-widget-track-stack-h",
    `${(TRACK_NAMES.length * WAVEFORM_LANE_HEIGHT) + ((TRACK_NAMES.length - 1) * WAVEFORM_SEPARATOR_HEIGHT)}px`,
  );
  for (const sel of _STEM_ROW_SELECTORS) {
    for (const el of document.querySelectorAll(sel)) {
      el.classList.remove("hidden");
    }
  }
  for (const row of document.querySelectorAll(".mixer-column .lane-header[data-stem], .energy-row[data-stem]")) {
    row.classList.remove("hidden");
    row.classList.remove("unavailable");
    row.removeAttribute("aria-disabled");
  }
}

// Reset the analysis cards (key, scale, confidence ring, loudness)
// between songs so a re-import doesn't flash the previous song's
// numbers before the new ones arrive via SSE.
function resetAnalysisCards() {
  const summaryKey = document.getElementById("summary-key");
  const summaryBpm = document.getElementById("summary-bpm");
  const summaryScale = document.getElementById("summary-scale");
  const summaryConfidence = document.getElementById("summary-confidence");
  const summaryConfidenceLabel = document.getElementById("summary-confidence-label");
  const loudnessCard = document.getElementById("loudness-card");
  if (summaryKey) summaryKey.textContent = "—";
  if (summaryBpm) summaryBpm.innerHTML = "— <small>BPM</small>";
  if (summaryScale) summaryScale.textContent = "";
  if (summaryConfidence) {
    summaryConfidence.textContent = "";
    summaryConfidence.style.removeProperty("--confidence-pct");
    summaryConfidence.classList.add("hidden");
  }
  if (summaryConfidenceLabel) summaryConfidenceLabel.classList.add("hidden");
  if (loudnessCard) loudnessCard.classList.add("hidden");
}

function renderPlaceholderTracks() {
  multitrackContainer.innerHTML = "";
  for (const name of TRACK_NAMES) {
    const ph = document.createElement("div");
    ph.className = "lane-placeholder";
    ph.dataset.stem = name;
    ph.style.setProperty("--lane-color", STEM_COLORS[name] || "#a0a0a0");
    multitrackContainer.appendChild(ph);
  }
}

const OVERVIEW_WAVE_POINTS = 1500;
const STEM_VU_FPS = 30;
const WAVEFORM_LANE_HEIGHT = 64;
const WAVEFORM_SEPARATOR_HEIGHT = 2;
let visualRenderToken = 0;
let visualAudioContext = null;
let stemVuRafId = null;

function isAudioBufferLike(value) {
  return value && typeof value.getChannelData === "function";
}

function clearOverviewWaveforms() {
  document.querySelector(".stem-waveform-layer")?.remove();
}

function resetStemMeters() {
  for (const meter of document.querySelectorAll(".mini-meter")) {
    meter.style.setProperty("--vu-scale", "0");
    meter.style.setProperty("--vu-peak-pct", "0");
    meter.style.setProperty("--vu-peak-opacity", "0");
  }
  for (const laneVu of mixerEl.querySelectorAll(".lane-vu")) {
    laneVu.style.setProperty("--vu-level", "0%");
    laneVu.style.setProperty("--vu-peak", "0%");
  }
}

function stopStemVuLoop() {
  if (stemVuRafId) {
    cancelAnimationFrame(stemVuRafId);
    stemVuRafId = null;
  }
  resetStemMeters();
}

function ensureOverviewWaveformLayer() {
  let layer = document.querySelector(".stem-waveform-layer");
  if (!layer) {
    layer = document.createElement("div");
    layer.className = "stem-waveform-layer";
    multitrackContainer.parentElement?.appendChild(layer);
  }
  return layer;
}

// Standard DAW-style waveform: track min and max raw sample values per
// pixel column. The signed peaks let us render the natural mirror-
// symmetric shape (top edge follows max, bottom follows min) and keeps
// transient detail that an RMS envelope would smooth away.
function bufferMinMaxPeaks(audioBuffer, count) {
  const ch = audioBuffer.getChannelData(0);
  const binSize = Math.max(1, Math.floor(ch.length / count));
  const peaks = new Array(count);
  for (let i = 0; i < count; i++) {
    const start = i * binSize;
    const end = i === count - 1 ? ch.length : Math.min(ch.length, start + binSize);
    let mn = 0;
    let mx = 0;
    for (let j = start; j < end; j++) {
      const v = ch[j];
      if (v > mx) mx = v;
      else if (v < mn) mn = v;
    }
    peaks[i] = [mn, mx];
  }
  return peaks;
}

function minMaxWaveformPath(peaks, norm) {
  const n = peaks.length;
  const top = new Array(n);
  const bottom = new Array(n);
  for (let i = 0; i < n; i++) {
    const x = ((i / (n - 1)) * 100).toFixed(3);
    const mx = Math.min(1, peaks[i][1] * norm);
    const mn = Math.max(-1, peaks[i][0] * norm);
    top[i] = `${i === 0 ? "M" : "L"}${x} ${(24 - mx * 21).toFixed(3)}`;
    bottom[n - 1 - i] = `L${x} ${(24 - mn * 21).toFixed(3)}`;
  }
  return `${top.join(" ")} ${bottom.join(" ")} Z`;
}

// Mixer-column mini-wave keeps a per-stem normalized envelope (each
// thumbnail fills its own little box). Used by mixer.js indirectly via
// renderRealMiniWave, which has its own peak computation.
function bufferPeaks(audioBuffer, count) {
  const peaks = bufferMinMaxPeaks(audioBuffer, count);
  let max = 0;
  for (const [mn, mx] of peaks) {
    if (mx > max) max = mx;
    if (-mn > max) max = -mn;
  }
  const norm = max > 0 ? 1 / max : 0;
  return peaks.map(([mn, mx]) => Math.max(Math.min(1, mx * norm), -mn * norm));
}

function waveformPath(peaks) {
  const top = peaks.map((amp, i) => {
    const x = (i / (peaks.length - 1)) * 100;
    const y = 24 - amp * 21;
    return `${i === 0 ? "M" : "L"}${x.toFixed(3)} ${y.toFixed(3)}`;
  });
  const bottom = [...peaks].reverse().map((amp, i) => {
    const x = ((peaks.length - 1 - i) / (peaks.length - 1)) * 100;
    const y = 24 + amp * 21;
    return `L${x.toFixed(3)} ${y.toFixed(3)}`;
  });
  return `${top.join(" ")} ${bottom.join(" ")} Z`;
}

function renderOverviewWaveformPath(stemName, peaks, norm, color) {
  const layer = ensureOverviewWaveformLayer();
  let row = layer.querySelector(`[data-stem="${stemName}"]`);
  if (!row) {
    row = document.createElement("div");
    row.className = "stem-waveform-row";
    row.dataset.stem = stemName;
    layer.appendChild(row);
  }
  row.style.setProperty("--stem-color", color);
  row.style.order = String(TRACK_NAMES.indexOf(stemName));
  row.innerHTML = `
    <svg class="stem-waveform-svg" viewBox="0 0 100 48" preserveAspectRatio="none" aria-hidden="true">
      <path d="${minMaxWaveformPath(peaks, norm)}"></path>
    </svg>
  `;
}

// Normalize all stems to a single shared max so the overview waveforms
// preserve real amplitude relationships (drums tall, piano short),
// matching what a DAW shows. Per-stem normalization made every lane
// fill its row regardless of how loud the stem actually was.
function renderAllOverviewWaveforms(stems, decodedMap) {
  const peaksByStem = new Map();
  let globalMax = 0;
  for (const stem of stems) {
    const buf = decodedMap.get(stem.name);
    if (!isAudioBufferLike(buf)) continue;
    const peaks = bufferMinMaxPeaks(buf, OVERVIEW_WAVE_POINTS);
    peaksByStem.set(stem.name, peaks);
    for (const [mn, mx] of peaks) {
      if (mx > globalMax) globalMax = mx;
      if (-mn > globalMax) globalMax = -mn;
    }
  }
  if (globalMax <= 0) return;
  const norm = 1 / globalMax;
  for (const stem of stems) {
    const peaks = peaksByStem.get(stem.name);
    if (!peaks) continue;
    const color = STEM_COLORS[stem.name] || "#a0a0a0";
    renderOverviewWaveformPath(stem.name, peaks, norm, color);
  }
}

function renderDecodedStemVisuals(stemName, audioBuffer, color) {
  if (!isAudioBufferLike(audioBuffer)) return;
  renderRealMiniWave(stemName, audioBuffer, color);
}

// Set the song-level "Stem Energy" panel from each stem's overall RMS.
// Without this baseline the bars sit at 0% until the user hits play
// (because audio.js only writes per-frame during active playback) and
// look like static placeholders. Normalizing all stems to the loudest
// one's RMS gives a meaningful relative balance ("drums dominate, piano
// quiet"), which is what a DAW-style energy panel is supposed to show.
// Once playback starts, audio.js's per-frame writes override these
// baseline values for real-time pulsing.
function renderStemEnergyBaseline(stems, decodedMap) {
  const rmsByStem = new Map();
  let maxRms = 0;
  for (const stem of stems) {
    const buf = decodedMap.get(stem.name);
    if (!isAudioBufferLike(buf)) continue;
    const ch = buf.getChannelData(0);
    if (!ch?.length) continue;
    let sum = 0;
    for (let i = 0; i < ch.length; i++) sum += ch[i] * ch[i];
    const rms = Math.sqrt(sum / ch.length);
    rmsByStem.set(stem.name, rms);
    if (rms > maxRms) maxRms = rms;
  }
  if (maxRms <= 0) return;
  for (const [name, rms] of rmsByStem) {
    const pct = Math.round((rms / maxRms) * 100);
    const row = document.querySelector(`.energy-row[data-stem="${name}"]`);
    if (!row) continue;
    const bar = row.querySelector("b");
    const txt = row.querySelector("em");
    if (bar) bar.style.setProperty("--v", `${pct}%`);
    if (txt) txt.textContent = `${pct}%`;
  }
}

function buildStemVuEnvelope(audioBuffer) {
  if (!isAudioBufferLike(audioBuffer)) return [];
  const ch = audioBuffer.getChannelData(0);
  const sampleRate = audioBuffer.sampleRate || 44100;
  const duration = audioBuffer.duration || (ch.length / sampleRate);
  const frameCount = Math.max(1, Math.ceil(duration * STEM_VU_FPS));
  const hop = Math.max(1, Math.floor(sampleRate / STEM_VU_FPS));
  const win = Math.max(1, Math.floor(sampleRate * 0.045));
  const env = new Float32Array(frameCount);
  let max = 0;
  for (let i = 0; i < frameCount; i++) {
    const center = Math.min(ch.length - 1, i * hop);
    const start = Math.max(0, center - Math.floor(win / 2));
    const end = Math.min(ch.length, start + win);
    let sum = 0;
    let peak = 0;
    for (let j = start; j < end; j++) {
      const v = Math.abs(ch[j]);
      sum += v * v;
      if (v > peak) peak = v;
    }
    const rms = Math.sqrt(sum / Math.max(1, end - start));
    const level = rms * 0.78 + peak * 0.22;
    env[i] = level;
    if (level > max) max = level;
  }
  if (max <= 0) return env;
  for (let i = 0; i < env.length; i++) {
    env[i] = Math.min(1, Math.sqrt(env[i] / max));
  }
  return env;
}

function stemVuGain(stemName) {
  const state = mixerState[stemName];
  if (!state) return 0;
  const anySolo = TRACK_NAMES.some((name) => trackIndex[name] !== undefined && mixerState[name]?.soloed);
  if (state.muted || (anySolo && !state.soloed)) return 0;
  return Math.max(0, state.volume);
}

function startStemVuLoop(stems, decodedMap, token) {
  stopStemVuLoop();
  const meters = stems.map((stem) => ({
    name: stem.name,
    env: buildStemVuEnvelope(decodedMap.get(stem.name)),
    miniMeterEl: document.querySelector(`.stem-list [data-stem="${stem.name}"] .mini-meter`),
    vuEl: mixerEl.querySelector(`.lane-vu[data-stem="${stem.name}"]`),
    peak: 0,
    peakHold: 0,
    holdFrames: 0,
    lastPeakPct: -1,
    lastHoldPct: -1,
    lastLevelPct: -1,
  })).filter((m) => m.env.length && (m.miniMeterEl || m.vuEl));

  if (!meters.length) return;
  const tick = () => {
    if (token !== visualRenderToken || !multitrack) return;
    const playing = multitrack.isPlaying?.() ?? false;
    const time = multitrack.getCurrentTime?.() ?? 0;
    for (const m of meters) {
      const idx = Math.max(0, Math.min(m.env.length - 1, Math.floor(time * STEM_VU_FPS)));
      const gain = stemVuGain(m.name);
      const input = playing && gain > 0 ? Math.min(1, m.env[idx] * gain) : 0;
      if (gain <= 0) {
        m.peak = 0;
        m.peakHold = 0;
        m.holdFrames = 0;
      }
      const nextPeak = input > m.peak ? input : Math.max(0, m.peak - 0.018);
      m.peak = nextPeak;

      if (input > m.peakHold) {
        m.peakHold = input;
        m.holdFrames = 28;
      } else if (m.holdFrames > 0) {
        m.holdFrames -= 1;
      } else {
        m.peakHold = Math.max(0, m.peakHold - 0.025);
      }

      const lvlPct = Math.round(input * 100);
      const peakPct = Math.round(nextPeak * 100);
      const holdPct = Math.round(m.peakHold * 100);

      if (m.miniMeterEl) {
        if (peakPct !== m.lastPeakPct) {
          m.miniMeterEl.style.setProperty("--vu-scale", nextPeak.toFixed(3));
        }
        if (holdPct !== m.lastHoldPct) {
          m.miniMeterEl.style.setProperty("--vu-peak-pct", String(holdPct));
          m.miniMeterEl.style.setProperty("--vu-peak-opacity", m.peakHold > 0.04 ? "1" : "0");
        }
      }
      if (m.vuEl) {
        if (lvlPct !== m.lastLevelPct) m.vuEl.style.setProperty("--vu-level", `${lvlPct}%`);
        if (holdPct !== m.lastHoldPct) m.vuEl.style.setProperty("--vu-peak", `${holdPct}%`);
      }
      m.lastLevelPct = lvlPct;
      m.lastPeakPct = peakPct;
      m.lastHoldPct = holdPct;
    }
    stemVuRafId = requestAnimationFrame(tick);
  };
  stemVuRafId = requestAnimationFrame(tick);
}

async function decodeStemForVisuals(stem) {
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtx) throw new Error("Web Audio is not available");
  visualAudioContext ??= new AudioCtx();
  const res = await fetch(stem.url, { cache: "force-cache" });
  if (!res.ok) throw new Error(`Failed to fetch ${stem.name} stem: ${res.status}`);
  const data = await res.arrayBuffer();
  return visualAudioContext.decodeAudioData(data);
}

function renderAllDecodedVisuals(stems, token) {
  clearOverviewWaveforms();
  const decoded = new Map();
  const promises = stems.map((stem) => {
    const color = STEM_COLORS[stem.name] || "#a0a0a0";
    return decodeStemForVisuals(stem)
      .then((buf) => {
        if (token !== visualRenderToken) return;
        decoded.set(stem.name, buf);
        renderDecodedStemVisuals(stem.name, buf, color);
      })
      .catch((err) => console.warn(`[visuals] ${stem.name}: ${err.message}`));
  });
  Promise.all(promises).then(() => {
    if (token !== visualRenderToken) return;
    renderAllOverviewWaveforms(stems, decoded);
    renderStemEnergyBaseline(stems, decoded);
    startStemVuLoop(stems, decoded, token);
  });
}

export function destroyPlayer() {
  document.querySelector(".app")?.classList.remove("is-import");
  document.querySelector(".app")?.classList.add("no-track");
  stopVuLoop();
  stopStemVuLoop();
  if (multitrack) {
    multitrack.destroy();
    setMultitrack(null);
  }
  renderPlaceholderTracks();
  clearOverviewWaveforms();
  for (const row of mixerEl.querySelectorAll(".lane-header")) {
    const dl = row.querySelector(".lane-dl");
    if (dl) {
      dl.href = "#";
      dl.removeAttribute("download");
    }
  }
  resetMixerState();
  refreshMixerVisuals();
  setLaneControlsEnabled(false);
  // Reset static rows, then keep the pre-import shell to extractable stems
  // only. wireUpAudio will re-apply the exact returned-track set.
  clearStemSelectionFilter();
  applyStemSelectionFilter(new Set(STEM_NAMES));
  npThumb.classList.remove("loaded");
  npThumb.removeAttribute("src");

  rulerTime.innerHTML = '<div class="playhead-marker" aria-hidden="true"><svg viewBox="0 0 10 10" width="10" height="10"><polygon points="0,0 10,0 5,8" fill="#e54e4e"></polygon></svg></div>';
  wavesGrid.innerHTML = "";

  titleEl.textContent = "";
  bpmChip.textContent = "\u2014 BPM";
  keyChip.textContent = "\u2014 \u2014";
  stemsChip.textContent = "\u2014 Stems";
  timeEl.textContent = "00:00 / 00:00";
  resetAnalysisCards();

  trackAnalysers.length = 0;
  for (const row of document.querySelectorAll(".energy-row")) {
    const bar = row.querySelector("b");
    const txt = row.querySelector("em");
    if (bar) bar.style.setProperty("--v", "0%");
    if (txt) txt.textContent = "0%";
  }
  setTotalDuration(0);
  setLoopEnabled(false);
  setLoopStart(0);
  setLoopEnd(0);
  setMasterVolume(0.5);
  setTrackIndex({});
  setWaveZoom(1);
  applyWaveZoom();
  buildPresenceRuler(0);
  updateFooterTimes(0);
  updatePresencePlayhead(0);
  if (waveScroll) waveScroll.scrollLeft = 0;
  loopBtn.classList.remove("active");
  playBtn.classList.remove("playing");
  stopBtn.classList.remove("stopped");
  loopRegionEl.classList.add("hidden");
}

export function renderEmptyShell() {
  document.querySelector(".app")?.classList.remove("is-import");
  document.querySelector(".app")?.classList.add("no-track");
  stopStemVuLoop();
  ensureMixerStateDefaults();
  mixerEl.innerHTML = "";
  for (const name of TRACK_NAMES) {
    const { row } = renderMixerRow({ name, url: "#" });
    mixerEl.appendChild(row);
  }
  applyStemSelectionFilter(new Set(STEM_NAMES));
  titleEl.textContent = "Ready to import a track";
  bpmChip.textContent = "\u2014 BPM";
  keyChip.textContent = "\u2014 \u2014";
  stemsChip.textContent = "\u2014 Stems";
  timeEl.textContent = "00:00 / 00:00";
  resetAnalysisCards();
  renderPlaceholderTracks();
  clearOverviewWaveforms();
  setLaneControlsEnabled(false);
}

function renderAllMiniWaves(mt, stems) {
  const wsArr = mt.wavesurfers || mt._wavesurfers;
  if (!wsArr?.length) return;
  stems.forEach((stem, i) => {
    const ws = wsArr[i];
    if (!ws) return;
    const color = STEM_COLORS[stem.name] || "#a0a0a0";
    const tryRender = () => {
      const buf = ws.getDecodedData?.();
      if (isAudioBufferLike(buf)) {
        renderDecodedStemVisuals(stem.name, buf, color);
        return true;
      }
      return false;
    };
    if (!tryRender()) ws.once?.("decode", tryRender);
  });
}

function setWaveformLoading(loading) {
  const el = document.getElementById("waveLoadingOverlay");
  if (!el) return;
  el.classList.toggle("hidden", !loading);
  if (!loading) el.classList.remove("stalled");
}

export function buildStripStems() {
  const container = document.getElementById("appbarStripStems");
  if (!container) return;
  container.innerHTML = "";
  for (const name of STEM_NAMES) {
    const color = STEM_COLORS[name];
    const active = selectedStems.has(name);
    const sq = document.createElement("div");
    sq.className = "strip-sq strip-sq-stem" + (active ? "" : " inactive");
    sq.dataset.stem = name;
    if (active) sq.style.cssText = `background:${color}1a;border-color:${color}44;color:${color}`;
    const srcSvg = document.querySelector(`.stem-choice[data-stem="${name}"] svg`);
    if (srcSvg) sq.appendChild(srcSvg.cloneNode(true));
    container.appendChild(sq);
  }
}

export function wireUpAudio(jobId, stems, duration, thumbnail) {
  const app = document.querySelector(".app");
  app?.classList.remove("is-import");
  app?.classList.remove("no-track");
  setWaveformLoading(true);
  stopVuLoop();
  stopStemVuLoop();
  if (multitrack) {
    multitrack.destroy();
    setMultitrack(null);
  }
  playBtn.classList.remove("playing");
  stopBtn.classList.remove("stopped");
  visualRenderToken += 1;
  const token = visualRenderToken;
  window.setTimeout(() => {
    const el = document.getElementById("waveLoadingOverlay");
    if (token === visualRenderToken && el && !el.classList.contains("hidden")) {
      el.classList.add("stalled");
    }
  }, 20000);
  window.setTimeout(() => {
    if (token === visualRenderToken) setWaveformLoading(false);
  }, 60000);
  setCurrentJobId(jobId);
  setTotalDuration(duration || 0);
  loadMixIntoState(jobId);
  refreshMixerVisuals();
  setLaneControlsEnabled(true);
  setLoopEnabled(false);
  setLoopStart(0);
  setLoopEnd(0);
  loopBtn.classList.remove("active");
  loopRegionEl.classList.add("hidden");

  // User-selected stems only. Backend produced all 6, but the import-
  // page toggles tell us which ones the user actually wanted to see.
  // Filter early so multitrack, decoded-visuals, energy baseline, and
  // mini-waves all operate on the trimmed set. The synthetic "original"
  // track always passes the filter -- the user wants the full song
  // available alongside the isolated stems for A/B comparison. (When
  // the user selected all 6 stems, the backend doesn't produce
  // original.wav, so it's simply not in `stems` and the mixer/sidebar
  // rows for it stay hidden.)
  stems = stems.filter((s) => s.name === "original" || selectedStems.has(s.name));
  applyStemSelectionFilter(new Set(stems.map((s) => s.name)));

  for (const stem of stems) {
    const row = mixerEl.querySelector(`.lane-header[data-stem="${stem.name}"]`);
    if (!row) continue;
    const dl = row.querySelector(".lane-dl");
    if (dl) {
      dl.href = stem.url;
      dl.download = `${stem.name}.wav`;
    }
  }

  stemsChip.textContent = `${stems.length} Stems`;

  if (thumbnail) {
    npThumb.onload = () => npThumb.classList.add("loaded");
    npThumb.onerror = () => npThumb.classList.remove("loaded");
    npThumb.src = thumbnail;
  }

  clearOverviewWaveforms();
  renderAllDecodedVisuals(stems, token);

  setTrackIndex(Object.fromEntries(stems.map((s, i) => [s.name, i])));
  multitrackContainer.innerHTML = "";
  const mt = Multitrack.create(
    stems.map((s, i) => ({
      id: i,
      url: s.url,
      draggable: false,
      startPosition: 0,
      volume: 1,
      options: {
        waveColor: STEM_COLORS[s.name] || "#a0a0a0",
        progressColor: PROGRESS_COLOR,
        height: WAVEFORM_LANE_HEIGHT,
        barWidth: 3,
        barGap: 2,
        barRadius: 2,
        cursorWidth: 0,
      },
    })),
    {
      container: multitrackContainer,
      // 0 = fit waveforms to the container width. Any positive value
      // makes the bundle's internal div wider than the visible area
      // (so it scrolls horizontally), while our ruler ticks, playhead
      // marker, and loop-region all render relative to the visible
      // waves-column width — they go out of sync the moment the inner
      // div scrolls. Fitting to view keeps the three perfectly aligned.
      minPxPerSec: 0,
      rightButtonDrag: false,
      cursorWidth: 1.5,
      cursorColor: "#e54e4e",
      trackBackground: "transparent",
      trackBorderColor: "rgba(148, 163, 184, 0.08)",
    },
  );
  setMultitrack(mt);

  // Stop button glows iff transport is paused AND at the "start" (0,
  // or loopStart if loop is on). Centralised here so manual seeks via
  // the ruler also update the visual without extra plumbing.
  const STOP_TOLERANCE_SEC = 0.15;
  const updateStopVisual = () => {
    const t = mt.getCurrentTime?.() ?? 0;
    const startPos = loopEnabled ? loopStart : 0;
    const atStart = Math.abs(t - startPos) < STOP_TOLERANCE_SEC;
    const stopped = !mt.isPlaying() && atStart;
    stopBtn.classList.toggle("stopped", stopped);
  };

  mt.once("canplay", () => {
    setWaveformLoading(false);
    const ctx = mt.audioContext;
    console.debug(
      `[player] canplay — ${stems.length} stems, ctx=${ctx?.state}, audios:`,
      mt.audios?.map((a, i) => `${stems[i]?.name}:${a?.constructor?.name}`),
    );
    // Log any audio element load errors
    mt.audios?.forEach((a, i) => {
      if (a instanceof HTMLMediaElement) {
        a.addEventListener("error", () =>
          console.error(`[player] audio error stem[${i}] ${stems[i]?.name}:`, a.error?.message, a.error?.code),
        { once: true });
      }
    });
    if (!totalDuration) setTotalDuration(mt.getDuration() || 0);
    timeEl.textContent = `00:00 / ${fmtTime(totalDuration)}`;
    buildRuler(totalDuration);
    buildPresenceRuler(totalDuration);
    updateFooterTimes(0);
    updatePresencePlayhead(0);
    setMasterVolume(masterFader ? parseFloat(masterFader.value) : masterVolume);
    applyMix();
    setLoopStart(totalDuration * LOOP_DEFAULT_START_FRAC);
    setLoopEnd(totalDuration * LOOP_DEFAULT_END_FRAC);
    renderAllMiniWaves(mt, stems);
    applyWaveZoom();

    // CRITICAL: the Multitrack class itself does NOT emit play / pause /
    // timeupdate / seeking — those fire on the individual wavesurfer
    // instances. We pick wavesurfers[0] as the master clock since all
    // stems are kept in sync by the bundle's startSync() loop.
    const wsArr = mt.wavesurfers || mt._wavesurfers;
    const ws = wsArr?.[0];
    if (!ws) return;

    let loopWrapLogged = false;
    ws.on("timeupdate", (t) => {
      timeEl.textContent = `${fmtTime(t)} / ${fmtTime(totalDuration)}`;
      updatePlayheadMarker(t);
      updateFooterTimes(t);
      updatePresencePlayhead(t);
      updateStopVisual();
      if (loopEnabled && totalDuration > 0 && t >= loopEnd) {
        if (!loopWrapLogged) {
          loopWrapLogged = true;
        }
        mt.setTime(loopStart);
      }
    });
    ws.on("play", () => {
      playBtn.classList.add("playing");
      stopBtn.classList.remove("stopped");
    });
    ws.on("pause", () => {
      playBtn.classList.remove("playing");
      updateStopVisual();
    });
    ws.on("seeking", updateStopVisual);
  });
}
