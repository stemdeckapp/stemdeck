import Multitrack from "/vendor/multitrack.js";
import { fmtTime } from "./utils.js";
import {
  STEM_NAMES, TRACK_NAMES, STEM_COLORS, PROGRESS_COLOR,
  LOOP_DEFAULT_START_FRAC, LOOP_DEFAULT_END_FRAC, LANE_VOLUME_MAX,
} from "./constants.js";
import {
  mixerEl, multitrackContainer, bpmChip, keyChip, stemsChip, timeEl,
  titleEl, npThumb, rulerTime, wavesGrid, playBtn,
  stopBtn, loopBtn, loopRegionEl,
  multitrack, currentJobId, trackIndex, totalDuration, loopEnabled,
  loopStart, loopEnd, trackAnalysers,
  masterVolume, masterFader, mixerState,
  audioEngine, setAudioEngine,
  setMultitrack, setCurrentJobId, setTrackIndex, setTotalDuration,
  setLoopEnabled, setLoopStart, setLoopEnd, setMasterVolume,
  waveScroll, selectedStems,
  footerTitle, footerMeta, footerThumb,
  setFooterWaveDrawFn,
} from "./state.js";
import { createAudioEngine, estimateDecodedBytes } from "./audioEngine.js";
import {
  loadMixIntoState, resetMixerState, refreshMixerVisuals,
  setLaneControlsEnabled, ensureMixerStateDefaults, applyMix,
  renderRealMiniWave, renderMixerRow,
} from "./mixer.js";
import {
  buildRuler, updatePlayheadMarker, updateLoopRegionVisual,
  applyWaveZoom, buildPresenceRuler, updateFooterTimes,
  updatePresencePlayhead,
} from "./transport.js";
import { stopVuLoop } from "./audio.js";
import { destroySections } from "./sections.js";

// Feature flag for the Web Audio decode-and-mix engine (audioEngine.js).
// Playback runs off decoded AudioBuffers on a single AudioContext clock instead
// of N streaming <audio> elements — fixes Safari/WKWebView choppiness.
//
// Default ON everywhere. The prior Windows regression (waveform stuck, no audio)
// was caused by connection competition: both the multitrack and the engine
// fetched the same 6 stem WAVs concurrently, saturating the 6-connection
// HTTP/1.1 limit on WebView2/WKWebView and stalling multitrack's blob fetches
// so `canplay` never fired. Fixed by passing null URLs to the multitrack when
// the engine is active (wireUpAudio), giving the engine exclusive HTTP access.
// An explicit localStorage "stemdeck.audioEngine" = "0" forces the streaming
// path; "1" forces the engine (useful for debugging).
function audioEngineEnabled() {
  try {
    const pref = localStorage.getItem("stemdeck.audioEngine");
    if (pref === "1") return true;
    if (pref === "0") return false;
  } catch (e) {
    console.warn("[player] audioEngine flag read failed:", e);
  }
  return true; // default ON — smooth audio everywhere
}

// Above this estimated decoded-PCM size the engine is skipped and playback
// falls back to streaming, so a long full-stem track can't exhaust WKWebView's
// memory. ~1.2 GB ≈ 6 stems × ~10 min (or 4 stems × ~15 min) at 44.1 kHz/Float32.
const MAX_ENGINE_DECODED_BYTES = 1.2e9;

// Stem-selection filter: the import-page stem-choice toggles set
// selectedStems (state.js). Backend always processes all 6 -- we
// hide the rows for unselected stems in the studio dashboard so the
// user "only sees what they selected to extract".
const _STEM_ROW_SELECTORS = [
  ".stem-list span[data-stem]",
  ".presence-bars i[data-stem]",
  ".presence-labels span",
];

