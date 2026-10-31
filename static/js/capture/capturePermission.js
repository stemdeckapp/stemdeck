// One place that asks for the microphone or the camera and turns every way
// that can fail into a reason the user can act on. Shared by the audio take
// recorder and the video recorder (05 section 5.3, 06 section 9).
//
// Reasons:
//   insecure-context    the page is not a secure context (phone over plain http)
//   unsupported         no navigator.mediaDevices.getUserMedia at all
//   linux-webview       the same, inside the Linux desktop app (WebKitGTK)
//   denied              the browser or the user refused
//   os-blocked-macos    macOS privacy settings refused (desktop app)
//   os-blocked-windows  Windows privacy settings refused (desktop app)
//   no-device           nothing to open
//   device-busy         another app holds it, or the OS would not open it
//   rate-mismatch       Firefox cannot join a mic at another rate to playback
//   unknown             anything else
//
// `kind` is "audio" or "video". Nothing here throws.

import { t } from "../i18n.js";

const REASONS = new Set([
  "insecure-context", "unsupported", "linux-webview", "denied", "os-blocked-macos",
  "os-blocked-windows", "no-device", "device-busy", "rate-mismatch", "unknown",
]);

function isDesktopApp() {
  return !!window.__TAURI__;
}

function platform() {
  const ua = navigator.userAgent || "";
  if (/Mac OS X|Macintosh/.test(ua)) return "mac";
  if (/Windows/.test(ua)) return "windows";
  if (/Linux/.test(ua) && !/Android/.test(ua)) return "linux";
  return "other";
}

/** Whether capture can even be attempted here, without prompting. */
export function checkCaptureSupport() {
  if (typeof window !== "undefined" && window.isSecureContext === false) {
    return { ok: false, reason: "insecure-context" };
  }
  if (!navigator.mediaDevices?.getUserMedia) {
    return { ok: false, reason: isDesktopApp() && platform() === "linux" ? "linux-webview" : "unsupported" };
  }
  return { ok: true, reason: null };
}

/** Map a getUserMedia rejection to a reason code. */
export function reasonFromError(err) {
  const name = err?.name || "";
  if (name === "NotAllowedError" || name === "SecurityError" || name === "PermissionDeniedError") {
    // The desktop app grants its own origin without asking (agent A), so a
    // refusal there comes from the operating system's privacy settings.
    if (isDesktopApp() && platform() === "mac") return "os-blocked-macos";
    if (isDesktopApp() && platform() === "windows") return "os-blocked-windows";
    return "denied";
  }
  if (name === "NotFoundError" || name === "DevicesNotFoundError" || name === "OverconstrainedError") {
    return "no-device";
  }
  if (name === "NotReadableError" || name === "TrackStartError" || name === "AbortError") {
    return "device-busy";
  }
  if (name === "NotSupportedError" && /sample-?rate/i.test(err?.message || "")) return "rate-mismatch";
  return "unknown";
}

/**
 * Ask for one kind of media. Resolves {stream, reason: null} or
 * {stream: null, reason}. A remembered device that has gone away is not an
 * error: the request is retried once on the default device.
 */
export async function requestMedia(kind, constraints = true) {
  const support = checkCaptureSupport();
  if (!support.ok) return { stream: null, reason: support.reason };
  const ask = (c) => navigator.mediaDevices.getUserMedia(
    kind === "video" ? { audio: false, video: c } : { audio: c, video: false },
  );
  try {
    return { stream: await ask(constraints), reason: null };
  } catch (err) {
    const pinned = constraints && typeof constraints === "object" && constraints.deviceId;
    if (pinned && (err?.name === "OverconstrainedError" || err?.name === "NotFoundError")) {
      const { deviceId: _gone, ...rest } = constraints;
      try {
        return { stream: await ask(Object.keys(rest).length ? rest : true), reason: null, fellBack: true };
      } catch (err2) {
        console.warn("[capture] getUserMedia failed on the default device too:", err2);
        return { stream: null, reason: reasonFromError(err2) };
      }
    }
    console.warn("[capture] getUserMedia failed:", err);
    return { stream: null, reason: reasonFromError(err) };
  }
}

/** The sentence for a reason, in the current language. */
export function reasonMessage(reason, kind = "audio") {
  const r = REASONS.has(reason) ? reason : "unknown";
  const k = kind === "video" ? "video" : "audio";
  // rate-mismatch only exists for the microphone; a camera never hits it.
  if (r === "rate-mismatch") return t("capture.reason.rate-mismatch");
  return t(`capture.reason.${r}.${k}`);
}

/**
 * The https address that would work, for a page opened over plain http on a
 * LAN address: the desktop app serves phones over https on port 8443.
 */
export function secureAddressHint() {
  if (location.protocol !== "http:") return null;
  return `https://${location.hostname}:8443${location.pathname}`;
}

/**
 * Fill `el` with the message for `reason` (or clear it when reason is null),
 * plus the https address for an insecure page and the plugin note for Linux.
 */
export function renderPermissionState(el, reason, kind = "audio") {
  if (!el) return;
  el.replaceChildren();
  el.hidden = !reason;
  el.classList.toggle("capture-blocked", !!reason);
  if (!reason) return;
  el.setAttribute("role", "status");
  const p = document.createElement("p");
  p.className = "capture-reason";
  p.dataset.reason = reason;
  p.textContent = reasonMessage(reason, kind);
  el.appendChild(p);
  if (reason === "insecure-context") {
    const hint = secureAddressHint();
    if (hint) {
      const h = document.createElement("p");
      h.className = "capture-hint";
      h.textContent = t("capture.hint.https", { url: hint });
      el.appendChild(h);
    }
  }
  if (reason === "no-device" && isDesktopApp() && platform() === "linux") {
    const h = document.createElement("p");
    h.className = "capture-hint";
    h.textContent = t("capture.hint.linuxPlugins");
    el.appendChild(h);
  }
}
