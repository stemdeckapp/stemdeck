// The studio's Record button and recording panel.
//
// Record records into the armed recording tracks (takeLanes.js), one new
// take each, all from one input device split by channel. It opens the input
// on first use (the permission prompt), then starts the transport through its
// normal play path, count-in included. Press again to stop. Each take is
// saved at once and becomes its track's active take, so it can be heard, and
// the panel asks to keep or discard what was just recorded.
//
// Arming a track opens its input, as a DAW does: the track's meter then
// shows what its own channels receive (Input 1 of an interface, not the
// whole device), and "Hear myself" plays those channels, a mono input in the
// centre. The input closes again when nothing is armed and the panel is shut.
//
// With nothing armed: if the song has exactly one recording track, Record
// arms it and records (the obvious intent, one press). With none, or several,
// it says what to do instead of guessing: add a track, or arm one.
//
// The panel holds what changes from take to take: a level meter, the
// monitoring switch (off by default, with the headphones note), which lanes
// to mute while recording, and the slot the video recorder fills
// (#videoPanel). The input, the camera and the latency test are set once per
// machine, in Settings > Recording (recordingSettings.js), which this panel
// links to.

import { t, onLanguageChange } from "../i18n.js";
import { audioEngine, currentJobId, playbackSpeed, loopEnabled, mixerState, trackIndex } from "../state.js";
import { laneLabel } from "../constants.js";
import { applyMix, refreshMixerVisuals } from "../mixer.js";
import { togglePlayPause, toggleLoop } from "../transport.js";
import { checkCaptureSupport, renderPermissionState } from "./capturePermission.js";
import { openCapture, openSilentCapture, meterFraction } from "./captureGraph.js";
import { loadCalibration } from "./latencyCalibration.js";
import { SETTINGS_EVENT } from "./captureSettings.js";
import { openRecordingSettings } from "./recordingSettings.js";
import { recordBlockReason } from "./takeMath.js";
import { startTake, onTakeEvent } from "./takeRecorder.js";
import {
  requestTakesReload, armedTracks, getTracks, setArmed, trackInput, addTrack, initTrackControls,
  refreshTrackInputs, showTrackNote, autoPickChannel,
} from "./takeLanes.js";
import { channelsNeeded, defaultInputId } from "./trackInputs.js";

const $ = (id) => document.getElementById(id);

let capture = null;
let captureWant = null; // { deviceId, channelCount } the open capture was asked for
let opening = null;
// The takes being recorded together, one per armed track.
let group = null;
let reviewSessions = [];
let meterRaf = 0;
let mutedForTake = [];
let unwatchLost = null;
let startPos = 0;
// Set once an input has opened while the panel is up. A take reloads the
// lanes, which replaces the engine and its AudioContext; this is what brings
// the input back on the new one without another click.
let wantCapture = false;
// The input the armed tracks asked for could not be opened (not connected,
// refused): no retry until the armed tracks or their inputs change.
let armBlocked = null;
let armRaf = 0;
// Per armed mono track: how long its input has been silent while another
// input of the device had signal (autoPick).
const silentRuns = new Map();
// Below this an input is silent (-60 dBFS); above this it carries a source
// (-40 dBFS). Frames at about 60 a second: one second of both.
const AUTO_SILENT = 0.001;
const AUTO_SIGNAL = 0.01;
const AUTO_FRAMES = 60;

const MUTE_KEY = (jobId) => `stemdeck:rec:mute:${jobId}`;

function els() {
  return {
    btn: $("t-rec"),
    optsBtn: $("t-rec-opts"),
    panel: $("recPanel"),
    permission: $("recPermission"),
    meter: $("recMeterFill"),
    settingsLink: $("recSettingsLink"),
    monitor: $("recMonitor"),
    monitorDelay: $("recMonitorDelay"),
    muteList: $("recMuteList"),
    review: $("recReview"),
    reviewText: $("recReviewText"),
    keep: $("recKeep"),
    discard: $("recDiscard"),
    status: $("recStatus"),
  };
}

function setStatus(key, vars) {
  const s = $("recStatus");
  if (s) s.textContent = key ? t(key, vars) : "";
}

function settingsLinkButton() {
  const a = document.createElement("button");
  a.type = "button";
  a.className = "rec-link";
  a.textContent = t("rec.openSettings");
  a.addEventListener("click", goToSettings);
  return a;
}

