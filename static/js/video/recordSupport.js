// Whether this engine can record the Performance video, and the message that
// says why not. Pure: the browser facts are passed in, so node tests cover it
// (tests/js/record-support.test.mjs).
//
// Stock Ubuntu's WebKitGTK (the Linux desktop app) can lack MediaRecorder, or
// have it with no encoder behind it until the GStreamer plugins are
// installed. Recording then failed with no word at all (#787), so the Linux
// message names the packages.

import { MIME_LADDER } from "./videoCodec.js";

/** "mac", "windows", "linux" or "other", from a user agent string. */
export function platformFromUserAgent(ua) {
  const s = String(ua || "");
  if (/Mac OS X|Macintosh/.test(s)) return "mac";
  if (/Windows/.test(s)) return "windows";
  if (/Linux/.test(s) && !/Android/.test(s)) return "linux";
  return "other";
}

/**
 * The types of MIME_LADDER the engine says it can record, or null when it
 * cannot say (no isTypeSupported: the recorder then picks its own type).
 */
export function supportedRecorderMimes(isTypeSupported) {
  if (typeof isTypeSupported !== "function") return null;
  return MIME_LADDER.filter((type) => {
    try {
      return Boolean(isTypeSupported(type));
    } catch (err) {
      console.warn("[video] isTypeSupported threw for", type, err);
      return false;
    }
  });
}

/**
 * The i18n key of the message that says why video recording cannot start,
 * or null when it can.
 *
 *   hasMediaRecorder  window.MediaRecorder is a constructor
 *   hasCanvasCapture  canvas.captureStream exists (the Performance picture)
 *   supportedMimes    supportedRecorderMimes(), null when unknown
 *   platform          platformFromUserAgent()
 */
export function videoRecordBlockKey({ hasMediaRecorder, hasCanvasCapture = true, supportedMimes = null, platform = "other" }) {
  const linux = platform === "linux";
  if (!hasMediaRecorder || !hasCanvasCapture) return linux ? "streamer.rec.linuxPackages" : "streamer.rec.unsupported";
  if (Array.isArray(supportedMimes) && supportedMimes.length === 0) {
    return linux ? "streamer.rec.linuxPackages" : "streamer.rec.noEncoder";
  }
  return null;
}
