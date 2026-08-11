// queue.js — the import queue as the client sees it.
//
// One EventSource for the whole queue rather than one per job: browsers cap
// concurrent connections per origin at around six on HTTP/1.1 and the studio
// already spends most of that budget fetching stem WAVs, so twenty per-job
// streams would starve audio loading long before hitting any server limit.
//
// This module owns no DOM. It holds the latest snapshot and tells subscribers
// when it changes; catalog.js decides what that looks like.

const POLL_MS = 2000;
const MAX_SSE_ATTEMPTS = 6;

let snapshot = { running: null, queued: [], max_pending: 0, capacity_left: 0 };
let source = null;
let pollTimerId = null;
let attempt = 0;
const subscribers = new Set();

// ─── pure helpers (no DOM, no network -- the testable part) ───────────────────

export function queueCount(snap = snapshot) {
  return (snap.running ? 1 : 0) + (snap.queued?.length ?? 0);
}

export function ordinal(n) {
  const rem100 = n % 100;
  if (rem100 >= 11 && rem100 <= 13) return `${n}th`;
  const suffix = { 1: "st", 2: "nd", 3: "rd" }[n % 10] ?? "th";
  return `${n}${suffix}`;
}

/** Stage text for the running row. The backend stage already carries a
 *  percentage during separation ("Separating 42%"), so only append one when it
 *  does not, rather than rendering "Separating 42% 42%". */
export function runningLabel(job) {
  const stage = (job?.stage || "Working...").replace(/\.\.\.$/, "");
  if (/\d\s*%/.test(stage)) return stage;
  const pct = Math.round((job?.progress || 0) * 100);
  return pct > 0 ? `${stage} ${pct}%` : stage;
}

/** Per-job view state, keyed by job id, for whoever is drawing rows.
 *  Position counts the running job, so the first waiting job is 2nd in line. */
export function queueRowStates(snap = snapshot) {
  const rows = new Map();
  if (snap.running) {
    rows.set(snap.running.job_id, {
      state: "running",
      label: runningLabel(snap.running),
      progress: snap.running.progress || 0,
      position: 1,
    });
  }
  const offset = snap.running ? 2 : 1;
  (snap.queued ?? []).forEach((job, i) => {
    const place = i + offset;
    rows.set(job.job_id, {
      state: "waiting",
      label: `Queued - ${ordinal(place)} in line`,
      progress: 0,
      position: place,
    });
  });
  return rows;
}

// ─── state ───────────────────────────────────────────────────────────────────

export function getQueueSnapshot() {
  return snapshot;
}

export function onQueueChange(fn) {
  subscribers.add(fn);
  return () => subscribers.delete(fn);
}

function publish(next) {
  snapshot = next;
  for (const fn of subscribers) {
    try { fn(snapshot); } catch (e) { console.warn("[queue] subscriber failed:", e); }
  }
}

export async function refreshQueue() {
  try {
    const r = await fetch("/api/queue");
    if (!r.ok) return;
    publish(await r.json());
  } catch (e) {
    console.warn("[queue] refresh failed:", e);
  }
}

export async function cancelQueuedJob(jobId) {
  try {
    await fetch(`/api/jobs/${jobId}/cancel`, { method: "POST" });
  } catch (e) {
    console.warn("[queue] cancel failed:", e);
  }
  // Do not wait for the next frame: the row should go the moment it is clicked.
  await refreshQueue();
}

// ─── transport ───────────────────────────────────────────────────────────────

function startPolling() {
  if (pollTimerId) return;
  pollTimerId = setInterval(refreshQueue, POLL_MS);
  refreshQueue();
}

function stopPolling() {
  if (pollTimerId) {
    clearInterval(pollTimerId);
    pollTimerId = null;
  }
}

export function startQueueStream() {
  if (source) return;
  refreshQueue(); // first paint should not wait for the stream to connect

  const open = () => {
    const es = new EventSource("/api/queue/events");
    source = es;

    es.onmessage = (ev) => {
      attempt = 0;
      stopPolling(); // the stream is healthy again
      try {
        publish(JSON.parse(ev.data));
      } catch (e) {
        console.warn("[queue] bad frame:", e);
      }
    };

    es.onerror = () => {
      es.close();
      source = null;
      attempt += 1;
      if (attempt > MAX_SSE_ATTEMPTS) {
        // Give up on SSE and keep the view alive by polling, same fallback
        // shape as the per-job stream in job.js.
        startPolling();
        return;
      }
      setTimeout(open, 500 * Math.pow(2, attempt - 1)); // 0.5s .. 16s
    };
  };

  open();
}

export function stopQueueStream() {
  if (source) {
    source.close();
    source = null;
  }
  stopPolling();
}