function goToSettings() {
  closePanel();
  openRecordingSettings();
}

/** Why the input did not open, plus a way to choose another when there is none. */
function renderInputProblem(reason) {
  const el = $("recPermission");
  renderPermissionState(el, reason, "audio");
  if (reason === "no-device") el?.appendChild(settingsLinkButton());
}

// ─── Capture lifecycle ───

/** The open capture, if it still belongs to the engine that is playing now. */
function liveCapture() {
  if (capture && !capture.closed && audioEngine && capture.engine === audioEngine) return capture;
  return null;
}

/**
 * The armed tracks that record sound. A video track is picture only: it
 * never opens an input, so it is left out of everything about the input
 * (which device opens, the meters, monitoring).
 */
function audioArmed() {
  return armedTracks().filter((tr) => tr.kind !== "video");
}

/**
 * Whether the open panel should open the input for its meter: with nothing
 * armed (the Settings default device), or with an audio track armed. With
 * only video tracks armed, nothing is heard, so no microphone opens.
 */
function panelWantsInput() {
  return armedTracks().length === 0 || audioArmed().length > 0;
}

/**
 * What the input should be opened as: the armed tracks' device with enough
 * channels for all of them, or (nothing armed) the Settings default device.
 */
function wantedInput() {
  const armed = audioArmed();
  if (armed.length) {
    const inputs = armed.map(trackInput);
    // At least the pair, so a track on one input can see where the signal
    // is (autoPick) on a two-input interface.
    return { deviceId: inputs[0].device_id || "", channelCount: Math.max(2, channelsNeeded(inputs)) };
  }
  return { deviceId: defaultInputId(), channelCount: 2 };
}

function captureMatches(want) {
  return !!captureWant && captureWant.deviceId === want.deviceId && captureWant.channelCount >= want.channelCount;
}

function closeCapture() {
  unwatchLost?.();
  unwatchLost = null;
  capture?.close();
  capture = null;
  captureWant = null;
  renderLatency();
}

/** Open the input on the current engine, reusing it when it is still live. */
async function ensureCapture(want = wantedInput()) {
  const live = liveCapture();
  if (live && captureMatches(want)) return { capture: live, reason: null };
  if (opening) return opening;
  // A capture on a previous song's context is dead weight: that context is
  // being closed. Reopen on this one (the browser does not ask again). A
  // capture of another device, or of too few channels, reopens too.
  closeCapture();
  const engine = audioEngine;
  const { deviceId, channelCount } = want;
  // Only a placeholder: a message already on screen (why Record refused)
  // is not replaced by it, and it clears itself only if still showing.
  const statusEl = $("recStatus");
  const showOpening = statusEl && !statusEl.textContent;
  if (showOpening) setStatus("rec.status.opening");
  opening = openCapture(engine, { deviceId, channelCount }).then((res) => {
    opening = null;
    if (showOpening && statusEl.textContent === t("rec.status.opening")) setStatus("");
    renderInputProblem(res.reason);
    if (!res.capture) return res;
    if (audioEngine !== engine) {
      res.capture.close();
      return { capture: null, reason: "unknown" };
    }
    if (deviceId && res.capture.fellBack && audioArmed().length) {
      // The armed track's device is gone and the browser opened another:
      // a meter or a monitor on the wrong input would mislead.
      res.capture.close();
      armBlocked = deviceId;
      showTrackNote(t("rec.status.trackInputMissing", { name: audioArmed()[0]?.name || "" }));
      return { capture: null, reason: null, missing: true };
    }
    capture = res.capture;
    captureWant = { deviceId, channelCount };
    wantCapture = true;
    routeMonitor(capture);
    // Device names and channel counts are readable once permission is given.
    refreshTrackInputs();
    capture.setMonitor(!!$("recMonitor")?.checked);
    unwatchLost = capture.onDeviceLost(onDeviceLost);
    renderLatency();
    startMeter();
    startArmMeter();
    return res;
  });
  return opening;
}

/** Monitoring plays what the armed tracks record; with none armed, the whole device. */
function routeMonitor(c) {
  if (!c || c.closed) return;
  const armed = audioArmed();
  c.setMonitorChannels(armed.length ? armed.map((tr) => trackInput(tr).channels) : null);
}

/**
 * The armed tracks' meters: each shows the peak of its own channels, on a
 * decibel scale. Runs while any track is armed, and opens the input again
 * when a reload replaced the engine.
 */
