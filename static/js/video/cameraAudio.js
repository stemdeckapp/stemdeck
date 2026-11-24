// The camera's own microphone, recorded with the picture so the server can
// measure where the picture sits against the take's sound.
//
// Why: the take's sound (the capture graph's recorder worklet) and the
// picture (a MediaRecorder) start at different moments, and the browser can
// only estimate the gap. Measured on a Logitech StreamCam the estimate was
// 430 to 530 ms wrong and changed by 100 ms between takes. Inside one
// MediaRecorder the browser keeps picture and sound together, so the
// camera's sound, compared with the take's on the server
// (app/pipeline/av_sync.py), says where the picture is.
//
// The microphone is the camera's: the audio input whose group id is the
// camera's (the same physical device). None: the picture records alone, as
// before, and the server keeps the estimate. It is never connected to an
// AudioContext, never played and never mixed: the render maps only the
// camera file's picture (app/pipeline/take_render.py), and every review
// <video> is muted.
//
// Never throws. Pure helpers first (tests/js/camera-audio.test.mjs).

// Windows' virtual entries share a real device's group id; the real entry
// is the one to open.
const VIRTUAL_IDS = new Set(["default", "communications"]);

/**
 * The audio input of the same physical device as the camera, or null.
 * `devices` is navigator.mediaDevices.enumerateDevices()'s list.
 */
export function matchCameraMic(groupId, devices) {
  if (!groupId || !Array.isArray(devices)) return null;
  const same = devices.filter((d) => d?.kind === "audioinput" && d.deviceId && d.groupId === groupId);
  return same.find((d) => !VIRTUAL_IDS.has(d.deviceId)) || same[0] || null;
}

/**
 * getUserMedia constraints for the camera's microphone: the take's own
 * capture settings (static/js/capture/captureGraph.js audioConstraints): no
 * echo cancellation, noise suppression or gain control, which would change
 * the sound the comparison reads and, on a shared device, the take's input.
 */
export function cameraMicConstraints(deviceId) {
  return {
    deviceId: { exact: deviceId },
    echoCancellation: false,
    noiseSuppression: false,
    autoGainControl: false,
    channelCount: { ideal: 2 },
    latency: 0,
  };
}

/** "denied" or "failed" for a getUserMedia error. */
export function micErrorState(err) {
  return err?.name === "NotAllowedError" || err?.name === "SecurityError" ? "denied" : "failed";
}

/**
 * A track already open on the same microphone (the take's own input, when
 * the camera's microphone is what the musician plays into), or null. Its
 * clone is used: a second getUserMedia on that device, with the camera
 * microphone's constraints, could reconfigure the take's input.
 *
 * The same microphone by any of its names: its own device id, a Windows
 * alias ("default", "communications") of the same group in `devices`, or
 * the track's own group id. The take's input is often opened as "default",
 * and matching the real id alone missed it.
 */
export function openTrackOn(device, streams, devices = []) {
  if (!device) return null;
  const ids = new Set([device.deviceId]);
  if (device.groupId) {
    for (const d of Array.isArray(devices) ? devices : []) {
      if (d?.kind === "audioinput" && d.deviceId && d.groupId === device.groupId) ids.add(d.deviceId);
    }
  }
  for (const s of streams || []) {
    for (const tr of s?.getAudioTracks?.() || []) {
      if (tr.readyState === "ended") continue;
      const set = tr.getSettings?.() || {};
      if (set.deviceId && ids.has(set.deviceId)) return tr;
      if (device.groupId && set.groupId && set.groupId === device.groupId) return tr;
    }
  }
  return null;
}

// How long Record waits for the microphone to open before the take goes on
// without it.
export const OPEN_TIMEOUT_MS = 3000;

/**
 * The microphone permission as the Permissions API says it: "granted",
 * "denied", "prompt" (not asked yet), or "unknown" (no Permissions API for
 * it, as in Firefox and older WebKit).
 */
