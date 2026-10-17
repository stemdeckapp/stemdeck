// What a recording track records from: an input device and one channel of it
// (mono) or a pair (stereo).
//
// The user picks between two kinds of input, the way a home studio has them:
//   - voice: a microphone (a webcam's, a headset's, a USB mic). One input.
//   - instrument: an audio interface or a mixer with line inputs. Each
//     channel on its own ("Input 1", "Input 2") and the pairs ("Input 1+2").
//
// The browser's own list is not shown as it is. On Windows, Chromium and Edge
// add two aliases for the system default ("Default - ..." and
// "Communications - ..."), which repeat a real device under another name and
// another id, and every label ends in a USB id such as "(046d:0893)". Real
// devices are kept once each, named the way the user knows them:
// "Microphone (Logitech StreamCam) (046d:0893)" is "Logitech StreamCam",
// "Line (2- MG-XU) (0499:1703)" is "MG-XU".
//
// The pure part (friendlyName, inputKind, channelChoices, choicesFor,
// normaliseDevices, findDevice, resolveInput, ...) is pinned by
// tests/js/track-inputs.test.mjs.
//
// A track's input is stored on the server with the track
// ({device_id, device_label, channels}); null means "the default input from
// Settings > Recording". device_label holds the friendly name, so a track
// still finds its device when the id changes (another browser, the desktop
// app, a cleared profile): ids are per browser profile, names are not.

import { t } from "../i18n.js";
import { getRememberedDevice, listDevices, rememberDevice } from "./captureDevices.js";

const ALIASES = new Set(["default", "communications"]);
const USB_ID = /\s*\([0-9a-f]{4}:[0-9a-f]{4}\)\s*$/i;

// The type word Windows puts in front of an endpoint's name: "Microphone (X)",
// "Line (X)", "Line In (X)", "Headset Microphone (X)". The name in brackets is
// the device.
const ENDPOINT_TYPE = /^(microphone( array)?|mic( in)?|line( in)?|headset( microphone)?|external mic|digital audio interface|spdif( in)?|stereo mix|input)$/i;
// A device that is a microphone: voice. Anything else (an interface, a mixer,
// a line input) is an instrument input.
const VOICE = /\b(microphone|mic|headset|webcam|web cam|camera|streamcam|brio|airpods|hands-?free)\b/i;

/**
 * The name a user knows a device by. Strips the USB id, the alias prefix, the
 * Windows endpoint type and the instance number Windows puts before a second
 * device of one model ("2- MG-XU").
 */
export function friendlyName(label) {
  let s = String(label || "").trim();
  if (!s) return "";
  s = s.replace(USB_ID, "");
  s = s.replace(/^(default|communications)?\s*-\s+/i, "");
  const m = s.match(/^(.+?)\s*\((.+)\)$/);
  if (m && ENDPOINT_TYPE.test(m[1].trim())) s = m[2];
  s = s.replace(/^\d+\s*-\s*/, "").trim();
  return s || String(label).trim();
}

/** "voice" for a microphone, "instrument" for an interface or line input. */
export function inputKind(label) {
  return VOICE.test(String(label || "")) ? "voice" : "instrument";
}

/**
 * The channel choices for an interface with `max` input channels: each channel
 * on its own, then adjacent pairs (1+2, 3+4, ...). Channel indexes are 0-based;
 * labels count from 1, the way interfaces print them.
 */
export function channelChoices(max) {
  const n = Math.max(1, Math.min(64, Math.floor(Number(max)) || 1));
  const out = [];
  for (let i = 0; i < n; i++) out.push({ channels: [i], key: `${i + 1}`, n: i + 1 });
  for (let i = 0; i + 1 < n; i += 2) out.push({ channels: [i, i + 1], key: `${i + 1}+${i + 2}`, a: i + 1, b: i + 2 });
  return out;
}

/**
 * The choices a device offers. A microphone is one input whatever the driver
 * reports (a webcam's "stereo" is one capsule twice); an interface offers
 * each channel and each pair.
 */
export function choicesFor(device) {
  if (!device) return [];
  if (device.kind === "voice") return [{ channels: [0], key: "1", n: 1 }];
  return channelChoices(device.channels);
}

/**
 * The channels a new track records by default: one input, the first. A
 * guitar, a mic or a drum loop is one cable, and the pair recorded one
 * source as stereo with a silent side: on one speaker, 3 dB quieter. A
 * stereo source (a keyboard, a mixer's bus) picks 1+2 by hand. An armed
 * track whose input stays silent while another input of the device has
 * signal moves to it (recordUi.js). The argument is kept for callers that
 * pass a device or a channel count.
 */