function startArmMeter() {
  if (armRaf) return;
  const loop = () => {
    armRaf = 0;
    const armed = audioArmed();
    const rows = document.querySelectorAll(".lane-header.track-lane .lane-vu");
    if (!armed.length) {
      for (const vu of rows) vu.style.removeProperty("--vu-in");
      return;
    }
    let c = liveCapture();
    if (!c && audioEngine && !opening && !group) {
      const want = wantedInput();
      // One try per armed set: a refused or missing input is not asked for
      // again every frame (arming again, or another input, clears this).
      if (armBlocked !== want.deviceId) {
        ensureCapture(want).then((res) => { if (!res?.capture) armBlocked = want.deviceId; });
      }
    }
    if (c && !captureMatches(wantedInput()) && !group && !opening) {
      closeCapture();
      c = null;
    }
    const ids = new Set(armed.map((tr) => tr.id));
    for (const vu of rows) {
      const id = vu.dataset.stem;
      if (!ids.has(id)) { vu.style.removeProperty("--vu-in"); continue; }
      const track = armed.find((tr) => tr.id === id);
      const peak = c ? c.channelLevel(trackInput(track).channels) : 0;
      vu.style.setProperty("--vu-in", `${Math.round(meterFraction(peak) * 100)}%`);
      autoPick(c, track, peak);
    }
    for (const id of silentRuns.keys()) if (!ids.has(id)) silentRuns.delete(id);
    armRaf = requestAnimationFrame(loop);
  };
  armRaf = requestAnimationFrame(loop);
}

/**
 * An armed track on one input that stays silent while another input of the
 * same device carries a source (a drum loop on input 2, the track on input
 * 1) moves to that input, once, after a second of both. Not while
 * recording, and never a track whose input the user picked (takeLanes.js).
 */
function autoPick(c, track, peak) {
  const chans = trackInput(track).channels;
  const n = c?.deviceChannels || 0;
  if (!c || group || chans.length !== 1 || n < 2 || peak >= AUTO_SILENT) {
    silentRuns.delete(track.id);
    return;
  }
  let best = -1;
  let bestPeak = AUTO_SIGNAL;
  for (let ch = 0; ch < n; ch++) {
    if (ch === chans[0]) continue;
    const p = c.channelLevel([ch]);
    if (p > bestPeak) { best = ch; bestPeak = p; }
  }
  const run = best < 0 ? 0 : (silentRuns.get(track.id)?.ch === best ? silentRuns.get(track.id).frames + 1 : 1);
  if (best < 0) { silentRuns.delete(track.id); return; }
  if (run < AUTO_FRAMES) { silentRuns.set(track.id, { ch: best, frames: run }); return; }
  silentRuns.delete(track.id);
  autoPickChannel(track.id, best).then((moved) => { if (moved) routeMonitor(liveCapture()); });
}

function onDeviceLost() {
  if (isRecording()) {
    setStatus("rec.status.deviceLost");
    stopRecording();
  } else {
    setStatus("rec.status.inputLost");
  }
  closeCapture();
}

// ─── Panel ───

function panelOpen() {
  return !!$("recPanel") && !$("recPanel").hidden;
}

function placePanel() {
  const { optsBtn, panel } = els();
  if (!optsBtn || !panel) return;
  const r = optsBtn.getBoundingClientRect();
  const w = panel.offsetWidth;
  panel.style.left = `${Math.max(8, Math.min(Math.round(r.left), window.innerWidth - w - 8))}px`;
  panel.style.top = `${Math.max(8, Math.round(r.top - panel.offsetHeight - 8))}px`;
}

function openPanel() {
  const { optsBtn, panel } = els();
  if (!panel) return;
  panel.hidden = false;
  optsBtn?.setAttribute("aria-expanded", "true");
  renderMuteList();
  renderLatency();
  const support = checkCaptureSupport();
  if (!support.ok) renderPermissionState($("recPermission"), support.reason, "audio");
  placePanel();
  // Opening the panel is the moment to show a live meter and real device
  // names, both of which need the input open.
  if (support.ok && audioEngine && panelWantsInput()) ensureCapture().then(placePanel);
  startMeter();
}

