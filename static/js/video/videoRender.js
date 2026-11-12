// Client for the take render endpoints. Every path and field name the video
// UI depends on is in this file, so a change on the server side is one edit.

const POLL_MS = 1000;

function _base(jobId, takeId) {
  return `/api/jobs/${encodeURIComponent(jobId)}/takes/${encodeURIComponent(takeId)}`;
}

async function _detail(res) {
  try {
    const body = await res.json();
    if (typeof body?.detail === "string") return body.detail;
  } catch (err) {
    console.warn("[video] render error body was not JSON:", err);
  }
  return `HTTP ${res.status}`;
}

/** Save the sync nudge on the take, so the next render and a reload see it. */
export async function saveCameraOffset(jobId, takeId, cameraOffsetMs) {
  const res = await fetch(_base(jobId, takeId), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ camera_offset_ms: cameraOffsetMs }),
  });
  if (!res.ok) throw new Error(await _detail(res));
  return res.json().catch(() => ({}));
}

/**
 * Ask for a render. `mix` is the backing as heard: { names, gains, pitches } with the
 * take itself left out, and `takeGain` the take lane's own level. `span` is
 * "take" (from the count-in to where the take stopped) or "song" (all of it).
 * `fx` is the export EQ parameter (base64url), "" for none; `master` the
 * export's master= value, "" for none.
 * Resolves the server's status object ({status, progress, error, url}).
 */
export async function requestRender(jobId, takeId, {
  cameraOffsetMs, mix, takeGain = 1, canvas = "16:9", mirror = false, span = "take", fx = "",
  master = "",
}) {
  const body = {
    stems: mix?.names || [],
    gains: mix?.gains || [],
    take_gain: takeGain,
    camera_offset_ms: cameraOffsetMs,
    canvas,
    fit: "cover",
    mirror: Boolean(mirror),
    span: span === "song" ? "song" : "take",
  };
  // Omitted when nothing is transposed, so a build without rubberband can
  // still render an untransposed song.
  if (mix?.pitches?.some((p) => p !== 0)) body.pitches = mix.pitches;
  // The mode's own lanes (player.js _exportLanes): Original as playback built it.
  if (Array.isArray(mix?.lanes)) body.lanes = mix.lanes;
  // The lanes' EQs (eqEngine.exportFxParam), only when one changes the sound.
  // The take gets its track's EQ on the server.
  if (fx) body.fx = fx;
  // The master (masterEngine.exportMasterParam), only when it is on.
  if (master) body.master = master;
  const res = await fetch(`${_base(jobId, takeId)}/render`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw Object.assign(new Error(await _detail(res)), { status: res.status });
  return res.json();
}

/** Stop a render that is queued or running. The server kills its ffmpeg. */
export async function cancelRender(jobId, takeId) {
  const res = await fetch(`${_base(jobId, takeId)}/render/cancel`, { method: "POST" });
  if (!res.ok) throw Object.assign(new Error(await _detail(res)), { status: res.status });
  return res.json();
}

export async function renderStatus(jobId, takeId) {
  const res = await fetch(`${_base(jobId, takeId)}/render`, { cache: "no-store" });
  if (!res.ok) throw Object.assign(new Error(await _detail(res)), { status: res.status });
  return res.json();
}

export function renderDownloadUrl(jobId, takeId, status) {
  return status?.url || `${_base(jobId, takeId)}/render.mp4`;
}

/**
 * Poll until the render is done or failed. onProgress(fraction 0..1).
 * `signal` (AbortSignal) stops polling, e.g. when the track is closed.
 */
export async function waitForRender(jobId, takeId, { onProgress, signal } = {}) {
  for (;;) {
    if (signal?.aborted) throw new DOMException("aborted", "AbortError");
    const st = await renderStatus(jobId, takeId);
    const p = Number(st.progress);
    if (Number.isFinite(p)) onProgress?.(Math.max(0, Math.min(1, p)));
    if (st.status === "done") return st;
    if (st.status === "error" || st.status === "none") {
      // "none" while waiting means the render was cancelled or lost.
      throw new Error(st.error || st.status);
    }
    await new Promise((r) => setTimeout(r, POLL_MS));
  }
}
