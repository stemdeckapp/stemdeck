// Recording settings that more than one module reads: the camera's capture
// size and preview mirror, and the event that tells the recorders a setting
// changed. The input and camera device choices live in captureDevices.js and
// the calibration in latencyCalibration.js; their storage keys are unchanged,
// so choices made before Settings had a Recording tab still apply.

const QUALITY_KEY = "stemdeck:video:quality";
const MIRROR_KEY = "stemdeck:video:mirrorPreview";
export const CAMERA_QUALITIES = ["720", "1080"];
export const DEFAULT_CAMERA_QUALITY = "720";

/**
 * Fired on document whenever a recording setting changes.
 * detail.kind: "audioinput" | "calibration" | "videoinput" | "quality" | "mirror"
 */
export const SETTINGS_EVENT = "stemdeck:recording-settings";

function read(key, fallback) {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch (e) {
    console.warn("[capture] could not read a recording setting:", e);
    return fallback;
  }
}

function write(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (e) {
    console.warn("[capture] could not store a recording setting:", e);
  }
}

export function getCameraQuality() {
  const q = read(QUALITY_KEY, DEFAULT_CAMERA_QUALITY);
  return CAMERA_QUALITIES.includes(q) ? q : DEFAULT_CAMERA_QUALITY;
}

export function setCameraQuality(q) {
  write(QUALITY_KEY, CAMERA_QUALITIES.includes(q) ? q : DEFAULT_CAMERA_QUALITY);
  announce("quality");
}

/** Mirrored by default: people expect a preview to behave like a mirror. */
export function getMirrorPreview() {
  return read(MIRROR_KEY, "1") !== "0";
}

export function setMirrorPreview(on) {
  write(MIRROR_KEY, on ? "1" : "0");
  announce("mirror");
}

export function announce(kind) {
  document.dispatchEvent(new CustomEvent(SETTINGS_EVENT, { detail: { kind } }));
}
