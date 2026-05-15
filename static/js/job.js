import {
  form, urlInput, submitBtn, errorEl, jobBox, jobTitleEl, jobStageEl,
  jobDetailEl, jobCancelBtn, progressEl, titleEl, bpmChip, keyChip,
  eventSource, setEventSource, setCurrentJobId, currentJobId,
  selectedStems,
} from "./state.js";
import { destroyPlayer } from "./player.js";
import { wireUpAudio } from "./player.js";
import { stagePhrases } from "./phrases.js";
import { addTrackToLibrary, setCurrentTrack, updateTrackStatus } from "./catalog.js";

// Playful stage label rotation (Claude-Code-style flair). The backend
// emits truthful stage strings; we surface them in the small #job-detail
// line so progress is debuggable, while #job-stage rotates whimsy.
const ROTATION_MS = 2500;
let phraseTimerId = null;
let lastStatus = null;
let jobPollTimerId = null;
const renderedJobs = new Set();
const jobSources = new Map();

const TERMINAL_STATUSES = new Set(["done", "error", "cancelled"]);

function setSubmitProcessing(processing) {
  submitBtn.disabled = processing;
  submitBtn.classList.toggle("loading", processing);
  document.querySelector(".strip-sq-process")?.classList.toggle("loading", processing);
  const label = submitBtn.querySelector("span");
  if (label) label.textContent = processing ? "Processing" : "Process";
}

function pickPhrase(status) {
  const pool = stagePhrases[status] || stagePhrases.default;
  return pool[Math.floor(Math.random() * pool.length)];
}

function startPhraseRotation(status) {
  stopPhraseRotation();
  jobStageEl.textContent = pickPhrase(status);
  phraseTimerId = setInterval(
    () => { jobStageEl.textContent = pickPhrase(status); },
    ROTATION_MS,
  );
}

function stopPhraseRotation() {
  if (phraseTimerId) {
    clearInterval(phraseTimerId);
    phraseTimerId = null;
  }
}

function stopJobPolling() {
  if (jobPollTimerId) {
    clearInterval(jobPollTimerId);
    jobPollTimerId = null;
  }
}

export function showError(message) {
  errorEl.textContent = "";
  const msg = document.createElement("div");
  msg.className = "error-msg";
  msg.textContent = message;
  const retry = document.createElement("button");
  retry.className = "retry-btn";
  retry.type = "button";
  retry.textContent = "Try again";
  retry.addEventListener("click", () => {
    errorEl.classList.add("hidden");
    urlInput.focus();
    urlInput.select();
  });
  errorEl.append(msg, retry);
  errorEl.classList.remove("hidden");
}

export function reset() {
  if (eventSource) {
    eventSource.close();
    setEventSource(null);
  }
  stopJobPolling();
  stopPhraseRotation();
  lastStatus = null;
  destroyPlayer();
  errorEl.classList.add("hidden");
  errorEl.textContent = "";
  jobBox.classList.add("hidden");
  jobCancelBtn.classList.add("hidden");
  jobTitleEl.textContent = "";
  jobStageEl.textContent = "";
  jobDetailEl.textContent = "";
  progressEl.value = 0;
  setSubmitProcessing(false);
  setCurrentJobId(null);
}