export async function micPermission() {
  try {
    const st = await navigator.permissions?.query?.({ name: "microphone" });
    if (!st) return "unknown";
    return ["granted", "denied", "prompt"].includes(st.state) ? st.state : "unknown";
  } catch {
    return "unknown";
  }
}

/**
 * May the camera microphone be opened now, while a take starts? Never with
 * a prompt: a permission dialog in the middle of Record would hold the take
 * up. So only when the permission is known to be granted: the Permissions
 * API says "granted", or (no Permissions API) the device list names the
 * microphone, which browsers do only once microphone access was granted.
 * Otherwise "not-asked" (or "denied"), and the picture records alone.
 */
export function openDecision(permission, mic) {
  if (permission === "denied") return "denied";
  if (permission === "granted") return "open";
  if (permission === "unknown" && String(mic?.label || "")) return "open";
  return "not-asked";
}

/** `promise`, or `fallback` after `ms`; a late value is handed to `late`. */
function within(promise, ms, fallback, late) {
  let timer = null;
  let done = false;
  return Promise.race([
    promise.then((v) => {
      if (done) late?.(v);
      return v;
    }),
    new Promise((resolve) => {
      timer = setTimeout(() => {
        done = true;
        resolve(fallback);
      }, ms);
    }),
  ]).finally(() => {
    done = true;
    clearTimeout(timer);
  });
}

/**
 * Open the camera's microphone. Resolves { track, state, label }: `track` a
 * MediaStreamTrack (the caller stops it) or null, `state` "recorded" (a
 * track to record), "no-mic", "denied", "not-asked" (no permission yet, and
 * no prompt during a take), "failed" or "unsupported".
 * `openStreams` are streams already open (the take's input), reused by clone.
 * Gives up after OPEN_TIMEOUT_MS ("failed"); a track that opens later is stopped.
 */
export function openCameraAudio(videoTrack, openStreams = [], timeoutMs = OPEN_TIMEOUT_MS) {
  const failed = { track: null, state: "failed", label: "" };
  return within(openNow(videoTrack, openStreams), timeoutMs, failed, (late) => late?.track?.stop());
}

async function openNow(videoTrack, openStreams) {
  const md = typeof navigator !== "undefined" ? navigator.mediaDevices : null;
  if (!videoTrack || !md?.enumerateDevices || !md?.getUserMedia) {
    return { track: null, state: "unsupported", label: "" };
  }
  const permission = await micPermission();
  if (permission === "denied") return { track: null, state: "denied", label: "" };
  let devices;
  try {
    devices = await md.enumerateDevices();
  } catch (err) {
    console.warn("[video] listing devices for the camera microphone failed:", err);
    return { track: null, state: "failed", label: "" };
  }
  const settings = videoTrack.getSettings?.() || {};
  let groupId = settings.groupId;
  if (!groupId && settings.deviceId) {
    groupId = devices.find((d) => d.kind === "videoinput" && d.deviceId === settings.deviceId)?.groupId;
  }
  const mic = matchCameraMic(groupId, devices);
  if (!mic) return { track: null, state: "no-mic", label: "" };
  const label = String(mic.label || "");
  const open = openTrackOn(mic, openStreams, devices);
  if (open) {
    try {
      return { track: open.clone(), state: "recorded", label };
    } catch (err) {
      console.warn("[video] cloning the open microphone failed, opening it:", err);
    }
  }
  const decision = openDecision(permission, mic);
  if (decision !== "open") return { track: null, state: decision, label };
  try {
    const stream = await md.getUserMedia({ audio: cameraMicConstraints(mic.deviceId), video: false });
    const track = stream.getAudioTracks()[0] || null;
    if (!track) return { track: null, state: "failed", label };
    return { track, state: "recorded", label };
  } catch (err) {
    console.warn("[video] the camera microphone did not open:", err);
    return { track: null, state: micErrorState(err), label };
  }
}
