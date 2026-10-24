// Settings > Recording: the two inputs (a microphone and an audio
// interface, each with its level), the latency test, and the camera.
//
// These are set once per machine rather than per take, so they live in
// Settings and the recording panel keeps only what changes from take to take
// (which lanes to mute, monitoring, camera on or off).
//
// While the tab is showing it holds its own microphone stream (for the level
// meter and the calibration) and its own camera stream (for the framing
// preview). Both are closed when the tab is left or Settings closes, so no
// device light stays on behind a closed dialog.
//
// One source of truth: device choices go through captureDevices.js, the
// calibration through latencyCalibration.js, camera size and mirror through
// captureSettings.js, under the same storage keys the recorders read. Every
// change is announced with SETTINGS_EVENT so an open recorder follows it.

import { t, applyTranslations } from "../i18n.js";
import { audioEngine } from "../state.js";
import { createPlaybackContext } from "../audioContext.js";
import { checkCaptureSupport, renderPermissionState, requestMedia } from "./capturePermission.js";
import { buildDevicePicker, getRememberedDevice } from "./captureDevices.js";
import { openCapture, meterFraction } from "./captureGraph.js";
import { buildRolePicker } from "./trackInputs.js";
import { runCalibration } from "./latencyCalibration.js";
import {
  announce, getCameraQuality, setCameraQuality, getMirrorPreview, setMirrorPreview, CAMERA_QUALITIES,
} from "./captureSettings.js";

// Same sizes as the camera recorder (video/videoCodec.js QUALITY).
const CAMERA_SIZE = { "720": [1280, 720], "1080": [1920, 1080] };

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function row(titleKey, descKey, control) {
  const r = el("div", "settings-row");
  const text = el("div", "settings-row-text");
  const title = el("div", "settings-row-title");
  title.dataset.i18n = titleKey;
  const desc = el("div", "settings-row-desc");
  desc.dataset.i18n = descKey;
  text.append(title, desc);
  r.append(text);
  if (control) r.append(control);
  return r;
}

function section(...children) {
  const s = el("div", "settings-section");
  s.append(...children);
  return s;
}

/**
 * Build the Recording pane inside `pane`. Returns { enter, leave, dispose }:
 * enter() when the tab is shown, leave() when another tab is picked or
 * Settings closes.
 */
