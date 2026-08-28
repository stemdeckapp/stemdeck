import {
  form, urlInput, submitBtn, errorEl, jobBox, jobTitleEl, jobStageEl,
  jobDetailEl, jobCancelBtn, progressEl, titleEl, bpmChip, keyChip,
  eventSource, setEventSource, setCurrentJobId,
  foregroundJobId, setForegroundJobId,
  audioEngine, multitrack,
  selectedStems, vocalSplitMode,
} from "./state.js";
import { destroyPlayer, wireUpAudio, setWaveformLoading, updateFooterTrack } from "./player.js";
import { notifyFailure, dismissFailuresByJobId } from "./notifications.js";
import { getStagePhrases } from "./phrases.js";
import { addTrackToLibrary, setCurrentTrack, updateTrackStatus, applyStemPresenceCards } from "./catalog.js";
import { initSections } from "./sections.js";
import { importPlaylist, looksLikePlaylist } from "./playlist.js";
import { t } from "./i18n.js";

// Playful stage label rotation (Claude-Code-style flair). The backend
// emits truthful stage strings; we surface them in the small #job-detail
// line so progress is debuggable, while #job-stage rotates whimsy.
const ROTATION_MS = 2500;
let phraseTimerId = null;
let lastStatus = null;
let jobPollTimerId = null;
const renderedJobs = new Set();
const jobSources = new Map();
// On-demand lead/backing vocal split (#275): which jobs asked for it at
// submit time (via the Extract bar's Vocals "All / Lead + Backing" toggle).
// Exported so catalog.js's completeSettledJob (the background-job path --
// there is no per-job SSE stream for those) can trigger it too, via
// runVocalSplitIfWanted below. Only covers the primary single-file/single-URL
// and batch-upload submit paths -- "Sync again" restores and playlist
// imports don't carry this choice.
export const jobVocalSplitModes = new Map();
// Guards against double-firing the split POST if a job's "done" state is
// somehow observed twice (e.g. a late SSE frame after the queue already
// settled it) -- the split is expensive, so this must stay a hard one-shot.
const splitAutoTriggered = new Set();

// Fires the on-demand vocal split (#275) for a job that finished with the
// Extract bar's "Lead + Backing" toggle on, then refreshes the library cache
// (catalog.js's openTrack reads a locally cached stem list, which otherwise
// stays stale after a job's stems change post-completion). Returns the job's
// state after the split (or the original state, unchanged, if the split
// wasn't requested/needed/possible) -- callers use the return value rather
// than assuming their own `state` is still current.
export async function runVocalSplitIfWanted(state) {
  if (state.status !== "done" || splitAutoTriggered.has(state.job_id)) return state;
  const wantsSplit = jobVocalSplitModes.get(state.job_id) === "split";
  const hasVocals = (state.stems || []).some((s) => s.name === "vocals");
  const alreadySplit = state.vocal_split === "done";
  if (!wantsSplit || !hasVocals || alreadySplit) return state;
  splitAutoTriggered.add(state.job_id);
  if (state.job_id === foregroundJobId) setWaveformLoading(true, "Splitting lead/backing vocals…");
  try {
    const res = await fetch(`/api/jobs/${state.job_id}/vocal-split`, { method: "POST" });
    if (!(res.ok || res.status === 202)) return state;
    const r2 = await fetch(`/api/jobs/${state.job_id}`);
    if (!r2.ok) return state;
    const finalState = await r2.json();
    addTrackToLibrary({
      id: finalState.job_id,
      title: finalState.title || "",
      channel: t("footer.extractedLabel"),
      thumb: finalState.thumbnail,
      stems: finalState.selected_stems || [...selectedStems],
      selectedStems: finalState.selected_stems || [...selectedStems],
      audioStems: finalState.stems || [],
      status: "done",
      duration: finalState.duration,
      bpm: finalState.bpm,
      key: finalState.key,
      scale: finalState.scale,
      keyConfidence: finalState.key_confidence,
      lufs: finalState.lufs,
      peakDb: finalState.peak_db,
      stemPresence: finalState.stem_presence,
      sections: finalState.sections,
      sectionsSource: finalState.sections_source,
      sourceUrl: jobSources.get(finalState.job_id) || "",
      createdAt: finalState.created_at,
    });
    return finalState;
  } catch (e) {
    console.warn("[job] vocal split failed:", e);
    return state;
  }
}

const TERMINAL_STATUSES = new Set(["done", "error", "cancelled"]);

