import { $ } from "./utils.js";
import { STEM_NAMES } from "./constants.js";

// ─── DOM refs ───

export const form = $("job-form");
export const urlInput = $("url");
export const submitBtn = $("submit");

export const playBtn = $("t-play");
export const playMiniBtn = $("t-play-mini");
export const stopBtn = $("t-stop");
export const loopBtn = $("t-loop");
export const titleEl = $("title");
export const bpmChip = $("t-bpm");
export const keyChip = $("t-key");
export const stemsChip = $("t-stems-chip");
export const timeEl = $("t-time");
export const masterFader = $("t-master");
export const npArt = $("np-art");
export const npThumb = $("np-thumb");

export const jobBox = $("job");
export const jobTitleEl = $("job-title");
export const jobStageEl = $("job-stage");
export const jobDetailEl = $("job-detail");
export const jobCancelBtn = $("job-cancel");
export const progressEl = $("progress");

export const errorEl = $("error");
export const lanesEl = $("lanes");
export const mixerEl = $("mixer");
export const multitrackContainer = $("multitrack-container");
export const wavesGrid = $("waves-grid");
export const rulerTime = $("ruler-time");
export const loopRegionEl = $("loop-region");
export const playheadMarker = document.querySelector(".playhead-marker");
export const waveScroll = $("wave-scroll");
export const waveCanvas = $("wave-canvas");
export const zoomInBtn = $("zoom-in");
export const zoomOutBtn = $("zoom-out");
export const zoomFitBtn = $("zoom-fit");
export const zoomTrack = $("zoom-track");
export const presenceRulerEl = $("presence-ruler");
export const presencePlayheadEl = $("presence-playhead");
export const footerTimeElapsed = $("footer-time-elapsed");
export const footerTimeTotal = $("footer-time-total");
export const stemListEl = document.querySelector(".stem-list");
export const npScrubEl = document.querySelector(".np-scrub");
export const npScrubFill = document.querySelector(".np-scrub > span");

// ─── Mutable state ───

export let eventSource = null;
export let multitrack = null;
export let currentJobId = null;

// `mixerState` is mutated in place (never reassigned). renderMixerRow's
// closures capture each entry by reference, so on a new job we merge
// localStorage values into the existing objects rather than replacing them.
export const mixerState = {};

export let trackIndex = {};
export let totalDuration = 0;
export let loopEnabled = false;
export let loopStart = 0;
export let loopEnd = 0;
export let waveZoom = 1;

// Selected stems for extraction. The set determines (a) which stem
// rows render in the studio dashboard after a job completes and (b)
// which stem audio gets loaded into the multitrack. Backend always
// runs Demucs on all 6 stems regardless -- filtering happens entirely
// client-side at render time. Persisted across reloads in localStorage
// so a user who turns off "Vocals" stays set up that way for the
// next song.
const _STEM_SEL_KEY = "stemdeck:selected-stems";
function _loadSelectedStems() {
  try {
    const raw = localStorage.getItem(_STEM_SEL_KEY);
    if (raw) {
      const arr = JSON.parse(raw);
      if (Array.isArray(arr) && arr.length > 0) {
        return new Set(arr.filter((n) => STEM_NAMES.includes(n)));
      }
    }
  } catch (e) { console.warn("[state] failed to load stem selection:", e); }
  return new Set(STEM_NAMES);
}
export let selectedStems = _loadSelectedStems();
export function saveSelectedStems() {
  try {
    localStorage.setItem(_STEM_SEL_KEY, JSON.stringify([...selectedStems]));
  } catch (e) { console.warn("[state] failed to save stem selection:", e); }
}
export function setStemSelected(name, selected) {
  if (selected) selectedStems.add(name);
  else selectedStems.delete(name);
  saveSelectedStems();
}

// Web Audio analysers for live VU meters.
export let audioContext = null;
export let masterVolume = 0.5; // mirrored from masterFader.value
export const trackAnalysers = []; // index → { analyser, data, vuEl }
export let vuRafId = null;

// Master bus nodes — created once in audio.js, shared across mixer.js.
// masterBusGain is driven by the master fader; masterLimiter is a
// transparent brickwall limiter that prevents inter-stem summing clipping.
export let masterBusGain = null;
export let masterLimiter = null;

// ─── Setter helpers for mutable state (so other modules can update) ───

export function setEventSource(v) { eventSource = v; }
export function setMultitrack(v) { multitrack = v; }
export function setCurrentJobId(v) { currentJobId = v; }
export function setTrackIndex(v) { trackIndex = v; }
export function setTotalDuration(v) { totalDuration = v; }
export function setLoopEnabled(v) { loopEnabled = v; }
export function setLoopStart(v) { loopStart = v; }
export function setLoopEnd(v) { loopEnd = v; }
export function setWaveZoom(v) { waveZoom = v; }
export function setAudioContext(v) { audioContext = v; }
export function setMasterVolume(v) { masterVolume = v; }
export function setVuRafId(v) { vuRafId = v; }
export function setMasterBusGain(v) { masterBusGain = v; }
export function setMasterLimiter(v) { masterLimiter = v; }