function applyState(state) {
  if (state.job_id) {
    addTrackToLibrary({
      id: state.job_id,
      title: state.title || urlInput.value || "Processing track",
      channel: state.status === "done" ? "Extracted" : "Processing",
      thumb: state.thumbnail,
      stems: state.selected_stems || state.stems?.map((stem) => stem.name) || [...selectedStems],
      selectedStems: state.selected_stems || [...selectedStems],
      audioStems: state.stems || [],
      status: state.status,
      duration: state.duration,
      bpm: state.bpm,
      key: state.key,
      scale: state.scale,
      keyConfidence: state.key_confidence,
      lufs: state.lufs,
      peakDb: state.peak_db,
      sourceUrl: jobSources.get(state.job_id) || urlInput.value,
    });
    setCurrentTrack(state.job_id);
  }
  if (state.title) {
    jobTitleEl.textContent = state.title;
    titleEl.textContent = state.title;
  }
  if (state.bpm) bpmChip.textContent = `${state.bpm} BPM`;
  if (state.key) keyChip.textContent = state.key;
  const summaryKey = document.getElementById("summary-key");
  const summaryBpm = document.getElementById("summary-bpm");
  const summaryScale = document.getElementById("summary-scale");
  const summaryConfidence = document.getElementById("summary-confidence");
  const summaryConfidenceLabel = document.getElementById("summary-confidence-label");
  const loudnessCard = document.getElementById("loudness-card");
  const summaryLufs = document.getElementById("summary-lufs");
  const summaryPeak = document.getElementById("summary-peak");
  if (summaryKey && state.key) summaryKey.textContent = state.key;
  if (summaryBpm && state.bpm) {
    summaryBpm.textContent = "";
    const bpmNum = document.createTextNode(String(state.bpm) + " ");
    const bpmUnit = document.createElement("small");
    bpmUnit.textContent = "BPM";
    summaryBpm.append(bpmNum, bpmUnit);
  }
  if (summaryScale && state.scale) summaryScale.textContent = state.scale;
  if (summaryConfidence && state.key_confidence != null) {
    const confidence = Math.max(0, Math.min(100, Number(state.key_confidence)));
    const confSpan = document.createElement("span");
    confSpan.textContent = `${confidence}%`;
    summaryConfidence.textContent = "";
    summaryConfidence.appendChild(confSpan);
    summaryConfidence.style.setProperty("--confidence-pct", confidence);
    summaryConfidence.classList.remove("hidden");
    summaryConfidenceLabel?.classList.remove("hidden");
  }
  // LUFS and peak surface only when the analyzer produced numeric values.
  // Silence or extremely short clips return null -- we keep the card
  // hidden in that case rather than render "— LUFS".
  if (loudnessCard && state.lufs != null && state.peak_db != null) {
    if (summaryLufs) summaryLufs.textContent = state.lufs.toFixed(1);
    if (summaryPeak) summaryPeak.textContent = state.peak_db.toFixed(1);
    loudnessCard.classList.remove("hidden");
  }
  // Stage label is owned by the phrase-rotation timer below; we don't
  // overwrite it from each SSE tick. The truthful backend stage goes
  // to the small detail line instead.
  jobDetailEl.textContent = state.stage || "";
  progressEl.value = Math.round((state.progress || 0) * 100);

  // Cancel button is visible exactly while the job is in a non-terminal state.
  const terminal = TERMINAL_STATUSES.has(state.status);
  jobCancelBtn.classList.toggle("hidden", terminal);

  if (state.status !== lastStatus) {
    if (terminal) stopPhraseRotation();
    else startPhraseRotation(state.status);
    lastStatus = state.status;
  }

  if (state.status === "error") {
    stopJobPolling();
    updateTrackStatus(state.job_id, "error");
    showError(state.error || "Unknown error");
    setSubmitProcessing(false);
  } else if (state.status === "cancelled") {
    stopJobPolling();
    updateTrackStatus(state.job_id, "cancelled");
    jobBox.classList.add("hidden");
    setSubmitProcessing(false);
  } else if (state.status === "done") {
    stopJobPolling();
    updateTrackStatus(state.job_id, "done");
    jobBox.classList.add("hidden");
    if (!renderedJobs.has(state.job_id)) {
      renderedJobs.add(state.job_id);
      wireUpAudio(
        state.job_id,
        state.stems || [],
        state.duration || 0,
        state.thumbnail,
      );
    }
    setSubmitProcessing(false);
  }
}

async function probeJob(jobId) {
  const r = await fetch(`/api/jobs/${jobId}`);
  if (!r.ok) {
    if (r.status === 404) throw new Error("Job no longer exists on the server");
    throw new Error(`Job probe failed: ${r.status}`);
  }
  const s = await r.json();
  applyState(s);
  return s;
}

function startJobPolling(jobId) {
  stopJobPolling();
  const tick = async () => {
    try {
      const s = await probeJob(jobId);
      if (TERMINAL_STATUSES.has(s.status)) stopJobPolling();
    } catch (err) {
      console.warn("[job] REST fallback failed:", err);
    }
  };
  tick();
  jobPollTimerId = setInterval(tick, 1000);
}