// Last library-visible values written per job, so a 4 Hz progress stream does
// not re-run addTrackToLibrary (a full localStorage write plus a whole-sidebar
// render) for frames that change nothing the sidebar shows. Every frame carries
// the complete job state, so skipping redundant ones loses nothing: the next
// frame that does change status carries the analysis fields too.
const libraryRowKeys = new Map();

function libraryRowKey(state) {
  return [state.status, state.title || "", state.thumbnail || ""].join("\u0000");
}

// `processing` here means "a submit is in flight", not "a job is running".
// With a queue the form has to come back the instant the job is accepted, so
// the user can queue the next one.
function setSubmitProcessing(processing) {
  submitBtn.disabled = processing;
  submitBtn.classList.toggle("loading", processing);
  document.querySelector(".strip-sq-process")?.classList.toggle("loading", processing);
  const label = submitBtn.querySelector("span");
  if (label) label.textContent = processing ? t("job.processing") : t("job.process");
}

/** True when audio is loaded in the studio. Either engine counts: the Web Audio
 *  path sets audioEngine, the streaming path sets multitrack, and destroyPlayer
 *  clears both. Read at call time so the live bindings are current. */
function studioHasTrack() {
  return !!(audioEngine || multitrack);
}

function pickPhrase(status) {
  const stagePhrases = getStagePhrases();
  const pool = stagePhrases[status] || stagePhrases.default;
  return pool[Math.floor(Math.random() * pool.length)];
}

function setOverlayPhrase(text) {
  const el = document.getElementById("waveLoadingPhrase");
  if (el) el.textContent = text;
}

function startPhraseRotation(status) {
  stopPhraseRotation();
  const phrase = pickPhrase(status);
  jobStageEl.textContent = phrase;
  setOverlayPhrase(phrase);
  phraseTimerId = setInterval(() => {
    const p = pickPhrase(status);
    jobStageEl.textContent = p;
    setOverlayPhrase(p);
  }, ROTATION_MS);
}

function stopPhraseRotation() {
  if (phraseTimerId) {
    clearInterval(phraseTimerId);
    phraseTimerId = null;
  }
  jobStageEl.textContent = "";
}

function stopJobPolling() {
  if (jobPollTimerId) {
    clearInterval(jobPollTimerId);
    jobPollTimerId = null;
  }
}

// `retry` controls the button: "Try again" sends the user back to the URL field
// to start a fresh import, which is right for an import failure and wrong for
// anything else. Export failures pass retry:false and get a plain Dismiss, since
// the error box has no other way to be cleared.
export function showError(message, detail, { retry = true } = {}) {
  delete errorEl.dataset.kind; // see showPlaybackError
  errorEl.textContent = "";
  const msg = document.createElement("div");
  msg.className = "error-msg";
  msg.textContent = message;
  if (detail) {
    // Classified cause from the backend (e.g. "out-of-memory — ..."), shown
    // as a muted secondary line so failures are actionable, not opaque.
    const detailEl = document.createElement("div");
    detailEl.className = "error-detail";
    detailEl.textContent = detail;
    msg.appendChild(detailEl);
  }
  const btn = document.createElement("button");
  btn.className = "retry-btn";
  btn.type = "button";
  btn.textContent = retry ? t("job.tryAgain") : t("job.dismiss");
  btn.addEventListener("click", () => {
    errorEl.classList.add("hidden");
    if (retry) {
      urlInput.focus();
      urlInput.select();
    }
  });
  errorEl.append(msg, btn);
  errorEl.classList.remove("hidden");
}

function clearImportError() {
  delete errorEl.dataset.kind;
  errorEl.classList.add("hidden");
  errorEl.textContent = "";
}

// Playback failures reuse the import error box, which is the only alert surface
// the studio has. They are tagged so the player can retract its own message when
// the user loads a different track, without wiping an import failure the user
// has not read yet. Always retry:false -- "Try again" sends the user to the URL
// field, which is not what a broken stem file calls for.
export function showPlaybackError(message, detail, context = {}) {
  showError(message, detail, { retry: false });
  errorEl.dataset.kind = "playback";
  // The class of failure #359 was written about: a track that loads and then
  // does nothing. Recording it is what makes it reportable.
  notifyFailure({ kind: "playback", message, detail, context });
}

export function clearPlaybackError() {
  if (errorEl.dataset.kind === "playback") clearImportError();
}

