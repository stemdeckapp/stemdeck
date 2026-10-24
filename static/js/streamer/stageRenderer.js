// Performance Mode's one stage renderer: the camera's picture, full frame,
// drawn into a Canvas 2D context. The live window (streamer.js) and the
// recorded frames (streamerRecord.js) call the same function, so the take is
// exactly what the live window showed, without the editor's guides.
//
// The picture covers the whole output (cut at the sides, or the top and
// bottom, when the camera's shape is not the output's), centred, never
// stretched. No camera: black. Nothing is drawn over the picture.
//
// `editor` draws what only the live window shows: where the camera goes
// while it is not showing, and the safe zones. Called without it (the
// output), neither is drawn. The REC badge is a button over the window, not
// part of the picture at all.

import { fitRect } from "./streamerModel.js";

const DESIGN_SHORT = 403;

function isDrawable(v) {
  if (!v) return false;
  if (typeof HTMLVideoElement !== "undefined" && v instanceof HTMLVideoElement) return v.videoWidth > 0;
  return Boolean((v.videoWidth ?? v.naturalWidth ?? v.width) > 0);
}

/** Rounded rectangle path, without ctx.roundRect (older WebKitGTK lacks it). */
function rr(ctx, x, y, w, h, r) {
  const k = Math.max(0, Math.min(r, w / 2, h / 2));
  ctx.beginPath();
  ctx.moveTo(x + k, y);
  ctx.arcTo(x + w, y, x + w, y + h, k);
  ctx.arcTo(x + w, y + h, x, y + h, k);
  ctx.arcTo(x, y + h, x, y, k);
  ctx.arcTo(x, y, x + w, y, k);
  ctx.closePath();
}

/** The camera covering W x H: the part of the picture drawn (source
 * rectangle) and where (the whole output). Exported for the tests. */
export function cameraCrop(iw, ih, W, H) {
  const f = fitRect(iw, ih, W, H, "cover");
  if (!f.w || !f.h) return null;
  const s = f.w / iw;
  return { sx: -f.x / s, sy: -f.y / s, sw: W / s, sh: H / s, dx: 0, dy: 0, dw: W, dh: H };
}

function drawPlaceholder(ctx, env) {
  const { W, H, u } = env;
  const g = ctx.createLinearGradient(0, 0, 0, H);
  g.addColorStop(0, "#121821");
  g.addColorStop(1, "#090c11");
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, W, H);
  const s = 30 * u;
  const cx = W / 2;
  const cy = H * 0.46;
  ctx.save();
  ctx.strokeStyle = "rgba(201,207,215,.28)";
  ctx.lineWidth = 1.6 * u;
  rr(ctx, cx - s, cy - s * 0.62, s * 2, s * 1.35, 4 * u);
  ctx.stroke();
  ctx.beginPath();
  ctx.arc(cx, cy + s * 0.05, s * 0.36, 0, Math.PI * 2);
  ctx.stroke();
  if (env.labels.camera) {
    ctx.fillStyle = "rgba(201,207,215,.42)";
    ctx.font = `500 ${12 * u}px ${env.font}`;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.fillText(env.labels.camera, cx, cy + s * 0.95, W * 0.8);
  }
  ctx.restore();
}

function drawSafeZones(ctx, env) {
  const { W, H, u, labels } = env;
  ctx.save();
  ctx.lineWidth = Math.max(1, u);
  ctx.setLineDash([4 * u, 3 * u]);
  const zone = (inset, alpha, label, labelAlpha) => {
    const x = W * inset;
    const y = H * inset;
    ctx.strokeStyle = `rgba(110,168,255,${alpha})`;
    ctx.strokeRect(x, y, W - 2 * x, H - 2 * y);
    if (!label) return;
    ctx.fillStyle = `rgba(140,185,255,${labelAlpha})`;
    ctx.font = `400 ${10 * u}px ${env.font}`;
    ctx.textAlign = "left";
    ctx.textBaseline = "bottom";
    ctx.fillText(label, x + 6 * u, H - y - 4 * u);
  };
  zone(0.05, 0.75, labels.actionSafe, 0.95);
  zone(0.1, 0.5, labels.titleSafe, 0.85);
  ctx.restore();
}

/**
 * Draw the stage at the output size `width` x `height` into `ctx` (already
 * scaled for the device's pixels by the caller).
 * - camera: the camera's <video> (or anything with videoWidth / videoHeight
 *   that drawImage takes), or null.
 * - opts: { width, height, font, labels: { camera, actionSafe, titleSafe }, editor }
 *   editor: null for the output, or { safeZones }.
 */
export function renderStage(ctx, camera, opts) {
  const W = opts.width;
  const H = opts.height;
  const editor = opts.editor || null;
  const env = {
    W,
    H,
    u: Math.min(W, H) / DESIGN_SHORT,
    font: opts.font || "sans-serif",
    labels: { ...(opts.labels || {}) },
  };
  ctx.save();
  ctx.fillStyle = "#000000";
  ctx.fillRect(0, 0, W, H);
  const crop = isDrawable(camera) ? cameraCrop(camera.videoWidth || camera.width, camera.videoHeight || camera.height, W, H) : null;
  if (crop) ctx.drawImage(camera, crop.sx, crop.sy, crop.sw, crop.sh, crop.dx, crop.dy, crop.dw, crop.dh);
  else if (editor) drawPlaceholder(ctx, env);
  if (editor?.safeZones) drawSafeZones(ctx, env);
  ctx.restore();
}
