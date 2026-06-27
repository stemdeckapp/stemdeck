// Shared, DOM-free job/library helpers used by both the mobile UI and
// (incrementally) the desktop catalog. Anything in here must stay free of
// document/window-element access so it can be imported from either entry
// point. The API (`GET /api/jobs`) is the canonical data source.
import { fmtTime } from "../utils.js";

// Mirror of catalog.js PROCESSING_STATUSES — keep in sync with the backend
// job lifecycle. These map to the amber "working" dot.
const PROCESSING_STATUSES = new Set(["queued", "downloading", "analyzing", "separating", "processing"]);

export async function fetchJobs() {
  const res = await fetch("/api/jobs", { cache: "no-store" });
  if (!res.ok) throw new Error(`GET /api/jobs -> ${res.status}`);
  return res.json();
}

export function deriveSource(sourceUrl) {
  if (!sourceUrl) return "Local file";
  if (sourceUrl.startsWith("local:")) return "Local file";
  if (sourceUrl.includes("youtube.com") || sourceUrl.includes("youtu.be")) return "YouTube";
  if (sourceUrl.includes("soundcloud.com")) return "SoundCloud";
  return "Web";
}

// One of: "done" (green), "processing" (amber), "unavailable" (grey/red).
export function statusKind(status) {
  if (PROCESSING_STATUSES.has(status)) return "processing";
  if (status === "done") return "done";
  return "unavailable"; // unavailable / error / failed / unknown
}

export function coverInitial(title) {
  const t = (title || "").trim();
  return t ? t[0].toUpperCase() : "♪";
}

// Deterministic cover gradient: the same track always gets the same color.
// Palette matches the mobile design's aesthetic (design/mobile prototype).
const GRADIENTS = [
  "linear-gradient(150deg,#7b46f0,#3f6ef0 55%,#1b245f)",
  "linear-gradient(140deg,#2bd4c4,#1a6d9e)",
  "linear-gradient(150deg,#c44ad0,#5a2b9e)",
  "linear-gradient(150deg,#f06a6a,#7a2b4e)",
  "linear-gradient(150deg,#4a9bf5,#2b3a7a)",
  "linear-gradient(150deg,#f5a516,#d2541f)",
  "linear-gradient(150deg,#3fcf6e,#1a6d4e)",
];

export function coverGradient(seed) {
  const s = String(seed || "");
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return GRADIENTS[h % GRADIENTS.length];
}

// Normalize an /api/jobs state object into the shape the mobile UI renders.
export function jobToCard(state) {
  const id = state.job_id;
  const title = state.title || "Untitled";
  const stemCount = (state.stems || state.selected_stems || []).length;
  const parts = [];
  if (state.duration) parts.push(fmtTime(state.duration));
  if (stemCount) parts.push(`${stemCount} stem${stemCount === 1 ? "" : "s"}`);
  return {
    id,
    title,
    sub: deriveSource(state.source_url),
    meta: parts.join(" · "),
    stemCount,
    status: statusKind(state.status),
    createdAt: state.created_at || 0,
    initial: coverInitial(title),
    gradient: coverGradient(id || title),
  };
}
