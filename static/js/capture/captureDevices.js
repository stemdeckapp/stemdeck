// Input device list, picker and the remembered choice, for microphones and
// cameras alike. The audio input picker is trackInputs.buildInputPicker
// (voice and instrument, friendly names); this one is the camera's. `kind` here is a MediaDeviceInfo kind: "audioinput" or
// "videoinput".
//
// The remembered deviceId lives in localStorage, per browser and per machine,
// which is what a deviceId is scoped to anyway. A missing or blocked store
// just means "the default device".

import { t } from "../i18n.js";
import { requestMedia } from "./capturePermission.js";

const KEY_PREFIX = "stemdeck:capture:device:";

export function getRememberedDevice(kind) {
  try {
    return localStorage.getItem(KEY_PREFIX + kind) || "";
  } catch (e) {
    console.warn("[capture] could not read the remembered device:", e);
    return "";
  }
}

export function rememberDevice(kind, deviceId) {
  try {
    if (deviceId) localStorage.setItem(KEY_PREFIX + kind, deviceId);
    else localStorage.removeItem(KEY_PREFIX + kind);
  } catch (e) {
    console.warn("[capture] could not remember the device:", e);
  }
}

/** Devices of one kind. Labels are empty until permission has been granted. */
export async function listDevices(kind) {
  if (!navigator.mediaDevices?.enumerateDevices) return [];
  try {
    const all = await navigator.mediaDevices.enumerateDevices();
    return all.filter((d) => d.kind === kind);
  } catch (e) {
    console.warn("[capture] enumerateDevices failed:", e);
    return [];
  }
}

// ── Device names ──
//
// Before the page may use a kind of device, the browser lists each device of
// that kind with no name and no id, so a picker has nothing to offer.

/** Devices of `kind` are connected but none has a name: the page has no permission for that kind yet. */
export function namesHidden(raw, kind = "audioinput") {
  const list = (raw || []).filter((d) => d.kind === kind);
  return list.length > 0 && list.every((d) => !d.label);
}

/**
 * Whether to open a kind of device once just to read the device names.
 *
 * The desktop app grants its own page the microphone and the camera without
 * asking (desktop/src-tauri/src/mediaperm.rs) but never saves the grant, so
 * every page load starts with nameless devices with no ids, and a picker
 * offered only None until something opened the device. A browser keeps the
 * user's grant per site, and opening the device there would mean a prompt at
 * an unexpected moment, so only the desktop app does this, and only once per
 * page and kind: a refusal (the operating system's privacy settings) is not
 * retried on every refresh.
 */
export function needsNameUnlock(raw, { desktop = false, tried = false, kind = "audioinput" } = {}) {
  if (!desktop || tried) return false;
  return namesHidden(raw, kind);
}

// Per kind: whether this page tried the unlock, the try itself (two pickers
// refresh at once, and the second waits for the first's answer rather than
// read the nameless list again), and why it failed (a capturePermission
// reason), so the picker can say it.
const unlockState = {
  audioinput: { tried: false, running: null, ok: false, reason: null },
  videoinput: { tried: false, running: null, ok: false, reason: null },
};

/** The desktop app, whose shell grants its own page the microphone and the camera. */
export function isDesktopApp() {
  return !!window.__TAURI__?.core?.invoke;
}

/** Resolves true when the names of `kind` may have changed (an unlock ran, here or in a caller before). */
async function unlockNames(kind, raw) {
  const st = unlockState[kind];
  if (!st) return false;
  if (st.running) return st.running;
  // Listed while another caller's unlock was opening the device: read again.
  if (st.ok && namesHidden(raw, kind)) return true;
  if (!needsNameUnlock(raw, { desktop: isDesktopApp(), tried: st.tried, kind })) return false;
  st.tried = true;
  st.running = (async () => {
    const { stream, reason } = await requestMedia(kind === "videoinput" ? "video" : "audio");
    if (!stream) {
      st.reason = reason || "unknown";
      console.warn(`[capture] could not open the ${kind} to read the device names:`, st.reason);
      return false;
    }
    stream.getTracks().forEach((tr) => tr.stop());
    st.ok = true;
    return true;
  })();
  try {
    return await st.running;
  } finally {
    st.running = null;
  }
}

/** Why the names of `kind` are hidden: the desktop app's refusal, a browser refusal, or no permission yet. */
async function hiddenReason(kind) {
  const failed = unlockState[kind]?.reason;
  if (failed) return failed;
  try {
    const p = await navigator.permissions?.query?.({ name: kind === "videoinput" ? "camera" : "microphone" });
    if (p?.state === "denied") return "denied";
  } catch {
    // Firefox and Safari do not know every permission name: not a refusal.
  }
  return "not-granted";
}

/**
 * Devices of one kind, with their names where the page may read them.
 * `unlock` lets the desktop app open the device once to read the names; pass
 * it only when a picker is about to be shown. Resolves
 * {devices, hidden, reason}: `hidden` when the names are still hidden,
 * `reason` (a capturePermission reason) why, else null.
 */
export async function listNamedDevices(kind, { unlock = false } = {}) {
  let raw = await listDevices(kind);
  if (unlock && (await unlockNames(kind, raw))) raw = await listDevices(kind);
  const hidden = namesHidden(raw, kind);
  return { devices: raw, hidden, reason: hidden ? await hiddenReason(kind) : null };
}

function labelFor(device, index, kind) {
  // Without the USB id Chromium appends on Windows: "(046d:0893)".
  const label = (device.label || "").replace(/\s*\([0-9a-f]{4}:[0-9a-f]{4}\)\s*$/i, "");
  if (label) return label;
  return t(kind === "videoinput" ? "capture.cameraN" : "capture.inputN", { n: index + 1 });
}

/**
 * Keep a <select> filled with the devices of one kind, selecting the
 * remembered one, and follow plugs and unplugs. `onChange(deviceId)` runs when
 * the user picks another device (not on refreshes).
 */
export function buildDevicePicker(selectEl, kind, onChange) {
  let disposed = false;
  async function refresh() {
    const devices = await listDevices(kind);
    if (disposed) return devices;
    const remembered = getRememberedDevice(kind);
    const current = selectEl.value || remembered;
    selectEl.replaceChildren();
    const def = document.createElement("option");
    def.value = "";
    def.textContent = t("capture.defaultDevice");
    selectEl.appendChild(def);
    devices.forEach((d, i) => {
      // "default" and "communications" are aliases Chromium adds on Windows;
      // the empty value already stands for the default.
      if (!d.deviceId || d.deviceId === "default" || d.deviceId === "communications") return;
      const o = document.createElement("option");
      o.value = d.deviceId;
      o.textContent = labelFor(d, i, kind);
      selectEl.appendChild(o);
    });
    const ids = new Set([...selectEl.options].map((o) => o.value));
    selectEl.value = ids.has(current) ? current : "";
    selectEl.disabled = devices.length === 0;
    return devices;
  }
  const onPick = () => {
    rememberDevice(kind, selectEl.value);
    onChange?.(selectEl.value);
  };
  selectEl.addEventListener("change", onPick);
  const onDeviceChange = () => { refresh(); };
  navigator.mediaDevices?.addEventListener?.("devicechange", onDeviceChange);
  refresh();
  return {
    refresh,
    dispose() {
      disposed = true;
      selectEl.removeEventListener("change", onPick);
      navigator.mediaDevices?.removeEventListener?.("devicechange", onDeviceChange);
    },
  };
}