function closePanel() {
  const { optsBtn, panel } = els();
  if (!panel || panel.hidden) return;
  panel.hidden = true;
  optsBtn?.setAttribute("aria-expanded", "false");
  stopMeter();
  wantCapture = false;
  // The input stays open only while it is needed (a take, an armed track):
  // the browser shows a recording indicator for as long as it is.
  if (!group && !armedTracks().length) closeCapture();
}

function startMeter() {
  if (meterRaf || !panelOpen()) return;
  const fill = $("recMeterFill");
  const loop = () => {
    meterRaf = 0;
    if (!panelOpen()) return;
    const c = liveCapture();
    if (!c && wantCapture && audioEngine && !opening && !group && panelWantsInput()) ensureCapture();
    // With tracks armed, what they record; else the whole device. A video
    // track records no sound, so it adds nothing to the meter.
    const armed = audioArmed();
    const peak = !c ? 0 : armed.length
      ? Math.max(...armed.map((tr) => c.channelLevel(trackInput(tr).channels)))
      : c.level();
    if (fill) {
      fill.style.transform = `scaleX(${meterFraction(peak).toFixed(3)})`;
      fill.classList.toggle("clip", peak >= 0.99);
    }
    meterRaf = requestAnimationFrame(loop);
  };
  meterRaf = requestAnimationFrame(loop);
}

function stopMeter() {
  if (meterRaf) cancelAnimationFrame(meterRaf);
  meterRaf = 0;
}

function renderLatency() {
  const { monitorDelay } = els();
  const c = liveCapture();
  if (monitorDelay) {
    monitorDelay.textContent = c ? t("rec.monitorDelay", { ms: Math.round(c.roundTripSeconds * 1000) }) : "";
  }
}

function loadMuteChoice(jobId) {
  try {
    const raw = localStorage.getItem(MUTE_KEY(jobId));
    const v = raw ? JSON.parse(raw) : [];
    return Array.isArray(v) ? v : [];
  } catch (e) {
    console.warn("[rec] could not read the mute choice:", e);
    return [];
  }
}

function saveMuteChoice(jobId, names) {
  try {
    localStorage.setItem(MUTE_KEY(jobId), JSON.stringify(names));
  } catch (e) {
    console.warn("[rec] could not store the mute choice:", e);
  }
}

function laneNamesInOrder() {
  return Object.entries(trackIndex || {})
    .sort((a, b) => a[1] - b[1])
    .map(([name]) => name);
}

function renderMuteList() {
  const list = $("recMuteList");
  if (!list) return;
  list.replaceChildren();
  const jobId = currentJobId;
  if (!jobId) return;
  const chosen = new Set(loadMuteChoice(jobId));
  for (const name of laneNamesInOrder()) {
    const label = document.createElement("label");
    label.className = "rec-mute-item";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = name;
    box.checked = chosen.has(name);
    box.addEventListener("change", () => {
      const names = [...list.querySelectorAll("input:checked")].map((b) => b.value);
      saveMuteChoice(jobId, names);
    });
    const span = document.createElement("span");
    span.textContent = laneLabel(name);
    label.append(box, span);
    list.appendChild(label);
  }
}

// Mutes for the take are applied to the live mix only and never saved: the
// lanes reload from the stored mix after the take, which puts them back.
function applyTakeMutes(jobId) {
  mutedForTake = [];
  for (const name of loadMuteChoice(jobId)) {
    const st = mixerState[name];
    if (!st || st.muted || trackIndex[name] === undefined) continue;
    st.muted = true;
    mutedForTake.push(name);
  }
  if (mutedForTake.length) { refreshMixerVisuals(); applyMix(); }
}

function restoreTakeMutes() {
  for (const name of mutedForTake) {
    if (mixerState[name]) mixerState[name].muted = false;
  }
  if (mutedForTake.length) { refreshMixerVisuals(); applyMix(); }
  mutedForTake = [];
}

// ─── Record ───

const RECORDING = new Set(["starting", "recording"]);

function groupState() {
  if (!group) return "idle";
  if (group.sessions.some((s) => RECORDING.has(s.state))) return "recording";
  if (group.sessions.some((s) => s.state === "stopping")) return "stopping";
  return "done";
}