export function defaultChannels(_device) {
  return [0];
}

/** Two track inputs share one device (and so one clock). Null and "" are the default input. */
export function sameDevice(a, b) {
  return (a?.device_id || "") === (b?.device_id || "");
}

/** Highest channel index a set of inputs needs, plus one. */
export function channelsNeeded(inputs) {
  let max = 1;
  for (const inp of inputs) for (const c of inp?.channels || [0]) max = Math.max(max, c + 1);
  return max;
}

/**
 * Turn the browser's device list into the input list: aliases and repeats
 * dropped, each device named and classed. `raw` is a list of
 * MediaDeviceInfo-like objects. The device the system uses by default (the
 * "default" alias names it) is marked `systemDefault`.
 */
export function normaliseDevices(raw) {
  const list = (raw || []).filter((d) => !d.kind || d.kind === "audioinput");
  const alias = list.find((d) => d.deviceId === "default");
  const aliasName = alias ? friendlyName(alias.label) : "";
  const seen = new Set();
  const out = [];
  for (const d of list) {
    if (!d.deviceId || ALIASES.has(d.deviceId) || seen.has(d.deviceId)) continue;
    seen.add(d.deviceId);
    let max = 2;
    try {
      const caps = d.getCapabilities?.();
      if (Number.isFinite(caps?.channelCount?.max)) max = Math.max(1, Math.min(64, caps.channelCount.max));
    } catch (e) {
      console.warn("[rec] channel count unavailable:", e);
    }
    const name = friendlyName(d.label);
    out.push({
      deviceId: d.deviceId,
      groupId: d.groupId || "",
      label: d.label || "",
      name,
      kind: inputKind(d.label),
      channels: max,
      systemDefault: !!alias && !!name && name === aliasName && (!alias.groupId || alias.groupId === d.groupId),
      index: out.length,
    });
  }
  // Two endpoints of one interface ("Analogue 1 + 2 (Focusrite USB Audio)",
  // "Analogue 3 + 4 (...)") share a name once the type is stripped: give
  // those back the part that tells them apart.
  const count = new Map();
  for (const d of out) count.set(d.name, (count.get(d.name) || 0) + 1);
  for (const d of out) {
    if (count.get(d.name) < 2) continue;
    const m = d.label.replace(USB_ID, "").match(/^(.+?)\s*\((.+)\)$/);
    if (m) d.name = `${d.name} (${m[1].trim()})`;
  }
  return out;
}

/**
 * The device an input refers to: by id, else by name (the id changed: another
 * browser, the desktop app), else null (unplugged).
 */
export function findDevice(input, devices) {
  if (!input || !devices?.length) return null;
  const id = input.device_id || "";
  const byId = id ? devices.find((d) => d.deviceId === id) : null;
  if (byId) return byId;
  const name = input.device_label ? friendlyName(input.device_label) : "";
  if (!name) return null;
  const byName = devices.filter((d) => d.name === name || friendlyName(d.label) === name);
  return byName.length === 1 ? byName[0] : null;
}

/** The Settings default: the one chosen there, else the system default, else the first. */
export function pickDefaultDevice(devices, rememberedId) {
  if (!devices?.length) return null;
  return devices.find((d) => d.deviceId === rememberedId)
    || devices.find((d) => d.systemDefault)
    || devices[0];
}

/**
 * Whether to open the microphone once just to read the device names.
 *
 * The desktop app grants its own page the microphone without asking
 * (desktop/src-tauri/src/mediaperm.rs) but never saves the grant, so every
 * page load starts with a list of nameless devices with no ids, which
 * normaliseDevices drops: the track picker read "No input found" until
 * something opened the microphone. A browser keeps the user's grant per site,
 * and opening the microphone there would mean a prompt at an unexpected
 * moment, so only the desktop app does this, and only once per page: a refusal
 * (the operating system's privacy settings) is not retried on every refresh.
 */
export function needsNameUnlock(raw, { desktop = false, tried = false } = {}) {
  if (!desktop || tried) return false;
  const inputs = (raw || []).filter((d) => d.kind === "audioinput");
  return inputs.length > 0 && inputs.every((d) => !d.label);
}

let _unlockTried = false;

async function unlockInputNames(raw) {
  const desktop = !!window.__TAURI__?.core?.invoke;
  if (!needsNameUnlock(raw, { desktop, tried: _unlockTried })) return false;
  _unlockTried = true;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
    stream.getTracks().forEach((tr) => tr.stop());
    return true;
  } catch (e) {
    console.warn("[rec] could not open the microphone to read the input names:", e);
    return false;
  }
}