// Playback actually succeeded for this track — clear any stale "playback
// failed" notification for it (#401). Only the playback kind: a successful
// play doesn't mean an unrelated import/export failure for the same track
// is resolved too.
export function resolvePlaybackSuccess(jobId) {
  if (jobId) dismissFailuresByJobId(jobId, "playback");
}

// Clear the import chrome (progress box, error, phrase rotation, foreground
// SSE) without touching the studio. Split out of reset() so a submit that goes
// to the back of the queue does not tear down audio the user is playing.
function resetImportUi() {
  if (eventSource) {
    eventSource.close();
    setEventSource(null);
  }
  stopJobPolling();
  stopPhraseRotation();
  lastStatus = null;
  clearImportError();
  jobBox.classList.add("hidden");
  jobCancelBtn.classList.add("hidden");
  jobTitleEl.textContent = "";
  jobStageEl.textContent = "";
  jobDetailEl.textContent = "";
  progressEl.value = 0;
  setSubmitProcessing(false);
  setForegroundJobId(null);
}

export function reset() {
  resetImportUi();
  destroyPlayer();
  setCurrentJobId(null);
}

// The running import no longer owns the studio: the user opened another track.
// The job keeps running and its SSE stays connected -- it still updates the
// library row -- it just stops repainting a view that is now showing something
// else. Cancel moves to the queue view, which is why the button goes away.
export function detachForegroundJob() {
  if (!foregroundJobId) return;
  setForegroundJobId(null);
  stopPhraseRotation();
  setWaveformLoading(false);
  jobBox.classList.add("hidden");
  jobCancelBtn.classList.add("hidden");
}

// The analysis cards under the waveform. Split out of applyState so the
// studio-owned DOM lives behind one call: only the job the user is actually
// looking at may write here, and that is far easier to see when it is one
// named function than fifty inline element lookups.
function applyStudioSummary(state) {
  const summaryKey = document.getElementById("summary-key");
  const summaryBpm = document.getElementById("summary-bpm");
  const summaryScale = document.getElementById("summary-scale");
  const summaryScaleName = document.getElementById("summary-scale-name");
  const summaryConfidence = document.getElementById("summary-confidence");
  const summaryConfidenceLabel = document.getElementById("summary-confidence-label");
  const summaryLufs = document.getElementById("summary-lufs");
  const summaryPeak = document.getElementById("summary-peak");
  const summaryDuration = document.getElementById("summary-duration");
  if (summaryKey && state.key) summaryKey.textContent = state.key;
  if (summaryBpm && state.bpm) summaryBpm.textContent = String(state.bpm);
  if (summaryScale && state.scale) summaryScale.textContent = state.scale;
  if (summaryScaleName && state.scale) summaryScaleName.textContent = state.scale;
  if (summaryLufs && state.lufs != null) summaryLufs.textContent = state.lufs.toFixed(1);
  if (summaryPeak && state.peak_db != null) summaryPeak.textContent = t("job.peakDb", { value: state.peak_db.toFixed(1) });
  if (summaryDuration && state.duration) {
    const m = Math.floor(state.duration / 60);
    const s = Math.floor(state.duration % 60).toString().padStart(2, "0");
    summaryDuration.textContent = `${m.toString().padStart(2, "0")}:${s}`;
  }
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
  const summaryDr = document.getElementById("summary-dr");
  const summaryDrLabel = document.getElementById("summary-dr-label");
  const summaryStability = document.getElementById("summary-stability");
  const summaryStabilityLabel = document.getElementById("summary-stability-label");
  if (summaryDr && state.dynamic_range != null) summaryDr.textContent = String(state.dynamic_range);
  if (summaryDrLabel && state.dynamic_range != null) {
    const dr = state.dynamic_range;
    summaryDrLabel.textContent = dr < 7 ? t("job.dr.compressed") : dr < 10 ? t("job.dr.moderate") : dr < 14 ? t("job.dr.high") : t("job.dr.wide");
  }
  if (summaryStability && state.tempo_stability != null) {
    summaryStability.textContent = `${state.tempo_stability}%`;
    summaryStability.className = "meta-card-value" + (state.tempo_stability >= 80 ? " stability-high" : "");
  }
  if (summaryStabilityLabel && state.tempo_stability != null) {
    const s = state.tempo_stability;
    summaryStabilityLabel.textContent = s >= 90 ? t("job.stability.veryStable") : s >= 70 ? t("job.stability.stable") : s >= 50 ? t("job.stability.moderate") : t("job.stability.variable");
  }
  if (state.stem_presence != null) {
    applyStemPresenceCards(state.stem_presence);
  }
}