function renderRecButton() {
  const btn = $("t-rec");
  if (!btn) return;
  const state = groupState();
  const active = state === "recording";
  const saving = state === "stopping";
  btn.classList.toggle("recording", active);
  btn.classList.toggle("saving", saving);
  btn.setAttribute("aria-pressed", String(active));
  const key = active ? "rec.stopRecording" : "rec.record";
  btn.setAttribute("aria-label", t(key));
  btn.title = t(active ? "rec.stopRecording" : "rec.recordTitle");
  btn.disabled = saving;
}

/** Close the recording panel (its camera and input go with it): mode.js
 * Practice has no recording. */
export function closeRecordPanel() {
  closePanel();
}

export function isRecording() {
  return groupState() === "recording";
}

/** A status line ending in an "Add track" button. */
function setStatusWithAddTrack(key) {
  const s = $("recStatus");
  if (!s) return;
  s.textContent = `${t(key)} `;
  const b = document.createElement("button");
  b.type = "button";
  b.className = "rec-link";
  b.textContent = t("rec.track.add");
  b.addEventListener("click", async () => {
    setStatus("");
    const track = await addTrack();
    if (track?.id) setStatus("rec.status.trackAdded", { name: track.name || "" });
  });
  s.appendChild(b);
}

/**
 * The tracks Record records into: the armed ones. With none armed and exactly
 * one recording track, that one is armed (one press does the obvious thing).
 * With none at all, or several, null, and the panel says what to do.
 */
function tracksToRecord() {
  const armed = armedTracks();
  if (armed.length) return armed;
  const tracks = getTracks();
  if (tracks.length === 1) {
    setArmed(tracks[0].id, true);
    return armedTracks();
  }
  openPanel();
  if (!tracks.length) setStatusWithAddTrack("rec.status.addTrack");
  else setStatus("rec.status.armTrack");
  return null;
}

// How long Record waits for the engine of a song that is still opening.
const ENGINE_WAIT_MS = 15_000;

/**
 * The engine a take records into: the current one, once it is ready. A song
 * that has just opened may have no engine yet, or can still replace it (the
 * streaming engine falls back to the full decode when it cannot read a
 * stem), and a take bound to the engine being replaced records into a
 * context that closes under it. Null when no engine comes up in time.
 */
async function settledEngine(jobId) {
  const until = Date.now() + ENGINE_WAIT_MS;
  for (let i = 0; i < 4; i++) {
    while (!audioEngine && currentJobId === jobId && Date.now() < until) {
      await new Promise((r) => setTimeout(r, 50));
    }
    const eng = audioEngine;
    if (!eng || currentJobId !== jobId) return null;
    let ok = false;
    try {
      ok = await eng.ready;
    } catch (e) {
      console.warn("[rec] the audio engine did not start:", e);
    }
    if (audioEngine === eng) return ok && !eng.isDestroyed?.() ? eng : null;
  }
  return null;
}

// Set while a press of Record waits for the engine or the input, so a second
// press in that moment does not start a second set of takes.
let recordPending = false;

async function onRecordClick() {
  if (isRecording()) { stopRecording(); return; }
  if (groupState() === "stopping" || recordPending) return;
  recordPending = true;
  try {
    await startRecording();
  } finally {
    recordPending = false;
  }
}