/**
 * The input devices, named and classed. Names need microphone permission.
 * `unlock` lets the desktop app open the microphone once to read them; pass
 * it only when an input picker is about to be shown or a track created.
 */
export async function listInputDevices({ unlock = false } = {}) {
  const raw = await listDevices("audioinput");
  if (unlock && (await unlockInputNames(raw))) return normaliseDevices(await listDevices("audioinput"));
  return normaliseDevices(raw);
}

/** The input a new track starts with: the Settings default device, its default channels. */
// ── The two inputs set in Settings > Recording ──
//
// A microphone (what one sings or speaks into) and an audio interface (where
// instruments plug in), chosen separately. Kept per browser like the camera,
// under these roles. "none" is an explicit None; nothing stored means the
// choice was never made, and the single input chosen before there were two
// is carried over to the role its device belongs to.

export const INPUT_ROLES = Object.freeze(["voice", "instrument"]);
const NONE = "none";

function roleKey(role) {
  return `audioinput:${role}`;
}

/** The stored choice for a role: a device id, NONE, or "" when never set. */
export function storedRole(role) {
  return getRememberedDevice(roleKey(role));
}

/**
 * Choose a role's device: an id, or "" for None. Its name is kept too: a
 * device's id can change (the desktop app, a browser that resets them), and
 * the name finds it again.
 */
export function rememberRole(role, deviceId, name = "") {
  rememberDevice(roleKey(role), deviceId || NONE);
  rememberDevice(`${roleKey(role)}:name`, deviceId ? name : "");
}

/**
 * The device a role uses now: the one chosen in Settings (if connected), or
 * when never chosen, the earlier single choice if it is this kind of device,
 * else the system's default of this kind, else the first of this kind. Null
 * for None or when nothing of the kind is connected.
 */
export function roleDevice(role, devices) {
  if (!devices?.length) return null;
  const stored = storedRole(role);
  if (stored === NONE) return null;
  if (stored) {
    const dev = devices.find((d) => d.deviceId === stored);
    if (dev) return dev;
    // The id changed: the same device by its name, when only one has it.
    const name = getRememberedDevice(`${roleKey(role)}:name`);
    const byName = name ? devices.filter((d) => d.name === name) : [];
    if (byName.length === 1) return byName[0];
  }
  const legacy = getRememberedDevice("audioinput");
  const old = legacy ? devices.find((d) => d.deviceId === legacy && d.kind === role) : null;
  if (old) return old;
  const kind = devices.filter((d) => d.kind === role);
  return kind.find((d) => d.systemDefault) || kind[0] || null;
}

/**
 * The device a new track or an unset input records from: for an audio
 * track the interface, then the microphone; for a video track the
 * microphone, then the interface; then any device.
 */
export function defaultInputDevice(devices, trackKind = "audio") {
  const order = trackKind === "video" ? ["voice", "instrument"] : ["instrument", "voice"];
  for (const role of order) {
    const dev = roleDevice(role, devices);
    if (dev) return dev;
  }
  return pickDefaultDevice(devices, getRememberedDevice("audioinput"));
}

/** Without the device list: the id the default input is stored under, or "". */
export function defaultInputId() {
  for (const role of ["instrument", "voice"]) {
    const id = storedRole(role);
    if (id && id !== NONE) return id;
  }
  return getRememberedDevice("audioinput");
}

export async function defaultTrackInput(trackKind = "audio") {
  const devices = await listInputDevices({ unlock: true });
  const dev = defaultInputDevice(devices, trackKind);
  if (!dev) return null;
  return { device_id: dev.deviceId, device_label: dev.name || null, channels: defaultChannels(dev) };
}

/**
 * The input a track actually records from: its own (found again by name when
 * its id changed), or the Settings default device at its default channels.
 * `missing` is set when the track's own device is not connected; the stored
 * id is kept, so Record refuses rather than record from another input.
 * `rememberedId` defaults to the Settings choice.
 */
export function resolveInput(input, devices, rememberedId = defaultInputDevice(devices)?.deviceId || "") {
  if (input && Array.isArray(input.channels) && input.channels.length) {
    const dev = findDevice(input, devices);
    if (dev) {
      // A microphone is one input: a stored pair (chosen before the device
      // was known as a microphone) records its first channel.
      const channels = dev.kind === "voice" ? [0] : input.channels;
      return { device_id: dev.deviceId, device_label: dev.name, channels, kind: dev.kind, missing: false };
    }
    if (input.device_id || input.device_label) {
      return {
        device_id: input.device_id || "",
        device_label: friendlyName(input.device_label || ""),
        channels: input.channels,
        kind: inputKind(input.device_label),
        missing: !!devices?.length,
      };
    }
  }
  const dev = pickDefaultDevice(devices, rememberedId);
  return {
    device_id: dev?.deviceId || rememberedId || "",
    device_label: dev?.name || "",
    channels: dev ? defaultChannels(dev) : [0, 1],
    kind: dev?.kind || "instrument",
    missing: false,
  };
}

