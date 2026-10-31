// Input device list, picker and the remembered choice, for microphones and
// cameras alike. The audio input picker is trackInputs.buildInputPicker
// (voice and instrument, friendly names); this one is the camera's. `kind` here is a MediaDeviceInfo kind: "audioinput" or
// "videoinput".
//
// The remembered deviceId lives in localStorage, per browser and per machine,
// which is what a deviceId is scoped to anyway. A missing or blocked store
// just means "the default device".

import { t } from "../i18n.js";

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