async function startRecording() {
  const first = audioEngine;
  const jobId = currentJobId;
  // An open song whose engine is still coming up counts: Record waits for it.
  const block = recordBlockReason({ hasEngine: !!jobId, rate: playbackSpeed });
  if (block) {
    setStatus(block === "tempo" ? "rec.status.tempo" : "rec.status.noEngine");
    openPanel();
    return;
  }
  const support = checkCaptureSupport();
  if (!support.ok) {
    openPanel();
    renderPermissionState($("recPermission"), support.reason, "audio");
    return;
  }
  // The context must be running before the first await: Safari and WebKit
  // only allow resume() inside the user's gesture.
  const ctx = first?.audioContext;
  if (ctx?.state === "suspended") ctx.resume().catch((e) => console.warn("[rec] resume failed:", e));
  if (!first) setStatus("rec.status.waitingSong");
  const eng = await settledEngine(jobId);
  if ($("recStatus")?.textContent === t("rec.status.waitingSong")) setStatus("");
  if (!eng) {
    setStatus("rec.status.noEngine");
    openPanel();
    return;
  }
  if (eng !== first && eng.audioContext?.state === "suspended") {
    eng.audioContext.resume().catch((e) => console.warn("[rec] resume failed:", e));
  }

  const armed = tracksToRecord();
  if (!armed) return;
  // A video track records first: the camera films the first track's take.
  const tracks = [...armed].sort((a, b) => (b.kind === "video") - (a.kind === "video"));
  if (tracks[0].kind === "video") {
    // The camera's preview and its REC badge are in the panel.
    openPanel();
    let cameraReady = false;
    try {
      cameraReady = await (await import("../video/videoTake.js")).ensureCameraOn();
    } catch (e) {
      console.warn("[rec] opening the camera failed:", e);
    }
    if (!cameraReady) {
      setStatus("rec.status.cameraFailed");
      return;
    }
    if (audioEngine !== eng) return;
  }
  // Sound comes from the audio tracks' input; a video track records none.
  const audioTracks = tracks.filter((tr) => tr.kind !== "video");
  const inputs = new Map(audioTracks.map((tr) => [tr.id, trackInput(tr)]));
  // A track's device is not connected: say which, rather than record from
  // whatever the browser would open instead.
  const gone = audioTracks.find((tr) => inputs.get(tr.id).missing);
  if (gone) {
    openPanel();
    setStatus("rec.status.trackInputMissing", { name: gone.name });
    return;
  }
  let c = null;
  if (audioTracks.length) {
    const first = inputs.get(audioTracks[0].id);
    const want = { deviceId: first.device_id || "", channelCount: channelsNeeded([...inputs.values()]) };
    const res = await ensureCapture(want);
    c = res.capture;
    if (!c) {
      openPanel();
      if (res.missing) setStatus("rec.status.trackInputMissing", { name: audioTracks[0].name });
      else renderInputProblem(res.reason);
      return;
    }
    // The device chosen for the track is not connected, and the browser
    // opened another one instead: recording that would put the wrong input
    // on the track without a word.
    if (want.deviceId && c.fellBack) {
      closeCapture();
      openPanel();
      setStatus("rec.status.trackInputMissing", { name: audioTracks[0].name });
      return;
    }
  }
  let silent = null;
  if (audioTracks.length < tracks.length) {
    const res = await openSilentCapture(eng);
    silent = res.capture;
    if (!silent) {
      openPanel();
      setStatus("rec.status.failed");
      return;
    }
  }
  if (audioEngine !== eng) { silent?.close(); return; }
  beginTakes(eng, { audio: c, silent }, tracks, inputs);
}

function beginTakes(eng, caps, tracks, inputs) {
  const jobId = currentJobId;
  // Loop recording is not in this version: one pass, one take per track.
  if (loopEnabled) toggleLoop();
  // Start from where the playhead is, and stand still first.
  if (eng.isPlaying()) togglePlayPause();
  startPos = eng.getCurrentTime?.() ?? 0;
  reviewSessions = [];
  renderReview();
  applyTakeMutes(jobId);
  setStatus("rec.status.recording");
  const recorders = [];
  const sessions = tracks.map((track, i) => {
    const video = track.kind === "video";
    const cap = video ? caps.silent : caps.audio;
    const recorder = cap.createRecorder(video ? [0] : inputs.get(track.id).channels);
    recorders.push(recorder);
    const first = i === 0;
    return startTake({
      engine: eng,
      capture: cap,
      jobId,
      trackId: track.id,
      recorder,
      primary: first,
      startSongSec: startPos,
      // The transport starts once, from the first track's frame 0; every
      // take stamps its own frame 0, so the others are placed exactly too.
      startPlayback: first ? () => { if (!eng.isPlaying()) togglePlayPause(); renderRecButton(); } : () => {},
      stopPlayback: first ? () => { if (audioEngine === eng && eng.isPlaying()) togglePlayPause(); } : () => {},
      onAutoStop: (why) => {
        // The engine went away under the take (the song reloaded): whatever
        // plays now is not being recorded, so it stops too.
        if (why === "engine" && audioEngine && audioEngine !== eng && audioEngine.isPlaying()) togglePlayPause();
        renderRecButton();
      },
    });
  });
  group = { sessions, recorders, jobId, tracks, silent: caps.silent };
  // A live waveform of a video track would draw its silence.
  for (const track of tracks) if (track.kind !== "video") mountLive(track.id);
  renderRecButton();
  watchGroup(group, eng);
}