// ─── The pickers ───

/** "Input 2" or "Input 1+2". */
export function channelText(channels) {
  return channels.length === 1
    ? t("rec.track.inputMono", { n: channels[0] + 1 })
    : t("rec.track.inputPair", { a: channels[0] + 1, b: channels[1] + 1 });
}

/** What a track picker option reads: "Logitech StreamCam", "MG-XU · Input 1". */
export function choiceText(device, channels) {
  const name = device.name || t("capture.inputN", { n: device.index + 1 });
  return device.kind === "voice" ? name : t("rec.input.instrumentChoice", { name, input: channelText(channels) });
}

/**
 * Fill a <select> with the inputs, grouped as voice (microphones) and
 * instrument (interfaces). `withChannels` lists an interface's channels (the
 * track picker); without it each device is one option (Settings).
 * `missing` (a device name) adds a disabled "not connected" entry first.
 * Returns a map of option value -> {device, channels}.
 */
export function fillInputSelect(select, devices, { withChannels = false, missing = null } = {}) {
  const map = new Map();
  select.replaceChildren();
  if (missing != null) {
    // The track's device is not connected: say so in the picker itself, so
    // nothing records from another input without a word.
    const o = document.createElement("option");
    o.value = MISSING;
    o.disabled = true;
    o.textContent = t("rec.input.missing", { name: missing || t("rec.input.unknownDevice") });
    select.append(o);
  }
  for (const [kind, key] of [["voice", "rec.input.voiceGroup"], ["instrument", "rec.input.instrumentGroup"]]) {
    const devs = devices.filter((d) => d.kind === kind);
    if (!devs.length) continue;
    const group = document.createElement("optgroup");
    group.label = t(key);
    for (const dev of devs) {
      const choices = withChannels ? choicesFor(dev) : [{ channels: defaultChannels(dev) }];
      for (const choice of choices) {
        const o = document.createElement("option");
        o.value = withChannels ? `${dev.deviceId}\u0000${choice.channels.join(",")}` : dev.deviceId;
        o.textContent = withChannels ? choiceText(dev, choice.channels) : (dev.name || t("capture.inputN", { n: dev.index + 1 }));
        o.dataset.kind = kind;
        map.set(o.value, { device: dev, channels: choice.channels });
        group.append(o);
      }
    }
    select.append(group);
  }
  return map;
}

export const MISSING = "__missing__";

/**
 * One of Settings > Recording's two input pickers (`role` "voice" for the
 * microphone, "instrument" for the audio interface): None, then every input
 * grouped as voice and instrument, the role's device selected, following
 * plugs and unplugs. `onChange(deviceId)` runs when the user picks (not on
 * refreshes); "" is None.
 */
export function buildRolePicker(selectEl, role, onChange) {
  let disposed = false;
  let names = new Map();
  async function refresh() {
    const devices = await listInputDevices();
    if (disposed) return devices;
    names = new Map(devices.map((d) => [d.deviceId, d.name]));
    fillInputSelect(selectEl, devices);
    const none = document.createElement("option");
    none.value = "";
    none.textContent = t("settings.rec.none");
    selectEl.prepend(none);
    selectEl.value = roleDevice(role, devices)?.deviceId || "";
    selectEl.disabled = devices.length === 0;
    return devices;
  }
  const onPick = () => {
    rememberRole(role, selectEl.value, names.get(selectEl.value) || "");
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

/**
 * Settings > Recording's input picker: one list, voice and instrument, the
 * remembered choice selected, following plugs and unplugs. `onChange(deviceId)`
 * runs when the user picks another device (not on refreshes).
 */
export function buildInputPicker(selectEl, onChange) {
  let disposed = false;
  async function refresh() {
    const devices = await listInputDevices();
    if (disposed) return devices;
    fillInputSelect(selectEl, devices);
    const dev = pickDefaultDevice(devices, getRememberedDevice("audioinput"));
    if (dev) selectEl.value = dev.deviceId;
    if (!devices.length) {
      const o = document.createElement("option");
      o.textContent = t("rec.track.noInput");
      selectEl.append(o);
    }
    selectEl.disabled = devices.length === 0;
    return devices;
  }
  const onPick = () => {
    rememberDevice("audioinput", selectEl.value);
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