// Chains the on-demand vocal split onto a foreground job's completion (see
// runVocalSplitIfWanted above) -- from the user's perspective it's one
// action, even though the backend keeps it a separate, best-effort pass over
// the already-done job. Best-effort: any failure just proceeds with the base
// stems (the vocals lane), same as if the user had never asked for a split.
async function finishDoneJob(state) {
  const finalState = await runVocalSplitIfWanted(state);
  wireUpAudio(
    finalState.job_id,
    finalState.stems || [],
    finalState.duration || 0,
    finalState.thumbnail,
    finalState.mix_url ?? null,
    finalState.title || "",
    null,
    finalState.has_video ?? false,
  );
  initSections(finalState.job_id, finalState.sections, finalState.duration || 0);
}

function applyState(state) {
  // Everything below the library update writes to DOM the studio owns, and
  // only the import the user is actually watching may touch it. A background
  // job still gets its library row updated -- that is the point of the queue.
  const isForeground = !!state.job_id && state.job_id === foregroundJobId;

  if (state.job_id && libraryRowKeys.get(state.job_id) !== libraryRowKey(state)) {
    libraryRowKeys.set(state.job_id, libraryRowKey(state));
    addTrackToLibrary({
      id: state.job_id,
      // urlInput only speaks for the foreground job. While a background import
      // runs the user may already be typing the next URL in there, and it must
      // not end up as some other track's title or source.
      title: state.title || (isForeground ? urlInput.value : "") || t("job.processingTrackTitle"),
      channel: state.status === "done" ? t("footer.extractedLabel") : t("job.processing"),
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
      stemPresence: state.stem_presence,
      sections: state.sections,
      sectionsSource: state.sections_source,
      sourceUrl: jobSources.get(state.job_id) || (isForeground ? urlInput.value : ""),
      createdAt: state.created_at,
    });
  }

  if (state.job_id && TERMINAL_STATUSES.has(state.status)) libraryRowKeys.delete(state.job_id);

  // Everything from here down is studio DOM.
  if (!isForeground) return;

  setCurrentTrack(state.job_id);
  if (state.title) {
    jobTitleEl.textContent = state.title;
    titleEl.textContent = state.title;
  }
  if (state.bpm) bpmChip.textContent = t("job.bpmValue", { bpm: state.bpm });
  if (state.key) keyChip.textContent = state.key;
  if (state.title || state.bpm || state.key || state.thumbnail) {
    updateFooterTrack({
      title: state.title,
      thumbnail: state.thumbnail,
      key: state.key,
      bpm: state.bpm,
      stemCount: state.stems ? state.stems.filter((s) => s.name !== "original").length : null,
    });
  }
  applyStudioSummary(state);
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

  // The three terminal branches no longer clear the submit button: it is
  // released as soon as the POST returns, so the next import can be queued
  // while this one is still running.
  if (state.status === "error") {
    stopJobPolling();
    updateTrackStatus(state.job_id, "error");
    setWaveformLoading(false);
    showError(state.error || t("job.unknownError"), state.error_detail);
    // Also record it: the banner above is transient and the user may well
    // dismiss it before deciding to report anything.
    notifyFailure({
      kind: "import",
      message: state.error || t("job.unknownError"),
      detail: state.error_detail || null,
      context: {
        jobId: state.job_id,
        stage: state.stage,
        device: state.compute_device,
        gpuFallback: state.gpu_fallback,
        timings: state.stage_timings ? JSON.stringify(state.stage_timings) : null,
      },
    });
    setForegroundJobId(null);
  } else if (state.status === "cancelled") {
    stopJobPolling();
    updateTrackStatus(state.job_id, "cancelled");
    setWaveformLoading(false);
    jobBox.classList.add("hidden");
    setForegroundJobId(null);
  } else if (state.status === "done") {
    stopJobPolling();
    updateTrackStatus(state.job_id, "done");
    jobBox.classList.add("hidden");
    setForegroundJobId(null);
    if (!renderedJobs.has(state.job_id)) {
      // Claimed before the (possibly async) finish below starts, so a second
      // SSE/poll tick for the same job arriving mid-split can't re-enter this
      // branch and fire the vocal-split call twice.
      renderedJobs.add(state.job_id);
      finishDoneJob(state);
    }
  }
}