// The live waveform (static/js/capture/liveWave.js) draws what is being
// recorded on the track's own lane, anchored to the lane's .take-live-slot. It
// is optional: without the module, recording works the same, undrawn. It
// removes itself once the saved take's waveform is drawn, or at once when a
// take is discarded or fails; takeLanes.js unmounts it on a job change or a
// deleted track.
function mountLive(trackId) {
  // The lane shows the take being recorded, not the one it replaces: the
  // previous take's bars are hidden under the live ones. The reload after
  // Stop rebuilds the lane (and drops this class) with the new take's bars.
  document.querySelector(`.stem-waveform-row[data-stem="${trackId}"]`)?.classList.add("take-recording");
  // No slot yet (lanes still loading): liveWave.js anchors to the track's
  // overview row until the slot exists.
  const slot = document.querySelector(`.take-live-slot[data-track-id="${trackId}"]`) || null;
  import("./liveWave.js")
    .then((m) => m.mountLiveWave?.(trackId, slot))
    .catch(() => { /* this build has no live waveform module */ });
}

async function watchGroup(g, eng) {
  // Takes that stop themselves (end of song, a seek, a pause) go through the
  // same path as pressing Stop; this waits for whichever comes first.
  await Promise.all(g.sessions.map((s) => s.ready));
  const poll = () => new Promise((r) => setTimeout(r, 120));
  while (g.sessions.some((s) => RECORDING.has(s.state))) {
    renderRecButton();
    await poll();
  }
  renderRecButton();
  if (g.sessions.some((s) => s.state === "stopping")) setStatus("rec.status.saving");
  while (g.sessions.some((s) => s.state === "stopping")) await poll();
  onGroupDone(g, eng);
}

async function stopRecording() {
  const g = group;
  if (!g) return;
  setStatus("rec.status.saving");
  await Promise.all(g.sessions.map((s) => s.stop()));
}

function onGroupDone(g, eng) {
  if (group !== g) return;
  group = null;
  // A track whose take was not saved gets no reload: show its previous take
  // again.
  g.sessions.forEach((sess, i) => {
    if (sess.state === "review") return;
    const id = g.tracks[i]?.id;
    if (id) document.querySelector(`.stem-waveform-row[data-stem="${id}"]`)?.classList.remove("take-recording");
  });
  for (const r of g.recorders) r.dispose();
  g.silent?.close();
  restoreTakeMutes();
  if (audioEngine === eng && !eng.isPlaying()) {
    try { eng.setTime?.(startPos); } catch (e) { console.warn(e); }
  }
  renderRecButton();
  const saved = g.sessions.filter((s) => s.state === "review");
  const failed = g.sessions.filter((s) => s.state === "failed");
  if (saved.length) {
    reviewSessions = saved.map((s, i) => Object.assign(s, { trackName: g.tracks[g.sessions.indexOf(s)]?.name || "" }));
    // Something went wrong but the audio is saved: say what, and what to do.
    const note = failed.length ? "rec.status.someFailed"
      : saved.some((s) => s.placement === "estimated") ? "rec.status.unplaced"
        : saved.some((s) => s.engineLost) ? "rec.status.engineLost"
          : "";
    setStatus(note);
    renderReview();
    openPanel();
    // Each take is its track's active take now; reload so it can be heard
    // before deciding.
    requestTakesReload(g.jobId);
  } else if (failed.length) {
    const s = failed[0];
    const why = s.error?.status === 409 ? "rec.status.tooMany" : null;
    setStatus(why || (s.frames && !s.mapping ? "rec.status.tooShort" : "rec.status.failed"));
    openPanel();
  }
  if (!panelOpen() && !armedTracks().length) closeCapture();
}

function renderReview() {
  const { review, reviewText } = els();
  if (!review) return;
  const list = reviewSessions;
  review.hidden = !list.length;
  if (!list.length || !reviewText) return;
  reviewText.textContent = list.length === 1
    ? t("rec.review.textTrack", { take: list[0].entry?.label || list[0].takeId, track: list[0].trackName })
    : t("rec.review.textMany", { count: list.length });
}

async function onKeep() {
  const list = reviewSessions;
  if (!list.length) return;
  await Promise.all(list.map((s) => s.keep()));
  reviewSessions = [];
  renderReview();
  setStatus("rec.status.kept");
}

async function onDiscard() {
  const list = reviewSessions;
  if (!list.length) return;
  await Promise.all(list.map((s) => s.discard()));
  const left = list.filter((s) => s.state !== "discarded");
  reviewSessions = left;
  renderReview();
  setStatus(left.length ? "rec.status.failed" : "rec.status.discarded");
  // The track falls back to its previous take (or none).
  requestTakesReload(list[0].jobId);
}