export function buildRecordingPane(pane) {
  pane.replaceChildren();
  pane.classList.add("rec-settings");

  // ── Inputs: the microphone and the audio interface ──
  // Each has its picker, its level and its permission message, and is open
  // while the tab shows, so either can be checked by playing into it.
  const inputs = {};
  for (const [role, id] of [["voice", "setRecMic"], ["instrument", "setRecInterface"]]) {
    const sel = el("select", "settings-select rec-settings-select");
    sel.id = id;
    sel.dataset.i18nAriaLabel = role === "voice" ? "settings.rec.mic.title" : "settings.rec.iface.title";
    const meter = el("div", "rec-meter rec-settings-meter");
    meter.setAttribute("aria-hidden", "true");
    const fill = el("div", "rec-meter-fill");
    meter.append(fill);
    const permission = el("div", "rec-permission");
    permission.hidden = true;
    inputs[role] = { role, sel, meter, fill, permission, capture: null, opening: null, picker: null };
  }
  // Which input the latency test measures.
  const testSeg = el("div", "rec-settings-test");
  testSeg.setAttribute("role", "radiogroup");
  testSeg.dataset.i18nAriaLabel = "settings.rec.latency.which";
  const testBtns = ["instrument", "voice"].map((role) => {
    const b = el("button", "rec-seg-btn");
    b.type = "button";
    b.dataset.role = role;
    b.setAttribute("role", "radio");
    b.dataset.i18n = role === "voice" ? "settings.rec.mic.title" : "settings.rec.iface.title";
    testSeg.append(b);
    return b;
  });
  // Picked by the user, or null: then the interface, else the microphone.
  let testChoice = null;
  const testRole = () => testChoice
    || (inputs.instrument.sel.value || !inputs.voice.sel.value ? "instrument" : "voice");

  // ── Latency ──
  const latency = el("p", "rec-settings-latency");
  latency.id = "setRecLatency";
  const calBtn = el("button", "rec-small-btn primary");
  calBtn.type = "button";
  calBtn.id = "setRecCalibrate";
  calBtn.dataset.i18n = "settings.rec.latency.run";
  const calInstr = el("p", "rec-note");
  calInstr.dataset.i18n = "rec.cal.instructions";
  const calProgress = el("div", "rec-cal-progress");
  calProgress.setAttribute("aria-hidden", "true");
  const calBar = el("div", "rec-cal-bar");
  calProgress.append(calBar);
  const calStatus = el("p", "rec-cal-status");
  calStatus.id = "setRecCalStatus";
  calStatus.setAttribute("role", "status");
  calStatus.setAttribute("aria-live", "polite");

  // ── Camera ──
  const camSel = el("select", "settings-select rec-settings-select");
  camSel.id = "setRecCamera";
  camSel.dataset.i18nAriaLabel = "settings.rec.camera.title";
  const qualSel = el("select", "settings-select");
  qualSel.id = "setRecQuality";
  qualSel.dataset.i18nAriaLabel = "settings.rec.quality.title";
  for (const q of CAMERA_QUALITIES) {
    const o = el("option", null, `${q}p`);
    o.value = q;
    qualSel.append(o);
  }
  qualSel.value = getCameraQuality();
  const mirrorWrap = el("label", "settings-switch");
  const mirror = el("input");
  mirror.type = "checkbox";
  mirror.id = "setRecMirror";
  mirror.checked = getMirrorPreview();
  mirror.dataset.i18nAriaLabel = "settings.rec.mirror.title";
  const track = el("span", "settings-switch-track");
  track.append(el("span", "settings-switch-thumb"));
  mirrorWrap.append(mirror, track);
  const previewWrap = el("div", "vid-preview-wrap rec-settings-preview");
  const preview = el("video", "vid-preview");
  preview.id = "setRecPreview";
  preview.muted = true;
  preview.autoplay = true;
  preview.playsInline = true;
  preview.setAttribute("playsinline", "");
  preview.dataset.i18nAriaLabel = "video.previewAria";
  previewWrap.append(preview);
  const camStatus = el("div", "rec-permission");
  camStatus.hidden = true;

  pane.append(
    section(
      row("settings.rec.mic.title", "settings.rec.mic.desc", inputs.voice.sel), inputs.voice.meter, inputs.voice.permission,
      row("settings.rec.iface.title", "settings.rec.iface.desc", inputs.instrument.sel), inputs.instrument.meter, inputs.instrument.permission,
    ),
    section(row("settings.rec.latency.title", "settings.rec.latency.desc", calBtn), testSeg, latency, calInstr, calProgress, calStatus),
    section(
      row("settings.rec.camera.title", "settings.rec.camera.desc", camSel),
      row("settings.rec.quality.title", "settings.rec.quality.desc", qualSel),
      row("settings.rec.mirror.title", "settings.rec.mirror.desc", mirrorWrap),
      previewWrap,
      camStatus,
    ),
  );
  applyTranslations(pane);

  let active = false;
  let tempCtx = null;
  let raf = 0;
  let camStream = null;
  let camToken = 0;
  let calibrating = false;

  // ── Microphone ──

  function engineForCapture() {
    if (audioEngine?.audioContext) return audioEngine;
    // No track open: a context of our own, at the rate playback would use,
    // so a calibration made here is stored under the rate it will be read at.
    if (!tempCtx) tempCtx = createPlaybackContext(window.AudioContext || window.webkitAudioContext);
    return tempCtx ? { audioContext: tempCtx } : null;
  }

  function closeInput(inp) {
    inp.capture?.close();
    inp.capture = null;
    inp.fill.style.transform = "scaleX(0)";
    renderLatency();
  }

  /** Open a role's device, or nothing for None. Resolves the capture or null. */
  async function openInput(inp) {
    if (inp.capture && !inp.capture.closed) return inp.capture;
    if (inp.opening) return inp.opening;
    if (!inp.sel.value) { renderPermissionState(inp.permission, null, "audio"); return null; }
    const engine = engineForCapture();
    if (!engine) return null;
    if (engine.audioContext.state === "suspended") {
      engine.audioContext.resume().catch((e) => console.warn("[rec] resume failed:", e));
    }
    const deviceId = inp.sel.value;
    inp.opening = openCapture(engine, { deviceId }).then((res) => {
      inp.opening = null;
      renderPermissionState(inp.permission, res.reason, "audio");
      if (!res.capture) return null;
      // Closed meanwhile, or another device picked while this one opened.
      if (!active || inp.sel.value !== deviceId) { res.capture.close(); return null; }
      inp.capture = res.capture;
      inp.picker?.refresh();
      renderLatency();
      return inp.capture;
    });
    return inp.opening;
  }

  function renderLatency() {
    for (const b of testBtns) {
      const on = b.dataset.role === testRole();
      b.setAttribute("aria-checked", String(on));
      b.classList.toggle("active", on);
      b.disabled = !inputs[b.dataset.role].sel.value;
    }
    const capture = inputs[testRole()].capture;
    if (!capture) {
      latency.textContent = t("rec.latency.unknown");
      return;
    }
    const ms = (capture.roundTripSeconds * 1000).toFixed(1);
    latency.textContent = t(capture.calibrated ? "rec.latency.calibrated" : "rec.latency.estimated", { ms });
  }

  function meterLoop() {
    raf = 0;
    if (!active) return;
    for (const inp of Object.values(inputs)) {
      const peak = inp.capture && !inp.capture.closed ? inp.capture.level() : 0;
      inp.fill.style.transform = `scaleX(${meterFraction(peak).toFixed(3)})`;
      inp.fill.classList.toggle("clip", peak >= 0.99);
    }
    raf = requestAnimationFrame(meterLoop);
  }

  for (const inp of Object.values(inputs)) {
    inp.picker = buildRolePicker(inp.sel, inp.role, () => {
      announce("audioinput");
      closeInput(inp);
      if (active) openInput(inp);
    });
  }
  // The test measures the interface by default (a loop from its output to
  // its input is the usual way), the microphone when no interface is set.
  for (const b of testBtns) {
    b.addEventListener("click", () => {
      testChoice = b.dataset.role;
      calStatus.textContent = "";
      renderLatency();
    });
  }

  calBtn.addEventListener("click", async () => {
    if (calibrating) return;
    if (audioEngine?.isPlaying?.()) {
      calStatus.textContent = t("settings.rec.stopFirst");
      return;
    }
    const tested = inputs[testRole()];
    const c = await openInput(tested);
    if (!c) {
      calStatus.textContent = t("rec.cal.needInputSettings");
      return;
    }
    calibrating = true;
    calBtn.disabled = true;
    for (const inp of Object.values(inputs)) inp.sel.disabled = true;
    calBar.style.transform = "scaleX(0)";
    calStatus.textContent = t("rec.cal.running");
    try {
      const res = await runCalibration(c, {
        onProgress: (p) => { calBar.style.transform = `scaleX(${p.toFixed(3)})`; },
      });
      if (res.ok) {
        calStatus.textContent = t("rec.cal.done", { ms: res.roundTripMs.toFixed(1) });
        announce("calibration");
      } else {
        calStatus.textContent = t(res.reason === "too-quiet" ? "rec.cal.tooQuiet" : "rec.cal.inconsistent");
      }
    } catch (e) {
      console.warn("[rec] calibration failed:", e);
      calStatus.textContent = t("rec.cal.inconsistent");
    } finally {
      calibrating = false;
      calBtn.disabled = false;
      for (const inp of Object.values(inputs)) inp.picker.refresh();
      renderLatency();
    }
  });

  // ── Camera ──

  function stopCamera() {
    camToken++;
    camStream?.getTracks().forEach((tr) => tr.stop());
    camStream = null;
    preview.srcObject = null;
  }

  async function startCamera() {
    stopCamera();
    const token = camToken;
    const [w, h] = CAMERA_SIZE[qualSel.value] || CAMERA_SIZE["720"];
    const id = camSel.value || getRememberedDevice("videoinput");
    const { stream, reason } = await requestMedia("video", {
      ...(id ? { deviceId: { exact: id } } : {}),
      width: { ideal: w }, height: { ideal: h }, frameRate: { ideal: 30 },
    });
    if (token !== camToken || !active) {
      stream?.getTracks().forEach((tr) => tr.stop());
      return;
    }
    renderPermissionState(camStatus, reason, "video");
    if (!stream) return;
    camStream = stream;
    preview.srcObject = stream;
    preview.play?.().catch((e) => console.warn("[rec] preview play failed:", e));
    camPicker.refresh();
  }

  const camPicker = buildDevicePicker(camSel, "videoinput", () => {
    announce("videoinput");
    if (active) startCamera();
  });
  qualSel.addEventListener("change", () => {
    setCameraQuality(qualSel.value);
    if (active) startCamera();
  });
  const applyMirror = () => preview.classList.toggle("vid-mirrored", mirror.checked);
  mirror.addEventListener("change", () => {
    setMirrorPreview(mirror.checked);
    applyMirror();
  });
  applyMirror();

  return {
    enter() {
      if (active) return;
      active = true;
      calStatus.textContent = "";
      calBar.style.transform = "scaleX(0)";
      testChoice = null;
      renderLatency();
      const support = checkCaptureSupport();
      if (!support.ok) {
        for (const inp of Object.values(inputs)) renderPermissionState(inp.permission, support.reason, "audio");
        renderPermissionState(camStatus, support.reason, "video");
        return;
      }
      // After each picker has its devices: before that it has no value.
      for (const inp of Object.values(inputs)) {
        inp.picker.refresh().then(() => {
          renderLatency();
          if (active) openInput(inp);
        });
      }
      startCamera();
      raf = requestAnimationFrame(meterLoop);
    },
    leave() {
      if (!active) return;
      active = false;
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
      stopCamera();
      for (const inp of Object.values(inputs)) closeInput(inp);
      if (tempCtx) {
        tempCtx.close().catch(() => {});
        tempCtx = null;
      }
    },
    dispose() {
      this.leave();
      for (const inp of Object.values(inputs)) inp.picker.dispose();
      camPicker.dispose();
    },
  };
}

/**
 * Open Settings on the Recording tab. Used by the recording panel's link and
 * by the message Record shows when no usable input is chosen.
 */
export function openRecordingSettings() {
  let backdrop = document.querySelector(".library-editor-backdrop");
  if (!backdrop) {
    document.getElementById("settingsBtn")?.click();
    backdrop = document.querySelector(".library-editor-backdrop");
  }
  backdrop?.querySelector('.settings-tab[data-tab="recording"]')?.click();
}