async function probeJob(jobId) {
  const r = await fetch(`/api/jobs/${jobId}`);
  if (!r.ok) {
    if (r.status === 404) throw new Error(t("job.noLongerExists"));
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
// Falls back to REST polling only after SSE exhausts its retry budget.
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
        if (err.message === t("job.noLongerExists")) {
          stopped = true;
          showError(err.message);
          setSubmitProcessing(false);
          return;
        }
        // Network down -- fall through to backoff.
      }

      attempt += 1;
      if (attempt > 6) {
        // SSE gave up — activate REST polling as the fallback.
        startJobPolling(jobId);
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
  // The import, not the track in the studio. Reading currentJobId here meant
  // that opening another track mid-import pointed Cancel at the wrong job.
  const id = foregroundJobId;
  if (!id) return;
  jobCancelBtn.disabled = true;
  jobCancelBtn.textContent = t("job.cancelling");
  try {
    await fetch(`/api/jobs/${id}/cancel`, { method: "POST" });
    // The next SSE frame (or the REST probe in connectEvents) will
    // surface the cancelled state and hide the button via applyState.
  } catch {
    /* SSE will reflect the result regardless */
  } finally {
    jobCancelBtn.disabled = false;
    jobCancelBtn.textContent = t("job.cancel");
  }
}

async function postFileJob(file) {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("stems", JSON.stringify([...selectedStems]));
  const res = await fetch("/api/jobs", { method: "POST", body: fd });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data.job_id;
}

/** Give an accepted upload its library row straight away, so a batch appears as
 *  rows the moment each file lands rather than only once every upload is done. */
function registerUploadRow(jobId, file) {
  const title = sanitizeFilename(file.name);
  const sourceUrl = `local:${title}`;
  jobSources.set(jobId, sourceUrl);
  jobVocalSplitModes.set(jobId, selectedStems.has("vocals") ? vocalSplitMode : "all");
  addTrackToLibrary({
    id: jobId,
    title,
    channel: "Processing",
    thumb: "",
    stems: [...selectedStems],
    selectedStems: [...selectedStems],
    audioStems: [],
    status: "queued",
    bpm: null,
    key: null,
    scale: null,
    keyConfidence: null,
    lufs: null,
    peakDb: null,
    sourceUrl,
  });
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

// Programmatic URL import — re-uses the full studio/SSE pipeline (same as the
// import form's URL path). Used by the library "Sync again" auto-restore to
// re-download + re-separate a track whose backend audio was swept. Takes over
// the studio like a normal import. Returns the new job id, or null on failure.
export async function importFromUrl(url, { title, stems } = {}) {
  if (!url || url.startsWith("local:")) return null; // local files can't auto-restore
  reset();
  setSubmitProcessing(true);
  setWaveformLoading(true, "");
  const stemSel = stems?.length ? stems : [...selectedStems];

  let jobId;
  try {
    const res = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, stems: stemSel }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);
    jobId = data.job_id;
  } catch (err) {
    showError(t("job.restoreFailed", { message: err.message }));
    setSubmitProcessing(false);
    return null;
  }

  setSubmitProcessing(false);
  setCurrentJobId(jobId);
  setForegroundJobId(jobId);
  jobSources.set(jobId, url);
  // Merges into the existing library entry by sourceUrl (replaceTrackId),
  // preserving its folder placement; status updates as SSE frames arrive.
  addTrackToLibrary({
    id: jobId,
    title: title || url || t("job.processingTrackTitle"),
    channel: "Processing",
    thumb: "",
    stems: stemSel,
    selectedStems: stemSel,
    audioStems: [],
    status: "processing",
    bpm: null,
    key: null,
    scale: null,
    keyConfidence: null,
    lufs: null,
    peakDb: null,
    sourceUrl: url,
  });
  setCurrentTrack(jobId);

  jobBox.classList.add("hidden");
  jobCancelBtn.classList.add("hidden");
  startPhraseRotation("queued");
  lastStatus = "queued";
  connectEvents(jobId);
  return jobId;
}