// Settings > Recording changed something the open input depends on.
function onSettingsChanged(ev) {
  const kind = ev.detail?.kind;
  if (kind === "audioinput") {
    // A new device means a new stream. A take in progress keeps its input.
    if (isRecording()) return;
    closeCapture();
    if (panelOpen() && panelWantsInput()) ensureCapture().then(placePanel);
    refreshTrackInputs();
  } else if (kind === "calibration") {
    const c = liveCapture();
    if (c) c.applyCalibration(loadCalibration(c.deviceId, c.sampleRate));
    renderLatency();
  }
}

// ─── Wiring ───

export function initRecordUi() {
  const e = els();
  if (!e.btn || !e.panel) return;
  e.btn.addEventListener("click", (ev) => { ev.stopPropagation(); onRecordClick(); });
  e.optsBtn?.addEventListener("click", (ev) => {
    ev.stopPropagation();
    if (panelOpen()) closePanel(); else openPanel();
  });
  // Loaded now, so it listens before a first take's first batch arrives.
  import("./liveWave.js").catch((err) => console.warn("[rec] live waveform unavailable:", err));
  e.panel.addEventListener("click", (ev) => ev.stopPropagation());
  e.panel.addEventListener("pointerdown", (ev) => ev.stopPropagation());
  // Only a real click outside closes the panel. A script's click, such as the
  // hidden <a download> that saves a file, bubbles here too; without this the
  // panel closed under the "Download MP4" button that started it.
  document.addEventListener("click", (ev) => { if (ev.isTrusted) closePanel(); });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && panelOpen()) {
      closePanel();
      e.optsBtn?.focus();
    }
  });
  window.addEventListener("resize", () => { if (panelOpen()) placePanel(); });
  // The panel grows while open (the review, the camera preview, the takes
  // list), and it is anchored by its bottom edge above the button: place it
  // again whenever its size changes, or it runs off the bottom of the window.
  if (typeof ResizeObserver === "function") {
    new ResizeObserver(() => { if (panelOpen()) placePanel(); }).observe(e.panel);
  }

  e.settingsLink?.addEventListener("click", goToSettings);
  document.addEventListener(SETTINGS_EVENT, onSettingsChanged);
  e.monitor?.addEventListener("change", () => {
    liveCapture()?.setMonitor(e.monitor.checked);
    e.panel.classList.toggle("monitoring", e.monitor.checked);
  });
  e.keep?.addEventListener("click", onKeep);
  e.discard?.addEventListener("click", onDiscard);

  // A take kept or discarded elsewhere (another module) still settles here.
  onTakeEvent((ev) => {
    if ((ev.type === "finished" || ev.type === "discarded") && reviewSessions.some((s) => s.takeId === ev.takeId)) {
      reviewSessions = reviewSessions.filter((s) => s.takeId !== ev.takeId);
      renderReview();
    }
  });
  initTrackControls();
  // Arming a track on another device, or changing an armed track's input,
  // changes what the input should be: reopen it while the panel shows.
  document.addEventListener("stemdeck:tracks-armed", () => {
    if (isRecording()) return;
    armBlocked = null;
    // Arming is a click: the moment a suspended context may start.
    const ctx = audioEngine?.audioContext;
    if (ctx?.state === "suspended") ctx.resume().catch((e) => console.warn("[rec] resume failed:", e));
    const live = liveCapture();
    if (live && !captureMatches(wantedInput())) closeCapture();
    else if (live) routeMonitor(live);
    const armed = audioArmed().length;
    if (armed && audioEngine && checkCaptureSupport().ok) {
      ensureCapture().then(() => { if (panelOpen()) placePanel(); });
      startArmMeter();
    } else if (!armed && (!panelOpen() || !panelWantsInput()) && !group) {
      // Nothing that records sound is armed: the input closes (only video
      // tracks armed, or none and the panel shut).
      closeCapture();
    } else if (panelOpen()) {
      ensureCapture().then(placePanel);
    }
  });
  onLanguageChange(() => {
    renderRecButton();
    renderLatency();
    renderReview();
    if (panelOpen()) renderMuteList();
  });
  renderRecButton();
  renderLatency();
}