function applyStemSelectionFilter(presentNames) {
  // Waveform rows: original hides if absent; STEM_NAMES rows always show, grayed if absent
  for (const el of document.querySelectorAll(".stem-waveform-row[data-stem]")) {
    const stem = el.dataset.stem;
    if (stem === "original") {
      el.classList.toggle("hidden", !presentNames.has(stem));
      el.classList.remove("unavailable");
    } else {
      el.classList.remove("hidden");
      el.classList.toggle("unavailable", !presentNames.has(stem));
    }
  }
  const originalRow = presentNames.has("original") ? 1 : 0;
  const visibleTrackCount = originalRow + STEM_NAMES.length;
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
  const mixerCap = STEM_NAMES.length + (presentNames.has("original") ? 1 : 0);
  for (const name of STEM_NAMES) {
    if (visibleMixerNames.length >= mixerCap) break;
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
  for (const el of document.querySelectorAll(".stem-waveform-row[data-stem]")) {
    el.classList.remove("hidden");
    el.classList.remove("unavailable");
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
  for (const name of ["original", ...STEM_NAMES]) {
    const ph = document.createElement("div");
    ph.className = "lane-placeholder";
    ph.dataset.stem = name;
    ph.style.setProperty("--lane-color", STEM_COLORS[name] || "#a0a0a0");
    multitrackContainer.appendChild(ph);
  }
}

const OVERVIEW_WAVE_POINTS = 1500;
const STEM_VU_FPS = 30;
const WAVEFORM_LANE_HEIGHT = 70;
const WAVEFORM_SEPARATOR_HEIGHT = 2;
let visualRenderToken = 0;
let visualAudioContext = null;
let stemVuRafId = null;
let _footerWavePeaks = null;
let _lastFooterProgress = 0;
let _footerPlaceholderResizeObs = null;
const _FOOTER_BARS = 300;

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

// The overview lanes are drawn as discrete vertical bars (mirrored around the
// center line), matching WaveSurfer's barWidth:3 / barGap:2 / barRadius:2 look
// used on the streaming path -- so the engine path (this SVG, from peaks.json)
// is visually identical to the WaveSurfer canvas. We set the SVG viewBox width
// to the bar count and stretch it across the lane (preserveAspectRatio="none"),
// so one viewBox unit == one bar slot. Choosing barCount = laneWidth / 5 makes
// each slot 5px, and a 0.6-unit bar renders as ~3px with a ~2px gap.
const OVERVIEW_BAR_SLOT_PX = 5; // 3px bar + 2px gap, matches WaveSurfer defaults
const OVERVIEW_BAR_FRAC = 0.6; // bar width as a fraction of its slot (3/5)

// How many bars fit across the current lane width. Falls back to a sane count
// before layout settles. Returned bars are clamped so very narrow panels still
// show a readable waveform.
function overviewBarCount() {
  const w = document.querySelector(".waves-column")?.clientWidth || 0;
  const t = w > 0 ? Math.round(w / OVERVIEW_BAR_SLOT_PX) : 220;
  return Math.max(40, t);
}

// Downsample signed [min,max] peaks to `barCount` mirrored bars and emit them as
// <rect>s in a viewBox whose width == barCount (1 unit per slot). `norm` is the
// shared cross-stem normalization so loudness relationships are preserved.
function barsWaveformSvg(peaks, norm, barCount) {
  const n = peaks.length;
  if (!n) return "";
  const off = (1 - OVERVIEW_BAR_FRAC) / 2;
  const rects = new Array(barCount);
  for (let i = 0; i < barCount; i++) {
    const start = Math.floor((i * n) / barCount);
    const end = Math.max(start + 1, Math.floor(((i + 1) * n) / barCount));
    let mn = 0;
    let mx = 0;
    for (let j = start; j < end && j < n; j++) {
      if (peaks[j][0] < mn) mn = peaks[j][0];
      if (peaks[j][1] > mx) mx = peaks[j][1];
    }
    const amp = Math.min(1, Math.max(-mn, mx) * norm);
    // Min height keeps silent regions as a faint baseline dotted line (as the
    // WaveSurfer bars did) instead of vanishing entirely.
    const h = Math.max(0.7, amp * 44);
    rects[i] =
      `<rect x="${(i + off).toFixed(3)}" y="${(24 - h / 2).toFixed(2)}" `
      + `width="${OVERVIEW_BAR_FRAC}" height="${h.toFixed(2)}" rx="0.3"></rect>`;
  }
  return rects.join("");
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

function renderOverviewWaveformPath(stemName, peaks, norm, color, barCount) {
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
  // A row is created for every mixer lane, including stems with no audio (e.g.
  // a subset extraction). The rows are flex-distributed across the lane stack,
  // so they only stay 1:1 with the mixer lanes when their count matches; an
  // empty lane renders nothing but keeps that alignment. Without this, fewer
  // waveform rows than lanes stretch to fill the stack and drift off their
  // tracks.
  if (!peaks?.length) {
    row.innerHTML = "";
    return;
  }
  const bars = barCount || overviewBarCount();
  row.innerHTML = `
    <svg class="stem-waveform-svg" viewBox="0 0 ${bars} 48" preserveAspectRatio="none" aria-hidden="true">
      ${barsWaveformSvg(peaks, norm, bars)}
    </svg>
  `;
}

// The lane set must mirror the mixer/multitrack lanes (orderedNames in
// wireUpAudio): "original" plus the stems when an original lane is present,
// otherwise just the stems. Rendering a row for every lane keeps the overlay
// aligned even when only a subset of stems was extracted.
function overviewLaneNames(stems) {
  return stems.some((s) => s.name === "original") ? TRACK_NAMES : STEM_NAMES;
}

function renderAllOverviewWaveformsFromPeaks(stems, peaksData) {
  const laneNames = overviewLaneNames(stems);
  // Only the extracted/selected stems (plus original) get a waveform, even if
  // peaks.json carries data for stems the user didn't keep (Demucs separates
  // all 6 internally). Every lane still gets a ROW so the overlay stays aligned;
  // non-selected lanes just render empty.
  const present = new Set(stems.map((s) => s.name));
  let globalMax = 0;
  for (const name of laneNames) {
    if (!present.has(name)) continue;
    const pts = peaksData[name];
    if (!pts?.length) continue;
    for (const [mn, mx] of pts) {
      if (mx > globalMax) globalMax = mx;
      if (-mn > globalMax) globalMax = -mn;
    }
  }
  const norm = globalMax > 0 ? 1 / globalMax : 0;
  const bars = overviewBarCount();
  for (const name of laneNames) {
    const pts = present.has(name) ? peaksData[name] : null;
    renderOverviewWaveformPath(name, pts, norm, STEM_COLORS[name] || "#a0a0a0", bars);
  }
}

// Normalize all stems to a single shared max so the overview waveforms
// preserve real amplitude relationships (drums tall, piano short),
// matching what a DAW shows. Per-stem normalization made every lane
// fill its row regardless of how loud the stem actually was.
function renderAllOverviewWaveforms(stems, decodedMap) {
  const laneNames = overviewLaneNames(stems);
  const peaksByStem = new Map();
  let globalMax = 0;
  for (const name of laneNames) {
    const buf = decodedMap.get(name);
    if (!isAudioBufferLike(buf)) continue;
    const peaks = bufferMinMaxPeaks(buf, OVERVIEW_WAVE_POINTS);
    peaksByStem.set(name, peaks);
    for (const [mn, mx] of peaks) {
      if (mx > globalMax) globalMax = mx;
      if (-mn > globalMax) globalMax = -mn;
    }
  }
  const norm = globalMax > 0 ? 1 / globalMax : 0;
  const bars = overviewBarCount();
  for (const name of laneNames) {
    renderOverviewWaveformPath(name, peaksByStem.get(name), norm, STEM_COLORS[name] || "#a0a0a0", bars);
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
    // When the Web Audio engine owns playback, the multitrack is silent — read
    // the clock/transport state from the engine so the meters track real audio.
    const src = audioEngine ?? multitrack;
    const playing = src.isPlaying?.() ?? false;
    const time = src.getCurrentTime?.() ?? 0;
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

export function destroyPlayer() {
  document.querySelector(".app")?.classList.remove("is-import");
  document.querySelector(".app")?.classList.remove("engine-waveforms");
  document.querySelector(".app")?.classList.add("no-track");
  destroySections();
  stopVuLoop();
  stopStemVuLoop();
  if (audioEngine) {
    audioEngine.destroy();
    setAudioEngine(null);
  }
  if (multitrack) {
    multitrack.destroy();
    setMultitrack(null);
  }
  if (visualAudioContext) {
    visualAudioContext.close().catch(() => {});
    visualAudioContext = null;
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
  applyWaveZoom();
  buildPresenceRuler(0);
  updateFooterTimes(0);
  updatePresencePlayhead(0);
  if (waveScroll) waveScroll.scrollLeft = 0;
  loopBtn.classList.remove("active");
  playBtn.classList.remove("playing");
  stopBtn.classList.remove("stopped");
  loopRegionEl.classList.add("hidden");
  _footerWavePeaks = null;
  _lastFooterProgress = 0;
  _footerPlaceholderResizeObs?.disconnect();
  _footerPlaceholderResizeObs = null;
  _footerWaveResizeObs?.disconnect();
  setFooterWaveDrawFn(null);
  const cv = document.getElementById("footer-waveform");
  if (cv) { const c = cv.getContext("2d"); c?.clearRect(0, 0, cv.width, cv.height); }
  updateFooterTrack({});
}

export function renderEmptyShell() {
  document.querySelector(".app")?.classList.remove("is-import");
  document.querySelector(".app")?.classList.add("no-track");
  stopStemVuLoop();
  ensureMixerStateDefaults();
  mixerEl.innerHTML = "";
  for (const name of ["original", ...STEM_NAMES]) {
    const { row } = renderMixerRow({ name, url: "#" });
    mixerEl.appendChild(row);
  }
  requestAnimationFrame(() => _applyLaneHeight(1 + STEM_NAMES.length));
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
  stems.forEach((stem) => {
    const i = trackIndex[stem.name];
    if (i === undefined) return;
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

let _loadingShownAt = 0;
const _LOADING_MIN_MS = 900;
let _currentStems = [];
let _mixUrl = null;
let _currentTitle = "";
// Whether the current job has a preserved video track (mp4 upload). Gates the
// "Export Mix (with video)" item (CSS .has-video) and downloadCurrentVideo().
let _currentHasVideo = false;

export function setWaveformLoading(loading, phrase) {
  const el = document.getElementById("waveLoadingOverlay");
  if (!el) return;
  if (loading) {
    _loadingShownAt = performance.now();
    const phraseEl = document.getElementById("waveLoadingPhrase");
    if (phraseEl && phrase !== undefined) phraseEl.textContent = phrase;
    else if (phraseEl && !phraseEl.textContent) phraseEl.textContent = "Still loading waveform…";
    el.classList.remove("hidden");
  } else {
    const elapsed = performance.now() - _loadingShownAt;
    const delay = Math.max(0, _LOADING_MIN_MS - elapsed);
    window.setTimeout(() => {
      el.classList.add("hidden");
      el.classList.remove("stalled");
      const phraseEl = document.getElementById("waveLoadingPhrase");
      if (phraseEl) phraseEl.textContent = "";
    }, delay);
  }
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

function _applyLaneHeight(count) {
  const wavePanel = document.querySelector(".daw-wave-panel");
  const panelH = wavePanel?.clientHeight ?? 0;
  const laneH = panelH > 0 && count > 0
    ? Math.max(WAVEFORM_LANE_HEIGHT, Math.floor(panelH / count))
    : WAVEFORM_LANE_HEIGHT;
  const appEl = document.querySelector(".app");
  appEl?.style.setProperty("--lane-h", `${laneH + 2}px`);
  appEl?.style.setProperty(
    "--wave-widget-track-stack-h",
    `${count * laneH + (count - 1) * WAVEFORM_SEPARATOR_HEIGHT}px`,
  );
  return laneH;
}

export function wireUpAudio(jobId, stems, duration, thumbnail, mixUrl = null, title = "", peaksPromise = null, hasVideo = false) {
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
  refreshMixerVisuals();
  const mixReady = loadMixIntoState(jobId, stems.map((s) => s.name))
    .then(() => { if (currentJobId === jobId) refreshMixerVisuals(); })
    .catch((e) => { console.warn("[player] mix state load failed:", e); });
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
  _currentStems = stems;
  _mixUrl = mixUrl || null;
  _currentTitle = title || "";
  _currentHasVideo = !!hasVideo;
  document.getElementById("footer-export-wrap")?.classList.toggle("has-video", !!hasVideo);
  applyStemSelectionFilter(new Set(stems.map((s) => s.name)));
  updateFooterTrack({ thumbnail, stemCount: stems.filter((s) => s.name !== "original").length });

  // Reset footer waveform state — will be re-populated below after peaks fetch.
  _footerWavePeaks = null;
  setFooterWaveDrawFn(null);
  const waveformUrl = _mixUrl
    || stems.find((s) => s.name === "original")?.url
    || stems[0]?.url;

  // Use the pre-started peaks promise from catalog.js (started in parallel with
  // the job-data fetch) so peaks.json is resolved before Multitrack.create fires
  // its stem WAV fetches. This keeps peaks.json out of Safari's 6-connection-per-
  // origin window, preventing stem WAV queuing that causes buffer underruns.
  let precomputedPeaks = {};
  const _footerStemName = stems.find((s) => s.name === "original") ? "original" : stems[0]?.name;
  const _peaksPromise = peaksPromise ?? (jobId
    ? (() => {
        const ac = new AbortController();
        const timer = setTimeout(() => ac.abort(), 3000);
        return fetch(`/api/jobs/${jobId}/stems/peaks.json`, { signal: ac.signal })
          .then((r) => (r.ok ? r.json() : {}))
          .catch(() => ({}))
          .finally(() => clearTimeout(timer));
      })()
    : Promise.resolve({}));

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

  // "original" is prepended at row 0 only when it actually has a URL so it
  // appears at the top. Omitting it when absent avoids a phantom 70px gap.
  // STEM_NAMES follow at the next consecutive rows so mixer lanes stay aligned.
  const stemsByName = Object.fromEntries(stems.map((s) => [s.name, s]));
  const orderedNames = [...(stemsByName["original"] ? ["original"] : []), ...STEM_NAMES];
  setTrackIndex(Object.fromEntries(orderedNames.map((name, i) => [name, i])));
  multitrackContainer.innerHTML = "";

  // Decide up front whether the Web Audio engine will own playback. When it
  // does, the multitrack is mounted for visuals only and must NOT load the stem
  // WAVs — otherwise its 6 blob fetches compete with the engine's 6 decodes for
  // the 6-connection HTTP/1.1 limit and `canplay` stalls (the WebView2/WKWebView
  // freeze). Null URLs make the multitrack's <audio> elements ready instantly.
  const engineStemCount = stems.filter((s) => s.url).length;
  const engineTooLarge =
    estimateDecodedBytes(totalDuration, engineStemCount) > MAX_ENGINE_DECODED_BYTES;
  const useEngine = audioEngineEnabled() && !engineTooLarge;
  // The null-URL multitrack (engine path) has no WaveSurfer canvas, so reveal the
  // SVG overview layer as the visible waveform (CSS hides it on the streaming path).
  document.querySelector(".app")?.classList.toggle("engine-waveforms", useEngine);
  if (engineTooLarge) {
    console.warn(
      `[player] track too large for Web Audio engine `
      + `(~${Math.round(estimateDecodedBytes(totalDuration, engineStemCount) / 1e6)} MB `
      + `for ${engineStemCount} stems × ${Math.round(totalDuration)}s); using streaming path`,
    );
  }

  const laneH = _applyLaneHeight(orderedNames.length);

  const mt = Multitrack.create(
    orderedNames.map((name, i) => ({
      id: i,
      url: useEngine ? null : (stemsByName[name]?.url ?? null),
      draggable: false,
      startPosition: 0,
      volume: stemsByName[name] ? 1 : 0,
      options: {
        waveColor: STEM_COLORS[name] || "#a0a0a0",
        progressColor: PROGRESS_COLOR,
        height: laneH,
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
      cursorWidth: 0,
      cursorColor: "#e54e4e",
      trackBackground: "transparent",
      trackBorderColor: "rgba(148, 163, 184, 0.08)",
    },
  );
  setMultitrack(mt);

  // Apply peaks to visuals once the fetch resolves. Runs after Multitrack.create
  // so AudioContext is already initialised before this callback fires.
  _peaksPromise.then((peaks) => {
    if (token !== visualRenderToken) return;
    precomputedPeaks = peaks;
    const footerPeaks = peaks[_footerStemName] || peaks[Object.keys(peaks)[0]];
    if (footerPeaks?.length) {
      const step = Math.ceil(footerPeaks.length / _FOOTER_BARS);
      _footerWavePeaks = footerPeaks.filter((_, i) => i % step === 0).slice(0, _FOOTER_BARS);
      setFooterWaveDrawFn(_drawFooterWave);
      _drawFooterWave(0);
    } else if (waveformUrl) {
      initFooterWaveform(waveformUrl);
    }
    // Render the overview as soon as peaks arrive — independent of the audio
    // `canplay` event, so the waveform appears even if the engine owns playback
    // (null-URL multitrack) or canplay is delayed. Idempotent: the canplay
    // handler also renders peaks, but both clearOverviewWaveforms() first.
    if (Object.keys(peaks).length) {
      clearOverviewWaveforms();
      renderAllOverviewWaveformsFromPeaks(stems, peaks);
    }
  });

  // Stop button glows iff transport is paused AND at the "start" (0,
  // or loopStart if loop is on). Centralised here so manual seeks via
  // the ruler also update the visual without extra plumbing.
  const STOP_TOLERANCE_SEC = 0.15;
  const updateStopVisual = () => {
    const src = audioEngine ?? mt;
    const t = src.getCurrentTime?.() ?? 0;
    const startPos = loopEnabled ? loopStart : 0;
    const atStart = Math.abs(t - startPos) < STOP_TOLERANCE_SEC;
    const stopped = !src.isPlaying() && atStart;
    stopBtn.classList.toggle("stopped", stopped);
  };

  mt.once("canplay", () => {
    setWaveformLoading(false);
    const ctx = mt.audioContext;
    console.debug(
      `[player] canplay — ${stems.length} stems, ctx=${ctx?.state}, audios:`,
      mt.audios?.map((a, i) => `${orderedNames[i]}:${a?.constructor?.name}`),
    );
    // Log load errors only for stems that actually have a source URL
    mt.audios?.forEach((a, i) => {
      if (a instanceof HTMLMediaElement && stemsByName[orderedNames[i]]?.url) {
        a.addEventListener("error", () =>
          console.error(`[player] audio error stem[${i}] ${orderedNames[i]}:`, a.error?.message, a.error?.code),
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
    mixReady.then(() => { if (currentJobId === jobId && multitrack === mt) applyMix(); });
    setLoopStart(totalDuration * LOOP_DEFAULT_START_FRAC);
    setLoopEnd(totalDuration * LOOP_DEFAULT_END_FRAC);
    // When the engine owns playback the multitrack has null URLs and no decoded
    // audio, so mini-waves are driven from the engine's buffers instead (below).
    if (!useEngine) renderAllMiniWaves(mt, stems);
    applyWaveZoom();
    if (Object.keys(precomputedPeaks).length) {
      clearOverviewWaveforms();
      renderAllOverviewWaveformsFromPeaks(stems, precomputedPeaks);
    }

    // CRITICAL: the Multitrack class itself does NOT emit play / pause /
    // timeupdate / seeking — those fire on the individual wavesurfer
    // instances. We pick wavesurfers[0] as the master clock since all
    // stems are kept in sync by the bundle's startSync() loop.
    const wsArr = mt.wavesurfers || mt._wavesurfers;

    // Streaming path only (engine off): render overview/energy/VU from
    // wavesurfer's decoded buffers. When the engine is on, the multitrack has
    // null URLs (no decode), so these are driven from the engine's buffers in
    // the eng.ready handler below instead.
    if (!useEngine && wsArr?.length) {
      const decoded = new Map();
      const waits = stems.map((stem) => {
        const idx = orderedNames.indexOf(stem.name);
        const wsi = idx >= 0 ? wsArr[idx] : null;
        if (!wsi) return Promise.resolve();
        const buf = wsi.getDecodedData?.();
        if (isAudioBufferLike(buf)) { decoded.set(stem.name, buf); return Promise.resolve(); }
        return new Promise((resolve) => {
          wsi.once?.("decode", () => { const buf = wsi.getDecodedData?.(); if (isAudioBufferLike(buf)) decoded.set(stem.name, buf); resolve(); });
          window.setTimeout(resolve, 15000);
        });
      });
      Promise.all(waits).then(() => {
        if (token !== visualRenderToken) return;
        if (!Object.keys(precomputedPeaks).length) {
          clearOverviewWaveforms();
          renderAllOverviewWaveforms(stems, decoded);
        }
        renderStemEnergyBaseline(stems, decoded);
        startStemVuLoop(stems, decoded, token);
      });
    }

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
      applyMix();
    });
    ws.on("pause", () => {
      playBtn.classList.remove("playing");
      updateStopVisual();
    });
    ws.on("seeking", updateStopVisual);

    // ── Web Audio engine (flag-gated) ──────────────────────────────────────
    // When enabled, decode the active stems and play them off a single
    // AudioContext clock instead of the streaming <audio> elements above. The
    // multitrack stays mounted for visuals only (it never plays), so the ws
    // play/pause/timeupdate handlers wired above simply don't fire — the engine
    // drives the identical UI updates via onTime/onEnded. transport.js, mixer
    // applyMix, the VU loop, and updateStopVisual all route through
    // `audioEngine ?? multitrack`, so flag-off is byte-identical to before.
    if (useEngine) {
      // Mirror the streaming "timeupdate" handler body so playhead/footer/
      // presence/stop-visual update the same way regardless of which clock runs.
      const driveTransportUi = (t) => {
        timeEl.textContent = `${fmtTime(t)} / ${fmtTime(totalDuration)}`;
        updatePlayheadMarker(t);
        updateFooterTimes(t);
        updatePresencePlayhead(t);
        updateStopVisual();
      };
      if (audioEngine) { audioEngine.destroy(); setAudioEngine(null); }
      const eng = createAudioEngine(stems, {
        onTime: driveTransportUi,
        onEnded: () => { playBtn.classList.remove("playing"); updateStopVisual(); },
      });
      setAudioEngine(eng);
      eng.ready.then((ok) => {
        // Bail if the user switched tracks while we were decoding.
        if (token !== visualRenderToken || multitrack !== mt) {
          eng.destroy();
          if (audioEngine === eng) setAudioEngine(null);
          return;
        }
        if (!ok) {
          // No decodable stems — fall back to the streaming path transparently.
          console.warn("[player] audio engine had no decodable stems; using streaming path");
          eng.destroy();
          setAudioEngine(null);
          return;
        }
        eng.setLoop(loopEnabled, loopStart, loopEnd);
        applyMix(); // push per-stem gains (incl. >1.0 boost) into the engine
        // Drive the decode-dependent visuals from the engine's own decoded
        // buffers (the null-URL multitrack has none): overview waveforms (when
        // no precomputed peaks), energy bars, VU envelopes, and lane mini-waves.
        const decoded = eng.getBuffers();
        if (!Object.keys(precomputedPeaks).length) {
          clearOverviewWaveforms();
          renderAllOverviewWaveforms(stems, decoded);
        }
        renderStemEnergyBaseline(stems, decoded);
        startStemVuLoop(stems, decoded, token);
        for (const stem of stems) {
          const buf = decoded.get(stem.name);
          if (isAudioBufferLike(buf)) {
            renderDecodedStemVisuals(stem.name, buf, STEM_COLORS[stem.name] || "#a0a0a0");
          }
        }
      }).catch((e) => {
        console.warn("[player] audio engine init failed; using streaming path:", e);
        eng.destroy();
        if (audioEngine === eng) setAudioEngine(null);
      });
    }
  });
}

export function drawFooterPlaceholder() {
  _drawPlaceholderWave();
  const bar = document.getElementById("footer-waveform")?.closest(".footer-wave-bar");
  if (bar) {
    _footerPlaceholderResizeObs?.disconnect();
    _footerPlaceholderResizeObs = new ResizeObserver(() => { if (!_footerWavePeaks) _drawPlaceholderWave(); });
    _footerPlaceholderResizeObs.observe(bar);
  }
}

function _drawPlaceholderWave() {
  const canvas = document.getElementById("footer-waveform");
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h) return;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const n = 300;
  const barW = canvas.width / n;
  const gap = 1;
  const cy = canvas.height / 2;
  for (let i = 0; i < n; i++) {
    const t = i / n;
    const amp = 0.15 + 0.55 * (
      0.5 * Math.abs(Math.sin(t * Math.PI * 3.7 + 0.5)) +
      0.3 * Math.abs(Math.sin(t * Math.PI * 9.1 + 1.2)) +
      0.2 * Math.abs(Math.sin(t * Math.PI * 21.3 + 2.8))
    );
    const barH = Math.max(3, amp * canvas.height * 0.78);
    const x = Math.round(i * barW);
    const nextX = i === n - 1 ? canvas.width : Math.round((i + 1) * barW);
    const bw = Math.max(1, nextX - x - gap);
    ctx.fillStyle = "rgba(255,255,255,0.07)";
    ctx.fillRect(x, cy - barH / 2, bw, barH);
  }
}

function _drawFooterWave(progress) {
  _lastFooterProgress = progress;
  const canvas = document.getElementById("footer-waveform");
  if (!canvas) return;
  if (!_footerWavePeaks) { _drawPlaceholderWave(); return; }
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  if (!w || !h) return;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const n = _footerWavePeaks.length;
  let maxAbs = 0;
  for (const [mn, mx] of _footerWavePeaks) {
    if (mx > maxAbs) maxAbs = mx;
    if (-mn > maxAbs) maxAbs = -mn;
  }
  const norm = maxAbs > 0 ? 1 / maxAbs : 1;
  const cy = canvas.height / 2;
  const barW = canvas.width / n;
  const gap = 1;
  const playedIdx = Math.floor(progress * n);
  for (let i = 0; i < n; i++) {
    const [mn, mx] = _footerWavePeaks[i];
    const top = cy - mx * norm * cy * 0.78;
    const bot = cy - mn * norm * cy * 0.78;
    const barH = Math.max(3, bot - top);
    const x = Math.round(i * barW);
    const nextX = i === n - 1 ? canvas.width : Math.round((i + 1) * barW);
    const bw = Math.max(1, nextX - x - gap);
    ctx.fillStyle = i < playedIdx ? "#f4b740" : "rgba(255,255,255,0.13)";
    ctx.fillRect(x, top, bw, barH);
  }
  if (progress > 0.001 && progress < 0.999) {
    const px = progress * canvas.width;
    ctx.beginPath();
    ctx.arc(px, cy, 4 * dpr, 0, Math.PI * 2);
    ctx.fillStyle = "#f4b740";
    ctx.fill();
  }
}

let _footerWaveResizeObs = null;
async function initFooterWaveform(stemUrl) {
  const canvas = document.getElementById("footer-waveform");
  if (!canvas || !stemUrl) return;
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtx) return;
  try {
    visualAudioContext ??= new AudioCtx();
    const res = await fetch(stemUrl, { cache: "force-cache" });
    if (!res.ok) return;
    const buf = await visualAudioContext.decodeAudioData(await res.arrayBuffer());
    _footerWavePeaks = bufferMinMaxPeaks(buf, _FOOTER_BARS);
    setFooterWaveDrawFn(_drawFooterWave);
    _drawFooterWave(0);
    // Redraw on resize so the canvas fills its container correctly
    _footerWaveResizeObs?.disconnect();
    _footerWaveResizeObs = new ResizeObserver(() => _drawFooterWave(_lastFooterProgress));
    _footerWaveResizeObs.observe(canvas.parentElement || canvas);
  } catch (e) {
    console.warn("[player] footer waveform:", e);
  }
}

export function updateFooterTrack({ title, thumbnail, key, bpm, stemCount } = {}) {
  if (footerThumb) {
    const artEl = footerThumb.closest(".footer-art");
    if (thumbnail) {
      footerThumb.src = thumbnail;
      footerThumb.onload = () => {
        footerThumb.classList.add("loaded");
        artEl?.classList.add("has-art");
      };
      footerThumb.onerror = () => {
        footerThumb.classList.remove("loaded");
        artEl?.classList.remove("has-art");
      };
    } else {
      footerThumb.removeAttribute("src");
      footerThumb.classList.remove("loaded");
      artEl?.classList.remove("has-art");
    }
  }
  if (footerTitle && title !== undefined) footerTitle.textContent = title;
  if (footerMeta) {
    const parts = [];
    if (key) parts.push(key);
    if (bpm) parts.push(`${Math.round(bpm)} BPM`);
    if (stemCount != null) parts.push(`${stemCount} Stems`);
    if (key !== undefined || bpm !== undefined || stemCount !== undefined)
      footerMeta.textContent = parts.join(" • ");
  }
}

function _triggerDownload(url, filename) {
  const fullUrl = url.startsWith("http") ? url : `${location.origin}${url}`;
  if (window.__TAURI__?.core?.invoke) {
    window.__TAURI__.core.invoke("save_audio_file", { url: fullUrl, filename });
    return;
  }
  const a = document.createElement("a");
  a.href = fullUrl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// Per-lane effective gain mirroring mixer.js applyMix (volume + mute + solo),
// minus the master fader (a monitoring level, not part of the mix). Returns the
// audible lanes and their gains so the backend can render a mixdown matching what
// is heard. _currentStems already includes "original" when the user picked a
// subset, so summing these lanes reconstructs the adjusted full song.
function _effectiveMixGains() {
  const anySolo = _currentStems.some((s) => mixerState[s.name]?.soloed);
  const names = [];
  const gains = [];
  for (const s of _currentStems) {
    if (s.name === "mix") continue;
    const m = mixerState[s.name];
    if (!m) continue;
    const g = m.muted ? 0 : (anySolo && !m.soloed ? 0 : m.volume);
    if (g <= 0) continue;
    names.push(s.name);
    gains.push(Math.max(0, Math.min(LANE_VOLUME_MAX, g)));
  }
  return { names, gains };
}

// Dynamic mixdown URL for the current mixer state. Returns null (no download)
// when every lane is silenced; `region` appends the loop bounds.
function _mixdownUrl(ext, region) {
  if (!currentJobId) return null;
  const { names, gains } = _effectiveMixGains();
  if (!names.length) return null;
  const q = new URLSearchParams({
    stems: names.join(","),
    gains: gains.map((g) => g.toFixed(3)).join(","),
  });
  if (region) {
    q.set("start", loopStart.toFixed(3));
    q.set("end", loopEnd.toFixed(3));
  }
  return `/api/jobs/${currentJobId}/mixdown.${ext}?${q}`;
}

function _exportFilename(ext) {
  const safe = _currentTitle
    .replace(/[^a-zA-Z0-9]+/g, "_")
    .replace(/_{2,}/g, "_")
    .slice(0, 80)
    .replace(/^_+|_+$/g, "");
  return safe ? `${safe}_exported_mix.${ext}` : `exported_mix.${ext}`;
}

// The download functions return true when a download was triggered and false
// when there is nothing audible to export (every lane muted), so the caller can
// surface a message.
export function downloadCurrentMix(ext = "wav") {
  const url = _mixdownUrl(ext, false);
  if (!url) return false;
  _triggerDownload(url, _exportFilename(ext));
  return true;
}

// MP4 export: the preserved source video muxed with the current audio mix.
// Only meaningful for mp4-sourced jobs (currentJobHasVideo()); returns false when
// there's no video track or every lane is muted.
export function downloadCurrentVideo() {
  if (!currentJobId || !_currentHasVideo) return false;
  const { names, gains } = _effectiveMixGains();
  if (!names.length) return false;
  const q = new URLSearchParams({
    stems: names.join(","),
    gains: gains.map((g) => g.toFixed(3)).join(","),
  });
  const safe = _currentTitle
    .replace(/[^a-zA-Z0-9]+/g, "_")
    .replace(/_{2,}/g, "_")
    .slice(0, 80)
    .replace(/^_+|_+$/g, "");
  const name = safe ? `${safe}_video.mp4` : "video.mp4";
  _triggerDownload(`/api/jobs/${currentJobId}/video.mp4?${q}`, name);
  return true;
}

export function downloadCurrentStems(format = "wav", onProgress) {
  const stems = _currentStems.filter((s) => s.name !== "original");
  const total = stems.length;
  if (!total) { onProgress?.(0, 0); return; }
  // Name each file "<song title>_<instrument>.<ext>" using the same title
  // sanitization as the mix/region exports.
  const safe = _currentTitle
    .replace(/[^a-zA-Z0-9]+/g, "_")
    .replace(/_{2,}/g, "_")
    .slice(0, 80)
    .replace(/^_+|_+$/g, "");
  stems.forEach((s, i) => {
    window.setTimeout(() => {
      const url = format === "mp3" ? s.url.replace(/\.wav(\?|$)/, ".mp3$1") : s.url;
      const fname = safe ? `${safe}_${s.name}.${format}` : `${s.name}.${format}`;
      _triggerDownload(url, fname);
      onProgress?.(i + 1, total);
    }, i * 150);
  });
}

export function downloadAllStemsZip(format = "wav") {
  if (!currentJobId) return;
  // Only the active (selected) stems loaded in the DAW — not all 6.
  const names = _currentStems.filter((s) => s.name !== "original").map((s) => s.name);
  if (!names.length) return;
  const safe = _currentTitle
    .replace(/[^a-zA-Z0-9]+/g, "_")
    .replace(/_{2,}/g, "_")
    .slice(0, 80)
    .replace(/^_+|_+$/g, "");
  const name = safe ? `${safe}_stems.zip` : "stems.zip";
  const q = new URLSearchParams({ format, stems: names.join(",") });
  _triggerDownload(`/api/jobs/${currentJobId}/stems/all.zip?${q}`, name);
}

function _regionFilename(ext) {
  const safe = _currentTitle
    .replace(/[^a-zA-Z0-9]+/g, "_")
    .replace(/_{2,}/g, "_")
    .slice(0, 80)
    .replace(/^_+|_+$/g, "");
  return `${safe || "region"}_region.${ext}`;
}

export function downloadRegionMix(ext = "wav") {
  if (!loopEnabled || loopStart >= loopEnd) return false;
  const url = _mixdownUrl(ext, true);
  if (!url) return false;
  _triggerDownload(url, _regionFilename(ext));
  return true;
}
