// The frames of a video track's take, drawn along its lane in the waveform
// column, as an editor's video track shows them: thumbnails at the camera's
// own aspect, each the frame at that point of the song, under the take's
// waveform.
//
// The server builds the frames once per take (GET .../filmstrip: a JPEG row
// of frames, one every `interval` seconds of camera time) and says where the
// camera starts in the song (the take's camera_start_sec). player.js calls
// addFilmstrip after it draws a lane: a lane's contents are rebuilt on every
// redraw (a zoom step is one), so the strip is drawn again from this cache.

import { totalDuration } from "../state.js";
import { getTracks } from "../capture/takeLanes.js";

// camera url -> { meta, image } once loaded, or the promise of it.
const cache = new Map();

/** Whether a lane is a video track's (picture only, no waveform). */
export function isVideoLane(laneName) {
  return getTracks().some((tr) => tr.id === laneName && tr.kind === "video");
}

/** The active take of a video track, when it has a camera file. */
export function videoTakeOf(laneName) {
  const track = getTracks().find((tr) => tr.id === laneName);
  if (!track || track.kind !== "video") return null;
  const take = track.active;
  if (!take?.has_camera || !take.camera_url || !Number.isFinite(take.camera_start_sec)) return null;
  return take;
}

/** The take's filmstrip ({ meta, image }), built by the server on first ask
 * (GET .../filmstrip, then its image): the entry, or the promise of it. */
export function loadFilmstrip(take) {
  return load(take);
}

function load(take) {
  const key = take.camera_url;
  const hit = cache.get(key);
  if (hit) return hit;
  const pending = (async () => {
    const res = await fetch(take.camera_url.replace(/\/camera$/, "/filmstrip"));
    if (!res.ok) throw new Error(`filmstrip -> ${res.status}`);
    const meta = await res.json();
    const image = new Image();
    image.src = meta.url;
    await image.decode();
    const loaded = { meta, image };
    cache.set(key, loaded);
    return loaded;
  })();
  cache.set(key, pending);
  pending.catch((err) => {
    console.warn("[video] filmstrip failed:", err);
    // Asked again on the next redraw rather than remembered as a failure.
    if (cache.get(key) === pending) cache.delete(key);
  });
  return pending;
}

/**
 * Tiles across the canvas, each the frame at the camera time under its
 * middle. `from` and `length` are the camera seconds the canvas covers.
 */
export function drawFilmstrip(canvas, loaded, from, length) {
  draw(canvas, loaded, from, length);
}

function draw(canvas, { meta, image }, from, length) {
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  if (!w || !h || !(length > 0)) return;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const fw = meta.frame_width;
  const fh = meta.frame_height;
  const tileW = Math.max(8, h * (fw / fh));
  for (let x = 0; x < w; x += tileW) {
    // The middle of the part of this tile that shows: a lane zoomed out can
    // be narrower than one tile, and the tile's own middle is then past the
    // end of the take.
    const t = from + ((x + Math.min(tileW, w - x) / 2) / w) * length;
    const i = Math.min(meta.count - 1, Math.max(0, Math.floor(t / meta.interval)));
    ctx.drawImage(image, i * fw, 0, fw, fh, x, 0, tileW, h);
    // A thin gap between tiles, as an editor draws them.
    ctx.fillStyle = "rgba(0, 0, 0, 0.45)";
    ctx.fillRect(Math.round(x + tileW) - 1, 0, 1, h);
  }
}

/** Draw the frames into a freshly drawn lane row, when it is a video track's. */
export function addFilmstrip(row, laneName) {
  const take = videoTakeOf(laneName);
  if (!take || !(totalDuration > 0)) return;
  const canvas = document.createElement("canvas");
  canvas.className = "lane-filmstrip";
  canvas.setAttribute("aria-hidden", "true");
  row.prepend(canvas);
  const paint = (loaded) => {
    if (!canvas.isConnected) return;
    // A camera that started before the song (during a count-in) shows from
    // the song's start; one that runs past the end stops there.
    const start = Math.max(0, take.camera_start_sec);
    const from = start - take.camera_start_sec;
    const length = Math.min(loaded.meta.duration - from, totalDuration - start);
    if (!(length > 0)) { canvas.remove(); return; }
    canvas.style.left = `${(start / totalDuration) * 100}%`;
    canvas.style.width = `${(length / totalDuration) * 100}%`;
    draw(canvas, loaded, from, length);
  };
  const entry = load(take);
  if (entry instanceof Promise) entry.then(paint).catch(() => canvas.remove());
  else paint(entry);
}
