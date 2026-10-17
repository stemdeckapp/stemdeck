// The one place the video code touches the capture layer.
//
// static/js/capture/ owns permission messages, device lists, the take
// lifecycle and the part upload. Every capture module is loaded with a
// dynamic import, and a small local stand-in is used when one is missing, so
// the video code never fails to load because of the capture layer. With the
// capture layer present the stand-ins are never reached.
//
// The stand-in take lifecycle listens for a "stemdeck:take" DOM event whose
// detail is the same event object takeRecorder's onTakeEvent delivers. That is
// what tests/e2e/video-take.spec.mjs drives when the recorder is absent.

const _mods = {};

async function _load(name) {
  if (!(name in _mods)) {
    _mods[name] = import(`../capture/${name}.js`).catch((err) => {
      console.warn(`[video] capture/${name}.js not available, using the local stand-in`, err);
      return null;
    });
  }
  return _mods[name];
}

// ── Permission ──────────────────────────────────────────────────────────

function _reasonFor(err) {
  switch (err?.name) {
    case "NotAllowedError":
    case "SecurityError":
      return "denied";
    case "NotFoundError":
    case "OverconstrainedError":
      return "no-device";
    case "NotReadableError":
    case "AbortError":
      return "device-busy";
    default:
      return "unknown";
  }
}

/**
 * Open the camera. Never throws: resolves {stream, reason:null} or
 * {stream:null, reason}. Video only; the audio capture opens the microphone separately.
 */
export async function requestCamera(videoConstraints) {
  const perm = await _load("capturePermission");
  if (perm?.requestMedia) return perm.requestMedia("video", videoConstraints);
  if (!window.isSecureContext) return { stream: null, reason: "insecure-context" };
  if (!navigator.mediaDevices?.getUserMedia) return { stream: null, reason: "unsupported" };
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ video: videoConstraints, audio: false });
    return { stream, reason: null };
  } catch (err) {
    console.warn("[video] getUserMedia(video) failed:", err);
    return { stream: null, reason: _reasonFor(err) };
  }
}

/** Fill `el` with the shared, translated permission message for the camera. */
export async function renderCameraPermission(el, reason, fallbackText) {
  const perm = await _load("capturePermission");
  if (perm?.renderPermissionState) {
    perm.renderPermissionState(el, reason, "video");
    return;
  }
  el.textContent = fallbackText;
}

// ── Devices ─────────────────────────────────────────────────────────────

// Which camera is chosen in Settings > Recording (captureDevices.js); the
// recorder only reads it.
const _CAM_KEY = "stemdeck:capture:device:videoinput";

export async function getRememberedCamera() {
  const dev = await _load("captureDevices");
  if (dev?.getRememberedDevice) return dev.getRememberedDevice("videoinput");
  try {
    return window.localStorage.getItem(_CAM_KEY);
  } catch (err) {
    console.warn("[video] reading the remembered camera failed:", err);
    return null;
  }
}

// ── Take lifecycle ──────────────────────────────────────────────────────

const _localListeners = new Set();
const _localParticipants = new Set();
let _localWired = false;

function _wireLocal() {
  if (_localWired) return;
  _localWired = true;
  document.addEventListener("stemdeck:take", async (e) => {
    const ev = e.detail;
    if (!ev?.type) return;
    for (const cb of _localListeners) {
      try { cb(ev); } catch (err) { console.warn("[video] take listener failed:", err); }
    }
    if (ev.type !== "stopping") return;
    // Stand-in for takeRecorder's finish step: collect every participant's
    // block and report it, so a test can see what would go into take.json.
    const blocks = {};
    for (const fn of _localParticipants) {
      try {
        const block = await fn(ev.takeId);
        if (block) blocks.video = block;
      } catch (err) {
        console.warn("[video] take participant failed:", err);
      }
    }
    document.dispatchEvent(new CustomEvent("stemdeck:take-sidecar", { detail: { takeId: ev.takeId, ...blocks } }));
  });
}

/** Subscribe to take events: armed, stopping, finished, discarded, failed. */
export async function onTakeEvent(cb) {
  const rec = await _load("takeRecorder");
  if (rec?.onTakeEvent) return rec.onTakeEvent(cb);
  _wireLocal();
  _localListeners.add(cb);
  return () => _localListeners.delete(cb);
}

/** fn(takeId) -> Promise<videoBlock|null>, awaited by takeRecorder before /finish. */
export async function registerTakeParticipant(fn) {
  const rec = await _load("takeRecorder");
  if (rec?.registerTakeParticipant) return rec.registerTakeParticipant(fn);
  _wireLocal();
  _localParticipants.add(fn);
  return () => _localParticipants.delete(fn);
}

// ── Upload ──────────────────────────────────────────────────────────────

const _RETRIES = 3;

async function _putPart(jobId, takeId, part, seq, blob) {
  const url = `/api/jobs/${encodeURIComponent(jobId)}/takes/${encodeURIComponent(takeId)}/parts/${part}?seq=${seq}`;
  let lastErr;
  for (let attempt = 0; attempt <= _RETRIES; attempt++) {
    try {
      const res = await fetch(url, {
        method: "PUT",
        headers: { "Content-Type": "application/octet-stream" },
        body: blob,
      });
      if (res.ok) return true;
      // A 4xx will not get better on retry; only 5xx and network errors do.
      if (res.status < 500) throw Object.assign(new Error(`HTTP ${res.status}`), { fatal: true });
      lastErr = new Error(`HTTP ${res.status}`);
    } catch (err) {
      if (err.fatal) throw err;
      lastErr = err;
    }
    await new Promise((r) => setTimeout(r, 250 * 2 ** attempt));
  }
  throw lastErr;
}

/** Upload one camera part; resolves on the server ack, rejects after retries. */
export async function uploadCameraPart(jobId, takeId, seq, blob) {
  const up = await _load("takeUpload");
  if (up?.uploadPart) return up.uploadPart(jobId, takeId, "camera", seq, blob);
  return _putPart(jobId, takeId, "camera", seq, blob);
}