export function wireJobForm() {
  jobCancelBtn.addEventListener("click", cancelCurrentJob);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();

    // An import must never take a loaded studio away from the user. With a
    // track playing, the new job goes straight to the background: no player
    // teardown, no loading overlay, no takeover when it finishes. It reports
    // progress on its library row instead.
    //
    // An import already holding the foreground keeps it, too. Otherwise
    // queueing a second track would point the studio overlay at a job that has
    // not started, and the first import -- the one about to finish -- would no
    // longer be the one that loads.
    const background = studioHasTrack() || !!foregroundJobId;
    if (background) {
      // Deliberately NOT resetImportUi(): that closes the running import's
      // event stream and drops its foreground claim, which would leave the job
      // about to finish with nothing listening for its completion.
      clearImportError();
    } else {
      reset();
    }
    setSubmitProcessing(true);

    const fileInput = document.getElementById("fileInput");
    // Prefer _file cache: browsers (WKWebView, Chromium) silently clear
    // fileInput.files after a fetch() submission, breaking re-submits.
    const file = fileInput?._file ?? fileInput?.files?.[0] ?? null;

    // A playlist is its own flow: expand, confirm the count, then queue every
    // track into a folder named after it. Nothing takes the studio.
    if (!file && looksLikePlaylist(urlInput.value)) {
      const url = urlInput.value;
      const queued = await importPlaylist(url, [...selectedStems]);
      setSubmitProcessing(false);
      if (queued) urlInput.value = "";
      return;
    }
    // Several files dropped at once: upload them one after another. Parallel
    // uploads of several 400 MB bodies would thrash memory on both ends, and
    // the endpoint takes exactly one file per request anyway. The first one
    // takes the studio if it is free, exactly as a single import would; the
    // rest queue behind it.
    const batch = fileInput?._files ?? null;
    if (batch && batch.length > 1) {
      if (!background) setWaveformLoading(true, "Uploading…");
      let queued = 0;
      let failure = null;
      for (const item of batch) {
        try {
          const id = await postFileJob(item);
          registerUploadRow(id, item);
          queued += 1;
          if (queued === 1 && !background) {
            setCurrentJobId(id);
            setForegroundJobId(id);
            setCurrentTrack(id);
            jobBox.classList.add("hidden");
            jobCancelBtn.classList.add("hidden");
            startPhraseRotation("queued");
            lastStatus = "queued";
            connectEvents(id);
          }
        } catch (err) {
          failure = err;
          break; // a full queue will reject the rest too; stop asking
        }
      }
      setSubmitProcessing(false);
      fileInput._clear?.();
      if (failure) {
        if (!queued && !background) {
          setWaveformLoading(false);
          setForegroundJobId(null);
        }
        showError(
          `Queued ${queued} of ${batch.length} files: ${failure.message}`,
          null,
          { retry: false },
        );
      }
      return;
    }

    const sanitized = file ? sanitizeFilename(file.name) : null;
    const sourceUrl = file ? `local:${sanitized}` : urlInput.value;
    const displayTitle = sanitized ?? (urlInput.value || t("job.processingTrackTitle"));

    const postUrlText = document.getElementById("post-url-text");
    if (postUrlText) postUrlText.textContent = displayTitle;

    // Show overlay immediately for both paths. File uploads show "Uploading…"
    // in the overlay phrase until the fetch completes and SSE takes over.
    // Skipped entirely for a background import -- the overlay covers the
    // studio, which is exactly what must not happen here.
    if (!background) {
      setWaveformLoading(true, file ? "Uploading…" : "");
      if (file) {
        lastStatus = "queued";
      }
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
      showError(t("job.startFailed", { message: err.message }));
      setSubmitProcessing(false);
      return;
    }

    // Released here, not when the job finishes: the queue is what the button
    // hands off to now, so the form is free again the moment the job exists.
    setSubmitProcessing(false);
    // The server has the upload; disarm the picker so the next click cannot
    // silently import the same file a second time.
    if (file) fileInput._clear?.();
    jobSources.set(jobId, sourceUrl);
    jobVocalSplitModes.set(jobId, selectedStems.has("vocals") ? vocalSplitMode : "all");
    addTrackToLibrary({
      id: jobId,
      title: displayTitle,
      channel: t("job.processing"),
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

    if (background) {
      // No per-job stream: opening one per queued import would burn through
      // the browser's ~6 connections per origin and starve stem loading. The
      // shared queue stream drives the row, and catalog.js completes the
      // library entry when the job leaves the queue.
      if (postUrlText) postUrlText.textContent = "";
      return;
    }

    setCurrentJobId(jobId);
    setForegroundJobId(jobId);
    setCurrentTrack(jobId);

    // Keep job box hidden, overlay drives the UI. Start phrase rotation now
    // that the job exists on the server.
    jobBox.classList.add("hidden");
    jobCancelBtn.classList.add("hidden");
    startPhraseRotation("queued");
    lastStatus = "queued";

    connectEvents(jobId);
  });
}