// Connect (or reconnect) to the SSE stream for a job. On unexpected
// disconnect we probe /api/jobs/{id} to decide: if the job is already
// terminal, accept its final state; otherwise reconnect with backoff.
function connectEvents(jobId) {
  let attempt = 0;
  let stopped = false;

  const open = () => {
    const es = new EventSource(`/api/jobs/${jobId}/events`);
    setEventSource(es);

    es.onmessage = (ev) => {
      attempt = 0; // any successful frame resets backoff
      let s;
      try { s = JSON.parse(ev.data); } catch { return; }
      // Defer by one tick so synchronous user event handlers (clicks,
      // input events) always complete before SSE state is applied.
      setTimeout(() => {
        applyState(s);
        if (TERMINAL_STATUSES.has(s.status)) {
          stopped = true;
          es.close();
          setEventSource(null);
        }
      }, 0);
    };

    es.onerror = async () => {
      if (stopped) return;
      es.close();
      setEventSource(null);

      // Probe REST once before declaring failure -- handles dev-server
      // reloads and brief network blips where the job is actually fine.
      try {
        const s = await probeJob(jobId);
        if (TERMINAL_STATUSES.has(s.status)) {
          stopped = true;
          return;
        }
      } catch (err) {
        if (err.message === "Job no longer exists on the server") {
          stopped = true;
          showError(err.message);
          setSubmitProcessing(false);
          return;
        }
        // Network down -- fall through to backoff.
      }

      attempt += 1;
      if (attempt > 6) {
        showError("Lost connection to server");
        setSubmitProcessing(false);
        return;
      }
      // 0.5s, 1s, 2s, 4s, 8s, 16s
      const delay = 500 * Math.pow(2, attempt - 1);
      setTimeout(() => { if (!stopped) open(); }, delay);
    };
  };

  open();
}

async function cancelCurrentJob() {
  const id = currentJobId;
  if (!id) return;
  jobCancelBtn.disabled = true;
  jobCancelBtn.textContent = "Cancelling…";
  try {
    await fetch(`/api/jobs/${id}/cancel`, { method: "POST" });
    // The next SSE frame (or the REST probe in connectEvents) will
    // surface the cancelled state and hide the button via applyState.
  } catch {
    /* SSE will reflect the result regardless */
  } finally {
    jobCancelBtn.disabled = false;
    jobCancelBtn.textContent = "Cancel";
  }
}

function sanitizeFilename(name) {
  // Strip extension, collapse whitespace, cap at 120 chars — mirrors the
  // backend _sanitize_title() so title and sourceUrl match on both sides.
  return name
    .replace(/\.[^.]+$/, "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 120);
}

export function wireJobForm() {
  jobCancelBtn.addEventListener("click", cancelCurrentJob);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    reset();
    setSubmitProcessing(true);

    const fileInput = document.getElementById("fileInput");
    const file = fileInput?.files?.[0] ?? null;
    const sanitized = file ? sanitizeFilename(file.name) : null;
    const sourceUrl = file ? `local:${sanitized}` : urlInput.value;
    const displayTitle = sanitized ?? (urlInput.value || "Processing track");

    const postUrlText = document.getElementById("post-url-text");
    if (postUrlText) postUrlText.textContent = displayTitle;

    // Show progress box immediately for file uploads — the HTTP upload of a
    // large file takes time and the UI would otherwise appear frozen.
    if (file) {
      jobBox.classList.remove("hidden");
      jobCancelBtn.classList.add("hidden");
      jobDetailEl.textContent = "Uploading…";
      startPhraseRotation("queued");
      lastStatus = "queued";
    }

    let fetchInit;
    if (file) {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("stems", JSON.stringify([...selectedStems]));
      fetchInit = { method: "POST", body: fd };
    } else {
      fetchInit = {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: urlInput.value,
          // Backend uses this to decide whether to ffmpeg-amix a
          // "selected stems" track (mix.wav) at the end of the pipeline.
          stems: [...selectedStems],
        }),
      };
    }

    let jobId;
    try {
      const res = await fetch("/api/jobs", fetchInit);
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || res.statusText);
      jobId = data.job_id;
    } catch (err) {
      if (file) jobBox.classList.add("hidden");
      showError(`Failed to start job: ${err.message}`);
      setSubmitProcessing(false);
      return;
    }

    setCurrentJobId(jobId);
    jobSources.set(jobId, sourceUrl);
    addTrackToLibrary({
      id: jobId,
      title: displayTitle,
      channel: "Processing",
      thumb: "",
      stems: [...selectedStems],
      selectedStems: [...selectedStems],
      audioStems: [],
      status: "processing",
      bpm: null,
      key: null,
      scale: null,
      keyConfidence: null,
      lufs: null,
      peakDb: null,
      sourceUrl,
    });
    setCurrentTrack(jobId);

    if (!file) {
      // YouTube path: keep progress box hidden until SSE events arrive.
      jobBox.classList.add("hidden");
      jobCancelBtn.classList.add("hidden");
      startPhraseRotation("queued");
      lastStatus = "queued";
    } else {
      // File path: clear the upload message — SSE will take over from here.
      jobDetailEl.textContent = "";
    }

    startJobPolling(jobId);
    connectEvents(jobId);
  });
}
